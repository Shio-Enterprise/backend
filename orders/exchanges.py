"""Trocas: pedido do cliente, análise, recebimento da peça e envio da reposição.

A troca é sempre por outra variação do mesmo produto, que tem o mesmo preço:
não há cobrança nem estorno. O estoque é movimentado só por aqui:

- aprovação: saída TROCA da reposição (reserva);
- recebimento: entrada DEVOLUCAO da peça devolvida e, se avariada, saída PERDA;
- cancelamento de troca aprovada: entrada TROCA que compensa a reserva.

Todo serviço trava o pedido e depois o pagamento, na mesma ordem dos
reembolsos, do webhook e de `release_if_expired`.
"""

import datetime
import logging
from collections import defaultdict
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import OuterRef, Subquery, Sum
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from notifications.email import send_email
from products.models import ProductVariation, StockMovement
from products.services import move_stock

from .metrics import METRICS_TIMEZONE
from .models import (
    EXCHANGE_OPEN_STATUSES,
    CustomerOrder,
    ExchangeItem,
    ExchangeItemCondition,
    ExchangeReason,
    ExchangeRequest,
    ExchangeStatus,
    OrderStatus,
    Payment,
    PaymentStatus,
    RefundStatus,
)
from .refunds import delivered_at, order_managers

logger = logging.getLogger(__name__)

# Política da loja para tamanho/cor; defeito segue o CDC (art. 26, II).
SIZE_OR_COLOR_DAYS = 30
DEFECT_DAYS = 90

# Status que contam como "itens já trocados" (os que seguram quantidade).
HOLDING_STATUSES = (*EXCHANGE_OPEN_STATUSES, ExchangeStatus.COMPLETED)

# Na ordem em que são verificados. São o contrato com o frontend.
EXCHANGE_ERROR_MESSAGES = {
    "exchange_in_progress": "Este pedido já tem uma troca em andamento.",
    "exchange_rejected": "O pedido de troca foi recusado. Fale com a loja.",
    "refund_exists": "Este pedido tem um reembolso. Fale com a loja.",
    "payment_not_confirmed": "A troca fica disponível depois do pagamento.",
    "order_not_delivered": "A troca fica disponível depois da entrega.",
    "delivery_date_unknown": (
        "Não foi possível calcular o prazo de troca. Fale com a loja."
    ),
    "deadline_passed": "O prazo de troca terminou. Fale com a loja.",
    "nothing_to_exchange": "Todos os itens deste pedido já foram trocados.",
    "size_deadline_passed": (
        "O prazo de 30 dias para troca de tamanho ou cor terminou. "
        "Para defeito, o prazo é de 90 dias."
    ),
    "replacement_out_of_stock": (
        "A variação escolhida para a troca não tem estoque suficiente."
    ),
    "no_pending_request": "Não há pedido de troca aguardando análise.",
    "not_approved": "A troca precisa estar aprovada.",
    "not_received": "Confirme o recebimento da peça antes do envio.",
    "not_cancelable": "Esta troca não pode mais ser cancelada.",
}
CONFLICT_CODES = {
    "exchange_in_progress",
    "exchange_rejected",
    "refund_exists",
    "no_pending_request",
    "not_approved",
    "not_received",
    "not_cancelable",
}


class ExchangeNotAllowed(APIException):
    status_code = 400
    default_code = "exchange_not_allowed"

    def __init__(self, code, extra=None):
        super().__init__(
            {"message": EXCHANGE_ERROR_MESSAGES[code], "code": code, **(extra or {})}
        )
        if code in CONFLICT_CODES:
            self.status_code = 409


@dataclass(frozen=True)
class ExchangeEligibility:
    can_request: bool
    code: str | None
    size_or_color_deadline: datetime.datetime | None
    defect_deadline: datetime.datetime | None


def deadline_after(delivered, days):
    """Fim do N-ésimo dia após a data local da entrega."""
    last_day = delivered.astimezone(METRICS_TIMEZONE).date() + datetime.timedelta(
        days=days
    )
    return datetime.datetime.combine(
        last_day, datetime.time(23, 59, 59), tzinfo=METRICS_TIMEZONE
    )


def _payment_or_none(order):
    try:
        return order.payment
    except Payment.DoesNotExist:
        return None


def exchanged_quantities(order):
    """Quantidade de cada item já comprometida em trocas."""
    rows = (
        ExchangeItem.objects.filter(
            order_item__order=order, exchange__status__in=HOLDING_STATUSES
        )
        .values("order_item_id")
        .annotate(total=Sum("quantity"))
    )
    return {row["order_item_id"]: row["total"] for row in rows}


def available_quantities(order):
    """Quanto de cada item ainda pode ser trocado."""
    used = exchanged_quantities(order)
    return {
        item.pk: max(item.quantity - used.get(item.pk, 0), 0)
        for item in order.items.all()
        if item.variation_id is not None
    }


def exchange_eligibility(order, now=None):
    """Única implementação da regra de quando o cliente pode solicitar."""
    now = now or timezone.now()
    payment = _payment_or_none(order)
    statuses = set(order.exchanges.values_list("status", flat=True))

    def blocked(code, size=None, defect=None):
        return ExchangeEligibility(False, code, size, defect)

    if statuses & set(EXCHANGE_OPEN_STATUSES):
        return blocked("exchange_in_progress")
    if ExchangeStatus.REJECTED in statuses:
        return blocked("exchange_rejected")
    if order.refunds.exclude(status=RefundStatus.REJECTED).exists() or (
        payment and payment.status == PaymentStatus.REFUNDED
    ):
        return blocked("refund_exists")
    if payment is None or payment.status != PaymentStatus.PAID:
        return blocked("payment_not_confirmed")
    if order.status != OrderStatus.DELIVERED:
        return blocked("order_not_delivered")
    delivered = delivered_at(order)
    if delivered is None:
        return blocked("delivery_date_unknown")
    size = deadline_after(delivered, SIZE_OR_COLOR_DAYS)
    defect = deadline_after(delivered, DEFECT_DAYS)
    if now > defect:
        return blocked("deadline_passed", size, defect)
    if not any(available_quantities(order).values()):
        return blocked("nothing_to_exchange", size, defect)
    return ExchangeEligibility(True, None, size, defect)


def exchange_options(order):
    """Para cada item ainda trocável, as outras variações do mesmo produto."""
    available = available_quantities(order)
    items = [
        item
        for item in order.items.select_related("variation")
        if available.get(item.pk)
    ]
    product_ids = {item.variation.product_id for item in items}
    siblings = defaultdict(list)
    for variation in ProductVariation.objects.filter(
        product_id__in=product_ids, product__is_active=True
    ).order_by("size", "color"):
        siblings[variation.product_id].append(variation)
    return [
        {
            "order_item_id": item.pk,
            "available_quantity": available[item.pk],
            "options": [
                {
                    "variation_id": v.pk,
                    "size": v.size,
                    "color": v.color,
                    "in_stock": v.stock_quantity > 0,
                }
                for v in siblings[item.variation.product_id]
                if v.pk != item.variation_id
            ],
        }
        for item in items
    ]


def with_exchange_status(queryset):
    """Anota o status da troca mais recente, sem consulta por pedido."""
    latest = ExchangeRequest.objects.filter(order=OuterRef("pk")).order_by(
        "-created_at"
    )
    return queryset.annotate(exchange_status=Subquery(latest.values("status")[:1]))


def _lock(order):
    locked = CustomerOrder.objects.select_for_update().get(pk=order.pk)
    payment = Payment.objects.select_for_update().filter(order=locked).first()
    return locked, payment


def _open_exchange(order, statuses):
    return (
        order.exchanges.filter(status__in=statuses)
        .prefetch_related("items__order_item", "items__replacement_variation")
        .first()
    )


def _locked_variations(ids):
    """Trava as variações sempre na mesma ordem, como `move_stock`."""
    return {
        v.pk: v
        for v in ProductVariation.objects.select_for_update()
        .filter(pk__in=ids)
        .order_by("pk")
    }


def _validate_items(order, items):
    """Confere itens, quantidades e variações. Devolve as linhas normalizadas."""
    if not items:
        raise ValidationError({"items": "Escolha ao menos um item para trocar."})
    order_items = {
        item.pk: item for item in order.items.select_related("variation__product")
    }
    available = available_quantities(order)
    seen = set()
    errors = {}
    rows = []
    for index, row in enumerate(items):
        item = order_items.get(row["order_item_id"])
        key = f"items[{index}]"
        if item is None or item.variation_id is None:
            errors[key] = "Item não pertence a este pedido."
            continue
        if item.pk in seen:
            errors[key] = "Cada item só pode aparecer uma vez."
            continue
        seen.add(item.pk)
        if row["quantity"] > available.get(item.pk, 0):
            errors[key] = (
                f"Quantidade acima da disponível para troca "
                f"({available.get(item.pk, 0)})."
            )
            continue
        replacement = (
            ProductVariation.objects.select_related("product")
            .filter(pk=row["replacement_variation_id"])
            .first()
        )
        if (
            replacement is None
            or replacement.product_id != item.variation.product_id
            or not replacement.product.is_active
        ):
            errors[key] = "Escolha outra variação do mesmo produto."
            continue
        if replacement.pk == item.variation_id:
            errors[key] = "Escolha uma variação diferente da comprada."
            continue
        rows.append((item, replacement, row["quantity"]))
    if errors:
        raise ValidationError(errors)
    return rows


def _check_stock(rows, variations):
    """Recusa a troca se alguma reposição não cobre a soma pedida."""
    needed = defaultdict(int)
    for _, replacement, quantity in rows:
        needed[replacement.pk] += quantity
    short = [
        str(pk) for pk, qty in needed.items() if variations[pk].stock_quantity < qty
    ]
    if short:
        raise ExchangeNotAllowed(
            "replacement_out_of_stock", {"variation_ids": sorted(short)}
        )


def _create(order, **fields):
    try:
        with transaction.atomic():
            return ExchangeRequest.objects.create(order=order, **fields)
    except IntegrityError as exc:
        raise ExchangeNotAllowed("exchange_in_progress") from exc


def request_exchange(order, customer, reason, items, comment=""):
    """Cria a troca em análise. `items`: order_item_id, replacement_variation_id e
    quantity."""
    with transaction.atomic():
        order, _ = _lock(order)
        eligibility = exchange_eligibility(order)
        if not eligibility.can_request:
            raise ExchangeNotAllowed(eligibility.code)
        if (
            reason == ExchangeReason.SIZE_OR_COLOR
            and timezone.now() > eligibility.size_or_color_deadline
        ):
            raise ExchangeNotAllowed("size_deadline_passed")
        rows = _validate_items(order, items)
        variations = _locked_variations({r.pk for _, r, _ in rows})
        _check_stock(rows, variations)
        exchange = _create(
            order,
            status=ExchangeStatus.REQUESTED,
            reason=reason,
            customer_comment=comment,
            requested_by=customer,
        )
        ExchangeItem.objects.bulk_create(
            ExchangeItem(
                exchange=exchange,
                order_item=item,
                replacement_variation=replacement,
                quantity=quantity,
            )
            for item, replacement, quantity in rows
        )
        transaction.on_commit(lambda: _notify_requested(exchange))
    return exchange


def _sorted_items(exchange):
    return sorted(exchange.items.all(), key=lambda i: str(i.replacement_variation_id))


def _movement(item, step, **fields):
    return move_stock(
        origin_type="EXCHANGE",
        origin_id=item.pk,
        idempotency_key=f"exchange:{item.pk}:{step}",
        **fields,
    )


def _reservation(item):
    return StockMovement.objects.filter(
        idempotency_key=f"exchange:{item.pk}:reserve"
    ).first()


def approve_exchange(order, admin, message=""):
    """Aprova e reserva a reposição; sem estoque, nada é gravado."""
    with transaction.atomic():
        order, _ = _lock(order)
        exchange = _open_exchange(order, (ExchangeStatus.REQUESTED,))
        if exchange is None:
            raise ExchangeNotAllowed("no_pending_request")
        items = _sorted_items(exchange)
        variations = _locked_variations({i.replacement_variation_id for i in items})
        _check_stock(
            [(i.order_item, i.replacement_variation, i.quantity) for i in items],
            variations,
        )
        for item in items:
            _movement(
                item,
                "reserve",
                variation=item.replacement_variation,
                kind="SAIDA",
                reason="TROCA",
                quantity=item.quantity,
                created_by=admin,
                note="Reserva da reposição da troca",
            )
        exchange.status = ExchangeStatus.APPROVED
        exchange.review_message = message
        exchange.reviewed_by = admin
        exchange.reviewed_at = timezone.now()
        exchange.save()
        transaction.on_commit(
            lambda: _notify_customer(
                exchange, "exchange_approved", "Sua troca foi aprovada"
            )
        )
    return exchange


def reject_exchange(order, admin, message):
    with transaction.atomic():
        order, _ = _lock(order)
        exchange = _open_exchange(order, (ExchangeStatus.REQUESTED,))
        if exchange is None:
            raise ExchangeNotAllowed("no_pending_request")
        exchange.status = ExchangeStatus.REJECTED
        exchange.review_message = message
        exchange.reviewed_by = admin
        exchange.reviewed_at = timezone.now()
        exchange.save()
        transaction.on_commit(
            lambda: _notify_customer(
                exchange, "exchange_rejected", "Sua troca foi recusada"
            )
        )
    return exchange


def receive_exchange(order, admin, conditions, internal_note=""):
    """Confirma a chegada das peças. `conditions`: {exchange_item_id: condição}."""
    with transaction.atomic():
        order, _ = _lock(order)
        exchange = _open_exchange(order, (ExchangeStatus.APPROVED,))
        if exchange is None:
            raise ExchangeNotAllowed("not_approved")
        items = sorted(exchange.items.all(), key=lambda i: str(i.order_item_id))
        missing = [str(i.pk) for i in items if i.pk not in conditions]
        unknown = [str(k) for k in conditions if k not in {i.pk for i in items}]
        if missing or unknown:
            raise ValidationError(
                {"items": "Informe a condição de cada item da troca, e só deles."}
            )
        for item in items:
            item.condition = conditions[item.pk]
            returned = dict(
                variation=item.order_item.variation,
                order_item=item.order_item,
                quantity=item.quantity,
                created_by=admin,
            )
            # A peça sempre entra; a avariada sai em seguida como perda,
            # para o histórico mostrar que ela voltou.
            _movement(
                item,
                "return",
                kind="ENTRADA",
                reason="DEVOLUCAO",
                note="Peça devolvida na troca",
                **returned,
            )
            if item.condition == ExchangeItemCondition.LOSS:
                _movement(
                    item,
                    "loss",
                    kind="SAIDA",
                    reason="PERDA",
                    note="Peça devolvida na troca com avaria",
                    **returned,
                )
            item.save(update_fields=["condition"])
        exchange.status = ExchangeStatus.RECEIVED
        if internal_note:
            exchange.internal_note = internal_note
        exchange.received_by = admin
        exchange.received_at = timezone.now()
        exchange.save()
        transaction.on_commit(
            lambda: _notify_customer(
                exchange, "exchange_received", "Recebemos a peça da sua troca"
            )
        )
    return exchange


def complete_exchange(order, admin, tracking_code):
    """Registra o envio da reposição e encerra a troca."""
    with transaction.atomic():
        order, _ = _lock(order)
        exchange = _open_exchange(order, (ExchangeStatus.RECEIVED,))
        if exchange is None:
            raise ExchangeNotAllowed("not_received")
        exchange.status = ExchangeStatus.COMPLETED
        exchange.tracking_code = tracking_code
        exchange.completed_by = admin
        exchange.completed_at = timezone.now()
        exchange.save()
        transaction.on_commit(
            lambda: _notify_customer(
                exchange, "exchange_completed", "A nova peça da sua troca foi enviada"
            )
        )
    return exchange


def cancel_exchange(order, admin, message):
    """Cancela antes do recebimento; a reserva da reposição volta ao estoque."""
    with transaction.atomic():
        order, _ = _lock(order)
        exchange = _open_exchange(order, EXCHANGE_OPEN_STATUSES)
        if exchange is None or exchange.status == ExchangeStatus.RECEIVED:
            raise ExchangeNotAllowed("not_cancelable")
        if exchange.status == ExchangeStatus.APPROVED:
            for item in _sorted_items(exchange):
                reservation = _reservation(item)
                if reservation is None:
                    continue
                _movement(
                    item,
                    "release",
                    variation=item.replacement_variation,
                    kind="ENTRADA",
                    reason="TROCA",
                    quantity=item.quantity,
                    created_by=admin,
                    note="Troca cancelada: reserva devolvida",
                    reverses_movement=reservation,
                )
        exchange.status = ExchangeStatus.CANCELED
        exchange.review_message = message
        exchange.canceled_by = admin
        exchange.canceled_at = timezone.now()
        exchange.save()
        transaction.on_commit(
            lambda: _notify_customer(
                exchange, "exchange_canceled", "Sua troca foi cancelada"
            )
        )
    return exchange


def _variation_label(variation):
    if variation is None:
        return "—"
    return (
        f"{variation.size} / {variation.color}" if variation.color else variation.size
    )


def _context(exchange):
    order = exchange.order
    return {
        "nome": order.user.name,
        "pedido": str(order.id),
        "exchange": exchange,
        "motivo": exchange.get_reason_display(),
        "itens": [
            {
                "produto": item.order_item.product_name,
                "quantidade": item.quantity,
                "de": _variation_label(item.order_item.variation),
                "para": _variation_label(item.replacement_variation),
            }
            for item in exchange.items.select_related(
                "order_item__variation", "replacement_variation"
            )
        ],
    }


def _notify_customer(exchange, template, subject):
    order = exchange.order
    send_email(
        order.user.email,
        subject,
        template,
        {
            **_context(exchange),
            "pedido_url": f"{settings.FRONTEND_URL}/my-orders/{order.id}",
        },
    )


def _notify_requested(exchange):
    _notify_customer(exchange, "exchange_requested", "Recebemos seu pedido de troca")
    managers = order_managers()
    if not managers:
        logger.warning("Pedido de troca sem administrador para avisar.")
        return
    # Um envio por pessoa: o endereço de um administrador não vai para outro.
    for email in managers:
        send_email(
            email,
            "Novo pedido de troca",
            "exchange_requested_admin",
            {
                **_context(exchange),
                "comentario": exchange.customer_comment,
                "pedido_url": (
                    f"{settings.FRONTEND_URL}/admin/orders/{exchange.order_id}"
                ),
            },
        )
