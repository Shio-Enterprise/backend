"""E-mail de divulgação de cupom: bloqueios, condições, prévia e envio."""

import logging
import time
from decimal import Decimal

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from notifications.email import send_email
from notifications.marketing import build_audience
from notifications.unsubscribe import unsubscribe_url
from orders.coupons import count_coupon_uses
from orders.models import Coupon, CouponDiscountType, CouponEmailSend

logger = logging.getLogger(__name__)

COUPON_EMAIL_SUBJECT = "Um cupom para você — Shio"
MAX_MESSAGE_LENGTH = 500


def coupon_send_block_reason(coupon: Coupon) -> str | None:
    """Motivo pelo qual o cupom não pode ser enviado, ou None. Início futuro não bloqueia."""
    if not coupon.is_active:
        return "Cupom inativo."
    if coupon.expiration_date is not None and coupon.expiration_date <= timezone.now():
        return "Cupom vencido."
    if (
        coupon.max_uses_total is not None
        and count_coupon_uses(coupon) >= coupon.max_uses_total
    ):
        return "Cupom esgotado."
    return None


def _brl(value) -> str:
    """12345.6 -> 'R$ 12.345,60'."""
    text = f"{Decimal(value):,.2f}"
    return "R$ " + text.replace(",", "X").replace(".", ",").replace("X", ".")


def _percent(value) -> str:
    number = Decimal(value).normalize()
    text = format(number, "f")
    return text.replace(".", ",")


def _date(value) -> str:
    return timezone.localtime(value).strftime("%d/%m/%Y")


def coupon_conditions(coupon: Coupon) -> list[str]:
    lines = []
    if coupon.discount_type == CouponDiscountType.PERCENTAGE:
        line = f"{_percent(coupon.discount_value)}% de desconto"
        if coupon.max_discount_amount is not None:
            line += f" (até {_brl(coupon.max_discount_amount)})"
    else:
        line = f"{_brl(coupon.discount_value)} de desconto"
    lines.append(line)
    if coupon.min_order_value is not None:
        lines.append(f"Pedidos acima de {_brl(coupon.min_order_value)}")
    if coupon.starts_at is not None and coupon.starts_at > timezone.now():
        lines.append(f"Válido a partir de {_date(coupon.starts_at)}")
    if coupon.expiration_date is not None:
        lines.append(f"Válido até {_date(coupon.expiration_date)}")
    if coupon.first_purchase_only:
        lines.append("Só para a primeira compra")
    scope = [d.name for d in coupon.drops.order_by("name")] + [
        c.name for c in coupon.categories.order_by("name")
    ]
    if scope:
        lines.append("Válido em: " + ", ".join(scope))
    return lines


def _context(coupon, message, name, unsub_url) -> dict:
    return {
        "nome": name,
        "mensagem": message,
        "codigo": coupon.code,
        "condicoes": coupon_conditions(coupon),
        "loja_url": settings.FRONTEND_URL,
        "unsubscribe_url": unsub_url,
    }


def render_coupon_preview(coupon: Coupon, message: str, name: str | None = None) -> str:
    context = _context(
        coupon,
        message,
        name,
        f"{settings.FRONTEND_URL}/descadastrar/exemplo",
    )
    context["subject"] = COUPON_EMAIL_SUBJECT
    context["year"] = timezone.localdate().year
    return render_to_string("emails/coupon.html", context)


def send_coupon_test(coupon: Coupon, admin, message: str) -> bool:
    """Envia o e-mail só para o próprio admin; não grava histórico."""
    return send_email(
        admin.email,
        COUPON_EMAIL_SUBJECT,
        "coupon",
        _context(
            coupon, message, getattr(admin, "name", None), unsubscribe_url(admin.email)
        ),
    )


def send_coupon_campaign(coupon: Coupon, admin, message: str) -> CouponEmailSend:
    """Um e-mail por destinatário, cada um com seu link de descadastro."""
    audience = build_audience(first_purchase_only=coupon.first_purchase_only)
    sent = failed = 0
    interval = settings.MARKETING_EMAIL_INTERVAL_SECONDS
    for index, recipient in enumerate(audience.recipients):
        # Respeita o limite de requisições por segundo da API de e-mail.
        if index and interval > 0:
            time.sleep(interval)
        ok = send_email(
            recipient.email,
            COUPON_EMAIL_SUBJECT,
            "coupon",
            _context(coupon, message, recipient.name, unsubscribe_url(recipient.email)),
        )
        if ok:
            sent += 1
        else:
            failed += 1
    return CouponEmailSend.objects.create(
        coupon=coupon,
        sent_by=admin,
        message=message,
        sent_count=sent,
        failed_count=failed,
    )
