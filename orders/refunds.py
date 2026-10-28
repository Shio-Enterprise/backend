"""Reembolsos: pedido do cliente, análise e registro do estorno.

A InfinitePay não tem API de estorno: o administrador estorna no painel dela e
registra aqui. Só `complete_refund` grava `PaymentStatus.REFUNDED`.

Todo serviço trava o pedido e depois o pagamento, na mesma ordem do webhook e
de `release_if_expired`.
"""

import datetime
import logging
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from authentication.models import UserRole
from authentication.permissions import user_has_admin_permission
from notifications.email import send_email

from .metrics import METRICS_TIMEZONE
from .models import (
    CustomerOrder,
    ExchangeStatus,
    OrderStatus,
    Payment,
    PaymentStatus,
    Refund,
    RefundReason,
    RefundStatus,
)
from .money import format_brl
from .services import (
    REFUND_CANCELABLE_STATUSES,
    restore_order_stock,
    update_status,
)

logger = logging.getLogger(__name__)

# Direito de arrependimento (CDC, art. 49), contado da entrega.
WITHDRAWAL_DAYS = 7

CUSTOMER_REASONS = (
    RefundReason.CUSTOMER_REQUEST,
    RefundReason.PRODUCT_ISSUE,
    RefundReason.OTHER,
)
ACTIVE_STATUSES = (RefundStatus.REQUESTED, RefundStatus.APPROVED)

# Na ordem em que são verificados. São o contrato com o frontend.
REFUND_ERROR_MESSAGES = {
    "refund_in_progress": "Este pedido já tem um reembolso.",
    "refund_rejected": "O pedido de reembolso foi recusado. Fale com a loja.",
    "exchange_exists": "Este pedido tem uma troca. Fale com a loja.",
    "payment_not_confirmed": (
        "O reembolso fica disponível depois da confirmação do pagamento."
    ),
    "order_in_transit": "Aguarde a entrega para solicitar o reembolso.",
    "delivery_date_unknown": (
        "Não foi possível calcular o prazo de reembolso. Fale com a loja."
    ),
    "deadline_passed": "O prazo de 7 dias após a entrega terminou. Fale com a loja.",
    "no_pending_request": "Não há pedido de reembolso aguardando análise.",
}
CONFLICT_CODES = {
    "refund_in_progress",
    "refund_rejected",
    "exchange_exists",
    "no_pending_request",
}


class RefundNotAllowed(APIException):
    status_code = 400
    default_code = "refund_not_allowed"

    def __init__(self, code):
        super().__init__({"message": REFUND_ERROR_MESSAGES[code], "code": code})
        if code in CONFLICT_CODES:
            self.status_code = 409


@dataclass(frozen=True)
class RefundEligibility:
    can_request: bool
    code: str | None
    deadline: datetime.datetime | None


def delivered_at(order):
    log = (
        order.status_logs.filter(new_status=OrderStatus.DELIVERED)
        .order_by("created_at")
        .first()
    )
    return log.created_at if log else None


def withdrawal_deadline(delivered):
    """Fim do 7º dia após a data local da entrega."""
    last_day = delivered.astimezone(METRICS_TIMEZONE).date() + datetime.timedelta(
        days=WITHDRAWAL_DAYS
    )
    return datetime.datetime.combine(
        last_day, datetime.time(23, 59, 59), tzinfo=METRICS_TIMEZONE
    )


def has_exchange(order):
    """Troca em andamento ou concluída impede o reembolso integral.

    Recusadas e canceladas não contam.
    """
    return order.exchanges.exclude(
        status__in=(ExchangeStatus.REJECTED, ExchangeStatus.CANCELED)
    ).exists()


def _payment_or_none(order):
    try:
        return order.payment
    except Payment.DoesNotExist:
        return None


def refund_eligibility(order, now=None):
    """Única implementação da regra de quando o cliente pode solicitar."""
    now = now or timezone.now()
    payment = _payment_or_none(order)
    statuses = set(order.refunds.values_list("status", flat=True))

    def blocked(code, deadline=None):
        return RefundEligibility(False, code, deadline)

    if statuses - {RefundStatus.REJECTED} or (
        payment and payment.status == PaymentStatus.REFUNDED
    ):
        return blocked("refund_in_progress")
    if RefundStatus.REJECTED in statuses:
        return blocked("refund_rejected")
    if has_exchange(order):
        return blocked("exchange_exists")
    if payment is None or payment.status != PaymentStatus.PAID:
        return blocked("payment_not_confirmed")
    if order.status in (
        OrderStatus.PAID,
        OrderStatus.PREPARING,
        OrderStatus.CANCELED,
    ):
        return RefundEligibility(True, None, None)
    if order.status == OrderStatus.SHIPPED:
        return blocked("order_in_transit")
    if order.status == OrderStatus.DELIVERED:
        delivered = delivered_at(order)
        if delivered is None:
            return blocked("delivery_date_unknown")
        deadline = withdrawal_deadline(delivered)
        if now > deadline:
            return blocked("deadline_passed", deadline)
        return RefundEligibility(True, None, deadline)
    return blocked("payment_not_confirmed")


def latest_refund(order):
    return order.refunds.order_by("-created_at").first()


def with_refund_status(queryset):
    """Anota o status do reembolso mais recente, sem consulta por pedido."""
    latest = Refund.objects.filter(order=OuterRef("pk")).order_by("-created_at")
    return queryset.annotate(refund_status=Subquery(latest.values("status")[:1]))


def _lock(order):
    locked = CustomerOrder.objects.select_for_update().get(pk=order.pk)
    payment = Payment.objects.select_for_update().filter(order=locked).first()
    return locked, payment


def _create(**fields):
    try:
        with transaction.atomic():
            return Refund.objects.create(**fields)
    except IntegrityError as exc:
        raise RefundNotAllowed("refund_in_progress") from exc


def _active_refund(order, statuses=ACTIVE_STATUSES):
    return order.refunds.filter(status__in=statuses).first()


def _cancel(order, refund, admin):
    """Antes do envio, o próprio cancelamento devolve o estoque."""
    update_status(
        order,
        OrderStatus.CANCELED,
        changed_by=admin,
        comment=f"Reembolso: {refund.get_reason_display()}",
        refund_cancellation=True,
    )


def request_refund(order, customer, reason, comment=""):
    with transaction.atomic():
        order, payment = _lock(order)
        eligibility = refund_eligibility(order)
        if not eligibility.can_request:
            raise RefundNotAllowed(eligibility.code)
        if order.status == OrderStatus.CANCELED:
            reason = RefundReason.PAID_AFTER_CANCELLATION
        refund = _create(
            order=order,
            status=RefundStatus.REQUESTED,
            amount=payment.total_amount,
            reason=reason,
            customer_comment=comment,
            requested_by=customer,
        )
        transaction.on_commit(lambda: _notify_requested(refund))
    return refund


def approve_refund(order, admin, message=""):
    with transaction.atomic():
        order, _ = _lock(order)
        refund = _active_refund(order, (RefundStatus.REQUESTED,))
        if refund is None:
            raise RefundNotAllowed("no_pending_request")
        if order.status in (OrderStatus.PAID, OrderStatus.PREPARING):
            _cancel(order, refund, admin)
        refund.status = RefundStatus.APPROVED
        refund.review_message = message
        refund.reviewed_by = admin
        refund.reviewed_at = timezone.now()
        refund.save()
        transaction.on_commit(
            lambda: _notify_customer(
                refund, "refund_approved", "Seu pedido de reembolso foi aprovado"
            )
        )
    return refund


def reject_refund(order, admin, message):
    with transaction.atomic():
        order, _ = _lock(order)
        refund = _active_refund(order, (RefundStatus.REQUESTED,))
        if refund is None:
            raise RefundNotAllowed("no_pending_request")
        refund.status = RefundStatus.REJECTED
        refund.review_message = message
        refund.reviewed_by = admin
        refund.reviewed_at = timezone.now()
        refund.save()
        transaction.on_commit(
            lambda: _notify_customer(
                refund, "refund_rejected", "Seu pedido de reembolso foi recusado"
            )
        )
    return refund


def complete_refund(
    order,
    admin,
    *,
    gateway_reference,
    refunded_at,
    items_returned=False,
    internal_note="",
    reason=None,
):
    """Registra o estorno já feito na InfinitePay. Devolve (refund, criado)."""
    with transaction.atomic():
        order, payment = _lock(order)
        if payment is not None and payment.status == PaymentStatus.REFUNDED:
            raise RefundNotAllowed("refund_in_progress")
        if payment is None or payment.status != PaymentStatus.PAID:
            raise RefundNotAllowed("payment_not_confirmed")
        if has_exchange(order):
            raise RefundNotAllowed("exchange_exists")

        refund = _active_refund(order)
        created = refund is None
        errors = {}
        if created:
            if not reason:
                errors["reason"] = "Informe o motivo do reembolso."
            elif (
                reason == RefundReason.PAID_AFTER_CANCELLATION
                and order.status != OrderStatus.CANCELED
            ):
                errors["reason"] = "Motivo válido apenas para pedido cancelado."
            elif reason == RefundReason.OTHER and not internal_note.strip():
                errors["internal_note"] = "Descreva o motivo na observação interna."
        now = timezone.now()
        if refunded_at > now:
            errors["refunded_at"] = "A data do estorno não pode estar no futuro."
        elif payment.paid_at and refunded_at < payment.paid_at:
            errors["refunded_at"] = (
                "A data do estorno não pode ser anterior ao pagamento."
            )
        if order.status == OrderStatus.SHIPPED and not items_returned:
            errors["items_returned"] = "Confirme que o pacote voltou para a loja."
        if errors:
            raise ValidationError(errors)

        if created:
            refund = _create(
                order=order,
                status=RefundStatus.APPROVED,
                amount=payment.total_amount,
                reason=reason,
                requested_by=admin,
                reviewed_by=admin,
                reviewed_at=now,
            )
        elif refund.status == RefundStatus.REQUESTED:
            # Registrar direto um pedido em análise equivale a aprová-lo.
            refund.reviewed_by = admin
            refund.reviewed_at = now

        status_before = order.status
        if status_before in REFUND_CANCELABLE_STATUSES:
            _cancel(order, refund, admin)
        if items_returned and status_before in (
            OrderStatus.SHIPPED,
            OrderStatus.DELIVERED,
        ):
            restore_order_stock(order, changed_by=admin, physical_return=True)

        payment.status = PaymentStatus.REFUNDED
        payment.save(update_fields=["status", "updated_at"])

        refund.status = RefundStatus.COMPLETED
        refund.gateway_reference = gateway_reference
        refund.refunded_at = refunded_at
        if internal_note:
            refund.internal_note = internal_note
        refund.completed_by = admin
        refund.completed_at = now
        refund.save()
        transaction.on_commit(
            lambda: _notify_customer(
                refund, "refund_completed", "Seu reembolso foi registrado"
            )
        )
    return refund, created


def stock_returned(order):
    from products.models import StockMovement

    return StockMovement.objects.filter(
        order_item__order=order, reason="DEVOLUCAO"
    ).exists()


def _context(refund):
    order = refund.order
    return {
        "nome": order.user.name,
        "pedido": str(order.id),
        "valor": format_brl(refund.amount),
        "refund": refund,
    }


def _notify_customer(refund, template, subject):
    order = refund.order
    send_email(
        order.user.email,
        subject,
        template,
        {
            **_context(refund),
            "pedido_url": f"{settings.FRONTEND_URL}/my-orders/{order.id}",
        },
    )


def order_managers():
    """E-mails das contas que passam em CanManageOrders."""
    candidates = (
        get_user_model()
        .objects.filter(is_active=True)
        .filter(
            Q(is_staff=True) | Q(is_superuser=True) | Q(profile__role=UserRole.ADMIN)
        )
        .distinct()
    )
    return [
        user.email
        for user in candidates
        if user.email and user_has_admin_permission(user, "manage_orders")
    ]


def _notify_requested(refund):
    _notify_customer(refund, "refund_requested", "Recebemos seu pedido de reembolso")
    managers = order_managers()
    if not managers:
        logger.warning("Pedido de reembolso sem administrador para avisar.")
        return
    # Um envio por pessoa: o endereço de um administrador não vai para outro.
    for email in managers:
        send_email(
            email,
            "Novo pedido de reembolso",
            "refund_requested_admin",
            {
                **_context(refund),
                "motivo": refund.get_reason_display(),
                "comentario": refund.customer_comment,
                "pedido_url": (
                    f"{settings.FRONTEND_URL}/admin/orders/{refund.order_id}"
                ),
            },
        )
