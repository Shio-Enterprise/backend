"""Cupons: uso derivado dos pedidos, validação e cálculo do desconto.

Não há contador de usos: o uso é um pedido válido com o cupom, então volta
sozinho quando o pedido é cancelado.
"""

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from orders.models import (
    Coupon,
    CouponDiscountType,
    CustomerOrder,
    OrderStatus,
    normalize_coupon_code,
)
from orders.money import normalize_money

# Na ordem em que são verificados. São o contrato com o frontend.
COUPON_ERROR_MESSAGES = {
    "coupon_not_found": "Cupom não encontrado.",
    "coupon_inactive": "Este cupom não está mais disponível.",
    "coupon_not_started": "Este cupom ainda não está valendo.",
    "coupon_expired": "Este cupom expirou.",
    "coupon_first_purchase_only": "Cupom válido apenas para a primeira compra.",
    "coupon_min_value": "Faltam {missing} para usar este cupom.",
    "coupon_not_applicable": "Nenhum item do carrinho é elegível para este cupom.",
    "coupon_limit_reached": "Este cupom esgotou.",
    "coupon_user_limit_reached": "Você já usou este cupom.",
}


@dataclass(frozen=True)
class CouponResult:
    coupon: Coupon | None
    discount: Decimal
    error_code: str | None  # None também quando não havia código
    error_message: str | None


NO_COUPON = CouponResult(None, Decimal("0.00"), None, None)


def valid_order_q(prefix=""):
    """Pedido válido: qualquer pedido não cancelado.

    Reserva vencida ainda conta até o cancelamento: senão o cliente poderia
    reusar o cupom e pagar o link antigo, que o webhook ainda aceita.
    """
    return ~Q(**{f"{prefix}status": OrderStatus.CANCELED})


def count_coupon_uses(coupon, user=None):
    orders = CustomerOrder.objects.filter(valid_order_q(), coupon=coupon)
    if user is not None:
        orders = orders.filter(user=user)
    return orders.count()


def resolve_coupon(user, items, subtotal, code=None, *, lock=False):
    """Decide qual cupom vale para o carrinho e quanto ele desconta.

    Sem código, aplica o primeiro cupom automático elegível. Com código, só
    ele é avaliado e o erro não cai para o automático.

    lock=True trava o cupom antes de contar os usos; quem chama já deve ter
    travado o usuário (regra de primeira compra).
    """
    code = normalize_coupon_code(code) if code else ""
    if not user.is_authenticated:
        if code:
            raise ValueError("Cupom digitado exige usuário autenticado.")
        return NO_COUPON

    coupons = Coupon.objects.all()
    if lock:
        coupons = coupons.select_for_update()
    now = timezone.now()

    if not code:
        candidates = coupons.filter(is_active=True, auto_apply=True).order_by(
            "created_at", "id"
        )
        for coupon in candidates:
            result = _evaluate(coupon, user, items, subtotal, now)
            if result.error_code is None:
                return result
        return NO_COUPON

    coupon = coupons.filter(code=code).first()
    if coupon is None:
        return _error("coupon_not_found")
    return _evaluate(coupon, user, items, subtotal, now)


def _evaluate(coupon, user, items, subtotal, now):
    if not coupon.is_active:
        return _error("coupon_inactive")
    if coupon.starts_at and coupon.starts_at > now:
        return _error("coupon_not_started")
    if coupon.expiration_date and coupon.expiration_date <= now:
        return _error("coupon_expired")
    if (
        coupon.first_purchase_only
        and CustomerOrder.objects.filter(valid_order_q(), user=user).exists()
    ):
        return _error("coupon_first_purchase_only")
    if coupon.min_order_value and subtotal < coupon.min_order_value:
        missing = _format_brl(coupon.min_order_value - subtotal)
        return _error("coupon_min_value", missing=missing)

    restricted, eligible = _eligible_items(coupon, items)
    # Sem restrição, carrinho vazio é só desconto zero, não erro.
    if restricted and not eligible:
        return _error("coupon_not_applicable")
    # Contagens por último: fazem consulta extra e dependem da trava.
    if (
        coupon.max_uses_total is not None
        and count_coupon_uses(coupon) >= coupon.max_uses_total
    ):
        return _error("coupon_limit_reached")
    if (
        coupon.max_uses_per_user is not None
        and count_coupon_uses(coupon, user=user) >= coupon.max_uses_per_user
    ):
        return _error("coupon_user_limit_reached")

    base = sum((item["total_price"] for item in eligible), Decimal("0.00"))
    if coupon.discount_type == CouponDiscountType.PERCENTAGE:
        discount = base * coupon.discount_value / Decimal("100")
        if coupon.max_discount_amount is not None:
            discount = min(discount, coupon.max_discount_amount)
    else:
        # O fixo nunca passa da base elegível.
        discount = min(coupon.discount_value, base)
    discount = normalize_money(min(subtotal, max(Decimal("0.00"), discount)))
    return CouponResult(coupon, discount, None, None)


def _eligible_items(coupon, items):
    """Devolve (restrito, itens no escopo do cupom)."""
    drop_ids = set(coupon.drops.values_list("id", flat=True))
    category_ids = set(coupon.categories.values_list("id", flat=True))
    if not drop_ids and not category_ids:
        return False, list(items)
    return True, [
        item
        for item in items
        if item["variation"].product.drop_id in drop_ids
        or item["variation"].product.category_id in category_ids
    ]


def _error(error_code, **params):
    message = COUPON_ERROR_MESSAGES[error_code].format(**params)
    return CouponResult(None, Decimal("0.00"), error_code, message)


def _format_brl(value):
    """Decimal('1234.5') -> 'R$ 1.234,50'."""
    text = f"{normalize_money(value):,.2f}"
    return "R$ " + text.replace(",", "_").replace(".", ",").replace("_", ".")
