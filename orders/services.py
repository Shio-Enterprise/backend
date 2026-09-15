from decimal import Decimal
from uuid import UUID

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.db import models, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework.exceptions import ValidationError

from orders.models import (
    Cart,
    CartItem,
    Coupon,
    CustomerOrder,
    OrderStatus,
    OrderStatusLog,
    Payment,
    PaymentStatus,
)
from products.models import ProductVariation, StockMovement
from products.services import move_stock

ALLOWED_TRANSITIONS = {
    OrderStatus.AWAITING_PAYMENT: {
        OrderStatus.PAID,
        OrderStatus.CANCELED,
    },
    OrderStatus.PAID: {
        OrderStatus.PREPARING,
    },
    OrderStatus.PREPARING: {
        OrderStatus.SHIPPED,
    },
    OrderStatus.SHIPPED: {
        OrderStatus.DELIVERED,
    },
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELED: set(),
}


def create_infinitepay_checkout(order, request):
    payment, _ = Payment.objects.get_or_create(
        order=order,
        defaults={"method": "CREDIT_CARD", "total_amount": order.total_amount},
    )

    items_data = []
    for item in order.items.select_related("variation", "variation__product"):
        items_data.append(
            {
                "quantity": item.quantity,
                "price": int(item.unit_price * 100),
                "description": item.product_name,
            }
        )

    if order.shipping_cost > 0:
        items_data.append(
            {
                "quantity": 1,
                "price": int(order.shipping_cost * 100),
                "description": "Frete",
            }
        )

    if order.discount_amount > 0:
        # Linha negativa para que o total cobrado pelo gateway bata com
        # order.total_amount (subtotal - desconto + frete). Sem ela, o cliente
        # veria o desconto na UI mas seria cobrado o valor cheio.
        items_data.append(
            {
                "quantity": 1,
                "price": -int(order.discount_amount * 100),
                "description": "Desconto de boas-vindas",
            }
        )

    redirect_url = request.build_absolute_uri("/api/orders/pagamento-sucesso/")

    phone_number = ""
    try:
        profile = order.user.profile
        phone_number = profile.phone_number or ""
    except ObjectDoesNotExist:
        phone_number = ""

    payload = {
        "handle": settings.INFINITEPAY_HANDLE,
        "redirect_url": redirect_url,
        "order_nsu": str(order.id),
        "items": items_data,
        "customer": {
            "name": order.user.name,
            "email": order.user.email,
            "phone_number": phone_number,
        },
        "address": {
            "cep": order.shipping_zip_code,
            "street": order.shipping_street,
            "neighborhood": order.shipping_neighborhood,
            "number": order.shipping_number,
            "complement": order.shipping_complement or "",
        },
    }

    headers = {"Content-Type": "application/json"}

    response = requests.post(
        "https://api.checkout.infinitepay.io/links",
        json=payload,
        headers=headers,
        timeout=10,
    )

    response.raise_for_status()
    data = response.json()

    payment.status = PaymentStatus.PROCESSING
    payment.save()

    return data.get("url")


def check_payment_status(order_nsu, transaction_nsu, slug):
    payload = {
        "handle": settings.INFINITEPAY_HANDLE,
        "order_nsu": order_nsu,
        "transaction_nsu": transaction_nsu,
        "slug": slug,
    }

    headers = {"Content-Type": "application/json"}

    response = requests.post(
        "https://api.checkout.infinitepay.io/payment_check",
        json=payload,
        headers=headers,
        timeout=10,
    )

    if response.status_code == 200:
        return response.json()
    return None


def get_or_create_user_cart(user):
    cart, _ = Cart.objects.get_or_create(user=user, status="ACTIVE")
    return cart


def get_cart_data(request):
    price_at = timezone.now()
    if request.user.is_authenticated:
        cart = Cart.objects.filter(user=request.user, status="ACTIVE").first()
        if not cart:
            welcome_coupon, welcome_discount_amount = get_welcome_discount_preview(
                request.user, Decimal("0.00")
            )
            return {
                "id": None,
                "items": [],
                "subtotal": Decimal("0.00"),
                "eligible_for_welcome_discount": welcome_coupon is not None,
                "welcome_discount_amount": welcome_discount_amount,
            }

        items = []
        subtotal = Decimal("0.00")
        for item in cart.items.select_related("variation", "variation__product").all():
            item.unit_price = item.variation.product.price_at(price_at)
            total_price = item.quantity * item.unit_price
            subtotal += total_price
            items.append(
                {
                    "variation_id": item.variation.id,
                    "product_id": item.variation.product.id,
                    "product_name": item.variation.product.name,
                    "size": item.variation.size,
                    "sku": item.variation.sku,
                    "quantity": item.quantity,
                    "unit_price": item.unit_price,
                    "base_price": item.variation.product.base_price,
                    "is_promotion_active": item.variation.product.promotion_active_at(
                        price_at
                    ),
                    "total_price": total_price,
                    "stock_quantity": item.variation.stock_quantity,
                }
            )
        welcome_coupon, welcome_discount_amount = get_welcome_discount_preview(
            request.user, subtotal
        )
        return {
            "id": cart.id,
            "items": items,
            "subtotal": subtotal,
            "eligible_for_welcome_discount": welcome_coupon is not None,
            "welcome_discount_amount": welcome_discount_amount,
        }
    else:
        session_cart = request.session.get("cart", {})
        items = []
        subtotal = Decimal("0.00")
        invalid_variation_ids = []

        variation_ids = list(session_cart.keys())
        variations = ProductVariation.objects.filter(
            id__in=variation_ids
        ).select_related("product")
        variations_by_id = {str(v.id): v for v in variations}

        for var_id_str, item_data in session_cart.items():
            variation = variations_by_id.get(var_id_str)
            if not variation:
                invalid_variation_ids.append(var_id_str)
                continue

            quantity = item_data.get("quantity", 0)
            unit_price = variation.product.price_at(price_at)

            total_price = quantity * unit_price
            subtotal += total_price

            items.append(
                {
                    "variation_id": variation.id,
                    "product_id": variation.product.id,
                    "product_name": variation.product.name,
                    "size": variation.size,
                    "sku": variation.sku,
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "base_price": variation.product.base_price,
                    "is_promotion_active": variation.product.promotion_active_at(
                        price_at
                    ),
                    "total_price": total_price,
                    "stock_quantity": variation.stock_quantity,
                }
            )

        if invalid_variation_ids:
            for iv_id in invalid_variation_ids:
                session_cart.pop(iv_id, None)
            request.session["cart"] = session_cart
            request.session.modified = True

        return {
            "id": None,
            "items": items,
            "subtotal": subtotal,
            "eligible_for_welcome_discount": False,
            "welcome_discount_amount": Decimal("0.00"),
        }


def add_item_to_cart(request, variation_id, quantity):
    if request.user.is_authenticated:
        with transaction.atomic():
            variation = get_object_or_404(
                ProductVariation.objects.select_for_update().select_related("product"),
                id=variation_id,
            )

            cart = get_or_create_user_cart(request.user)
            cart_item, _ = CartItem.objects.get_or_create(
                cart=cart,
                variation=variation,
                defaults={
                    "quantity": 0,
                    "unit_price": variation.product.effective_price,
                },
            )

            new_quantity = cart_item.quantity + quantity
            if new_quantity > variation.stock_quantity:
                raise ValueError(
                    f"Estoque insuficiente para {variation.product.name}. Disponível: {variation.stock_quantity}"
                )

            cart_item.quantity = new_quantity
            cart_item.unit_price = variation.product.effective_price
            cart_item.save()
    else:
        variation = get_object_or_404(
            ProductVariation.objects.select_related("product"), id=variation_id
        )
        session_cart = request.session.get("cart", {})
        var_id_str = str(variation.id)

        current_data = session_cart.get(var_id_str, {})
        current_quantity = current_data.get("quantity", 0)
        new_quantity = current_quantity + quantity

        if new_quantity > variation.stock_quantity:
            raise ValueError(
                f"Estoque insuficiente para {variation.product.name}. Disponível: {variation.stock_quantity}"
            )

        session_cart[var_id_str] = {
            "quantity": new_quantity,
            "unit_price": str(variation.product.effective_price),
        }
        request.session["cart"] = session_cart
        request.session.modified = True


def update_item_quantity(request, variation_id, quantity):
    variation = get_object_or_404(
        ProductVariation.objects.select_related("product"), id=variation_id
    )

    if quantity > variation.stock_quantity:
        raise ValueError(
            f"Estoque insuficiente para {variation.product.name}. Disponível: {variation.stock_quantity}"
        )

    if request.user.is_authenticated:
        cart = get_or_create_user_cart(request.user)
        cart_item = get_object_or_404(CartItem, cart=cart, variation=variation)
        cart_item.quantity = quantity
        cart_item.unit_price = variation.product.effective_price
        cart_item.save()
    else:
        session_cart = request.session.get("cart", {})
        var_id_str = str(variation.id)
        if var_id_str not in session_cart:
            raise KeyError("Item não está no carrinho.")

        session_cart[var_id_str]["quantity"] = quantity
        session_cart[var_id_str]["unit_price"] = str(variation.product.effective_price)
        request.session["cart"] = session_cart
        request.session.modified = True


def remove_item_from_cart(request, variation_id):
    if request.user.is_authenticated:
        cart = Cart.objects.filter(user=request.user, status="ACTIVE").first()
        if not cart:
            raise KeyError("Carrinho não encontrado.")
        cart_item = get_object_or_404(CartItem, cart=cart, variation_id=variation_id)
        cart_item.delete()
    else:
        session_cart = request.session.get("cart", {})
        var_id_str = str(variation_id)
        if var_id_str not in session_cart:
            raise KeyError("Item não está no carrinho.")
        session_cart.pop(var_id_str)
        request.session["cart"] = session_cart
        request.session.modified = True


def clear_cart(request):
    if request.user.is_authenticated:
        cart = Cart.objects.filter(user=request.user, status="ACTIVE").first()
        if cart:
            cart.items.all().delete()
    else:
        request.session["cart"] = {}
        request.session.modified = True


def merge_session_cart_to_db(request, user):
    session_cart = request.session.get("cart", {})
    if not session_cart:
        return
    with transaction.atomic():
        db_cart = get_or_create_user_cart(user)
        variation_ids = list(session_cart.keys())
        # Filter only valid UUIDs to avoid ValidationError on UUIDField
        valid_ids = []
        for vid in variation_ids:
            try:
                valid_ids.append(UUID(str(vid)))
            except Exception:
                continue

        if valid_ids:
            variations = (
                ProductVariation.objects.select_for_update()
                .filter(id__in=valid_ids)
                .select_related("product")
            )
        else:
            variations = ProductVariation.objects.none()
        variations_by_id = {str(v.id): v for v in variations}

        for var_id_str, item_data in session_cart.items():
            variation = variations_by_id.get(var_id_str)
            if not variation:
                continue

            quantity = item_data.get("quantity", 0)
            cart_item, _ = CartItem.objects.get_or_create(
                cart=db_cart,
                variation=variation,
                defaults={
                    "quantity": 0,
                    "unit_price": variation.product.effective_price,
                },
            )

            new_quantity = cart_item.quantity + quantity
            if new_quantity > variation.stock_quantity:
                new_quantity = variation.stock_quantity

            cart_item.quantity = new_quantity
            cart_item.unit_price = variation.product.effective_price
            cart_item.save()

        request.session.pop("cart", None)
        request.session.modified = True
        try:
            request.session.save()
        except Exception:
            # In some contexts session backend may not support save here;
            # ensure modified flag is set so caller can persist if needed.
            pass


@transaction.atomic
def update_status(order, new_status, changed_by=None, tracking_code=None, comment=None):
    """Centraliza a atualização de status de pedidos e cria um log histórico.

    Args:
        order: CustomerOrder instance
        new_status: OrderStatus value
        changed_by: User who made the change (optional)
        tracking_code: optional tracking code to save
        comment: optional comment/observation
    """
    locked = CustomerOrder.objects.select_for_update().get(pk=order.pk)
    order.status = locked.status
    previous_status = order.status
    if new_status == OrderStatus.CANCELED:
        shipped = (
            previous_status in (OrderStatus.SHIPPED, OrderStatus.DELIVERED)
            or order.status_logs.filter(
                new_status__in=[OrderStatus.SHIPPED, OrderStatus.DELIVERED]
            ).exists()
        )
        if not shipped:
            restore_order_stock(order, changed_by=changed_by)

    allowed = ALLOWED_TRANSITIONS.get(previous_status, set())

    if new_status not in allowed:
        raise ValidationError({
            "status": (
                f"Transição inválida: "
                f"{previous_status} -> {new_status}."
            )
        })

    if tracking_code:
        order.tracking_code = tracking_code

    order.status = new_status
    order.save(update_fields=["tracking_code", "status", "updated_at"])

    try:
        payment = order.payment
    except Exception:
        payment = None

    if new_status == OrderStatus.CANCELED and payment:
        if payment.status != PaymentStatus.PAID:
            payment.status = PaymentStatus.FAILED
            payment.save(update_fields=["status"])

    OrderStatusLog.objects.create(
        order=order,
        changed_by=changed_by,
        previous_status=previous_status,
        new_status=new_status,
        tracking_code=tracking_code,
        comment=comment,
    )

    return order


def update_tracking_code(order, tracking_code, changed_by=None, comment=None):
    if order.status != OrderStatus.SHIPPED:
        raise ValidationError({
            "tracking_code": (
                "O código de rastreio só pode ser alterado "
                "em pedidos enviados."
            )
        })

    if not tracking_code or not tracking_code.strip():
        raise ValidationError({
            "tracking_code": "O código de rastreio é obrigatório."
        })

    order.tracking_code = tracking_code
    order.save(update_fields=["tracking_code", "updated_at"])

    return order


@transaction.atomic
def restore_order_stock(order, changed_by=None, physical_return=False):
    CustomerOrder.objects.select_for_update().get(pk=order.pk)
    if physical_return and not changed_by:
        raise ValueError("Retorno físico exige confirmação administrativa.")
    for item in order.items.order_by("variation_id"):
        sale = StockMovement.objects.filter(
            order_item=item, reason="VENDA", origin_type="ORDER"
        ).first()
        if not sale or StockMovement.objects.filter(reverses_movement=sale).exists():
            continue
        move_stock(
            variation=item.variation,
            kind="ENTRADA",
            reason="DEVOLUCAO",
            quantity=item.quantity,
            origin_type="ORDER",
            origin_id=order.pk,
            order_item=item,
            idempotency_key=f"return:{item.pk}",
            created_by=changed_by,
            note="Retorno físico confirmado"
            if physical_return
            else "Cancelamento antes da expedição",
            reverses_movement=sale,
        )


def _compute_welcome_discount(user, subtotal):
    """Lógica compartilhada de elegibilidade e cálculo do desconto de
    boas-vindas, usada por get_welcome_discount e get_welcome_discount_preview.

    Retorna (coupon, discount_amount) ou (None, Decimal('0.00')) se o usuário
    não for elegível. NÃO adquire lock algum: quem precisa serializar contra
    checkouts concorrentes (get_welcome_discount) deve travar a linha do
    usuário ANTES de chamar esta função.

    Só considera o cupom BEMVINDO10 se ele estiver ativo e não expirado, e
    respeita o discount_type configurado (PERCENTAGE ou FIXED_VALUE), de modo
    que uma edição da linha do cupom no admin não seja silenciosamente
    ignorada.
    """
    if not user.is_authenticated:
        return None, Decimal("0.00")

    has_previous_order = CustomerOrder.objects.filter(user=user).exists()
    if has_previous_order:
        return None, Decimal("0.00")

    coupon = (
        Coupon.objects.filter(code="BEMVINDO10", is_active=True)
        .filter(
            models.Q(expiration_date__isnull=True)
            | models.Q(expiration_date__gt=timezone.now())
        )
        .first()
    )
    if not coupon:
        return None, Decimal("0.00")

    if coupon.discount_type == "FIXED_VALUE":
        # Nunca deixa o desconto ultrapassar o subtotal (total negativo).
        discount = min(coupon.discount_value, subtotal)
    else:
        discount = subtotal * (coupon.discount_value / Decimal("100"))

    return coupon, discount.quantize(Decimal("0.01"))


def get_welcome_discount(user, subtotal):
    """Retorna (coupon, discount_amount) para o desconto de boas-vindas,
    ou (None, Decimal('0.00')) se o usuário não for elegível.

    Deve ser chamada dentro de uma transaction.atomic() (o caller,
    CheckoutAPIView.post, já está decorado com @transaction.atomic).
    Faz o lock da linha do usuário via select_for_update() antes de delegar a
    checagem de elegibilidade a _compute_welcome_discount(): isso serializa
    dois checkouts concorrentes do mesmo usuário — a segunda transação só
    prossegue além do lock depois que a primeira commitar, e nesse ponto já
    enxerga o pedido criado pela primeira, evitando aplicar o desconto duas
    vezes.
    """
    if not user.is_authenticated:
        return None, Decimal("0.00")

    User = get_user_model()
    User.objects.select_for_update().get(pk=user.pk)

    return _compute_welcome_discount(user, subtotal)


def get_welcome_discount_preview(user, subtotal):
    """Versão somente-leitura de get_welcome_discount, para uso em contextos
    que não estão dentro de uma transaction.atomic() (ex.: GET /cart/, uma
    rota de leitura que apenas exibe uma prévia do desconto).

    Mesma assinatura e retorno de get_welcome_discount — (coupon, discount) ou
    (None, Decimal('0.00')) — e mesma lógica de elegibilidade (as duas delegam
    a _compute_welcome_discount), mas SEM select_for_update(): não adquire lock
    na linha do usuário, então não serializa contra checkouts concorrentes.
    Isso é aceitável aqui porque esta função só alimenta uma prévia informativa
    no carrinho; a aplicação real e segura contra corrida do desconto acontece
    em get_welcome_discount, chamada por CheckoutAPIView.post dentro de
    @transaction.atomic.
    """
    return _compute_welcome_discount(user, subtotal)
