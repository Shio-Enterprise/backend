"""E-mail de recuperação para clientes com pedido aguardando pagamento ou cancelado."""

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from authentication.models import NewsletterSubscriber
from notifications.email import send_email
from notifications.marketing import normalize_email
from notifications.unsubscribe import unsubscribe_url
from orders.models import CustomerOrder, OrderRecoveryEmailSend, OrderStatus
from orders.money import format_brl

RECOVERY_ALLOWED_STATUSES = frozenset(
    {OrderStatus.AWAITING_PAYMENT, OrderStatus.CANCELED}
)
MAX_MESSAGE_LENGTH = 500

SUGGESTED_MESSAGES = {
    OrderStatus.AWAITING_PAYMENT: (
        "Notamos que o pagamento do seu pedido ainda está pendente. "
        "Se precisar de ajuda para concluir a compra, estamos à disposição!"
    ),
    OrderStatus.CANCELED: (
        "Vimos que o seu pedido foi cancelado. "
        "Os itens que você escolheu continuam esperando por você na nossa loja!"
    ),
}


def short_order_code(order: CustomerOrder) -> str:
    return f"#{str(order.id)[:8].upper()}"


def order_email_subject(order: CustomerOrder) -> str:
    code = short_order_code(order)
    if order.status == OrderStatus.AWAITING_PAYMENT:
        return f"Seu pedido {code} aguarda pagamento — Shio"
    return f"Itens do seu pedido {code} na Shio"


def suggested_order_message(order: CustomerOrder) -> str:
    return SUGGESTED_MESSAGES.get(order.status, "")


def is_customer_unsubscribed(email: str | None) -> bool:
    if not email:
        return False
    normalized = normalize_email(email)
    return NewsletterSubscriber.objects.filter(
        email__iexact=normalized,
        unsubscribed_at__isnull=False,
    ).exists()


def order_send_block_reason(order: CustomerOrder) -> str | None:
    """Motivo de bloqueio para envio de recuperação do pedido, ou None."""
    if order.status not in RECOVERY_ALLOWED_STATUSES:
        return (
            "Só é possível enviar e-mail para pedidos aguardando pagamento "
            "ou cancelados."
        )
    customer_email = getattr(order.user, "email", None)
    if not customer_email:
        return "Cliente sem e-mail cadastrado."
    if is_customer_unsubscribed(customer_email):
        return "O cliente pediu para não receber e-mails."
    return None


def _intro_text(order: CustomerOrder) -> str:
    code = short_order_code(order)
    if order.status == OrderStatus.AWAITING_PAYMENT:
        return f"Seu pedido {code} está aguardando pagamento na Shio."
    if order.status == OrderStatus.CANCELED:
        return f"Notamos que o seu pedido {code} foi cancelado na Shio."
    return f"Sobre o seu pedido {code} na Shio."


def _order_items_context(order: CustomerOrder) -> list[dict]:
    items = []
    for item in order.items.all():
        subtotal = item.unit_price * item.quantity
        items.append(
            {
                "product_name": item.product_name,
                "sku_snapshot": item.sku_snapshot,
                "quantity": item.quantity,
                "unit_price_brl": format_brl(item.unit_price),
                "subtotal_brl": format_brl(subtotal),
            }
        )
    return items


def _context(
    order: CustomerOrder,
    message: str,
    name: str | None,
    unsub_url: str,
) -> dict:
    frontend_url = settings.FRONTEND_URL.rstrip("/")
    pedido_url = (
        f"{frontend_url}/my-orders/{order.id}"
        if order.status == OrderStatus.AWAITING_PAYMENT
        else None
    )
    return {
        "nome": name,
        "codigo_pedido": short_order_code(order),
        "intro_text": _intro_text(order),
        "mensagem": message,
        "itens": _order_items_context(order),
        "total_brl": format_brl(order.total_amount),
        "loja_url": frontend_url,
        "pedido_url": pedido_url,
        "unsubscribe_url": unsub_url,
    }


def render_order_email_preview(order: CustomerOrder, message: str) -> str:
    """Renderiza a pré-visualização HTML do e-mail de recuperação do pedido."""
    customer_name = getattr(order.user, "name", None) or None
    context = _context(
        order=order,
        message=message,
        name=customer_name,
        unsub_url=f"{settings.FRONTEND_URL.rstrip('/')}/descadastrar/exemplo",
    )
    context["subject"] = order_email_subject(order)
    context["year"] = timezone.localdate().year
    return render_to_string("emails/order_recovery.html", context)


def send_order_email_test(order: CustomerOrder, admin, message: str) -> bool:
    """Envia prévia de teste apenas para o e-mail do próprio admin; sem gravar histórico."""
    return send_email(
        admin.email,
        f"[Teste] {order_email_subject(order)}",
        "order_recovery",
        _context(
            order=order,
            message=message,
            name=getattr(admin, "name", None) or None,
            unsub_url=unsubscribe_url(admin.email),
        ),
    )


def send_order_recovery_email(
    order: CustomerOrder, admin, message: str
) -> OrderRecoveryEmailSend | None:
    """Envia o e-mail para o cliente do pedido e registra o envio se bem-sucedido."""
    customer_email = order.user.email
    customer_name = getattr(order.user, "name", None) or None
    ok = send_email(
        customer_email,
        order_email_subject(order),
        "order_recovery",
        _context(
            order=order,
            message=message,
            name=customer_name,
            unsub_url=unsubscribe_url(customer_email),
        ),
    )
    if not ok:
        return None
    return OrderRecoveryEmailSend.objects.create(
        order=order,
        sent_by=admin,
        message=message,
    )
