"""Envio de newsletter: público, prévia, teste e campanha."""

import logging
import time

from django.conf import settings
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.utils import timezone

from authentication.models import NewsletterSend, NewsletterSubscriber
from notifications.email import send_email
from notifications.marketing import Recipient, normalize_email
from notifications.unsubscribe import unsubscribe_url

logger = logging.getLogger(__name__)

User = get_user_model()


def get_newsletter_audience() -> list[Recipient]:
    """Retorna os destinatários da newsletter.

    Critério: inscritos com consentimento LGPD ativo e que não cancelaram a inscrição.
    """
    subscribers = NewsletterSubscriber.objects.filter(
        consent_lgpd=True,
        unsubscribed_at__isnull=True,
    ).values_list("email", flat=True)
    normalized = sorted({normalize_email(e) for e in subscribers if e})

    names = {
        normalize_email(e): n or None
        for e, n in User.objects.filter(is_active=True).values_list("email", "name")
    }

    return [Recipient(email=e, name=names.get(e)) for e in normalized]


def _newsletter_context(
    subject: str,
    message: str,
    name: str | None,
    unsub_url: str,
) -> dict:
    return {
        "subject": subject,
        "nome": name,
        "mensagem": message,
        "loja_url": settings.FRONTEND_URL,
        "unsubscribe_url": unsub_url,
        "year": timezone.localdate().year,
    }


def render_newsletter_preview(
    subject: str,
    message: str,
    name: str | None = None,
) -> str:
    """Renderiza a pré-visualização do e-mail de newsletter."""
    context = _newsletter_context(
        subject=subject or "Newsletter — Shio",
        message=message,
        name=name or "Cliente",
        unsub_url=f"{settings.FRONTEND_URL}/descadastrar/exemplo",
    )
    return render_to_string("emails/newsletter.html", context)


def send_newsletter_test(admin, subject: str, message: str) -> bool:
    """Envia o e-mail de teste só para o próprio admin; não grava histórico."""
    return send_email(
        admin.email,
        subject,
        "newsletter",
        _newsletter_context(
            subject=subject,
            message=message,
            name=getattr(admin, "name", None),
            unsub_url=unsubscribe_url(admin.email),
        ),
    )


def send_newsletter_campaign(admin, subject: str, message: str) -> NewsletterSend:
    """Envia a newsletter: um e-mail individual por destinatário com seu próprio link de descadastro."""
    audience = get_newsletter_audience()
    sent = failed = 0
    interval = getattr(settings, "MARKETING_EMAIL_INTERVAL_SECONDS", 0.6)

    for index, recipient in enumerate(audience):
        # Respeita o limite de requisições por segundo da API de e-mail.
        if index and interval > 0:
            time.sleep(interval)

        ok = send_email(
            recipient.email,
            subject,
            "newsletter",
            _newsletter_context(
                subject=subject,
                message=message,
                name=recipient.name,
                unsub_url=unsubscribe_url(recipient.email),
            ),
        )
        if ok:
            sent += 1
        else:
            failed += 1

    return NewsletterSend.objects.create(
        sent_by=admin,
        subject=subject,
        message=message,
        sent_count=sent,
        failed_count=failed,
    )
