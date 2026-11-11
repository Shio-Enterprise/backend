"""Envio de newsletter: público, prévia, teste e campanha."""

import logging
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.template.loader import render_to_string
from django.utils import timezone

from authentication.models import (
    NewsletterSend,
    NewsletterSendStatus,
    NewsletterSubscriber,
)
from notifications.email import send_email
from notifications.marketing import Recipient, normalize_email
from notifications.unsubscribe import unsubscribe_url

logger = logging.getLogger(__name__)

TEST_SUBJECT_PREFIX = "[Teste] "


class NewsletterInProgress(Exception):
    """Já existe um envio de newsletter em andamento."""


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
        f"{TEST_SUBJECT_PREFIX}{subject}",
        "newsletter",
        _newsletter_context(
            subject=subject,
            message=message,
            name=getattr(admin, "name", None),
            unsub_url=unsubscribe_url(admin.email),
        ),
    )


def expire_stale_sends() -> int:
    """Marca como interrompido o envio "enviando" que parou de avançar.

    Os contadores avançam a cada e-mail; se nada muda há
    NEWSLETTER_STALE_MINUTES, o processo morreu (deploy, reinício) e o envio
    não volta sozinho. Sem isso o 409 bloquearia novos envios para sempre.
    """
    limit = timezone.now() - timedelta(minutes=settings.NEWSLETTER_STALE_MINUTES)
    return NewsletterSend.objects.filter(
        status=NewsletterSendStatus.SENDING, updated_at__lt=limit
    ).update(
        status=NewsletterSendStatus.INTERRUPTED,
        finished_at=timezone.now(),
        updated_at=timezone.now(),
    )


def start_newsletter_campaign(admin, subject: str, message: str) -> NewsletterSend:
    """Cria o registro com status "enviando" e dispara o envio fora da requisição.

    Levanta NewsletterInProgress se já houver outro envio em andamento.
    """
    expire_stale_sends()
    try:
        with transaction.atomic():
            record = NewsletterSend.objects.create(
                sent_by=admin,
                subject=subject,
                message=message,
                total_count=len(get_newsletter_audience()),
                status=NewsletterSendStatus.SENDING,
            )
    except IntegrityError as exc:
        raise NewsletterInProgress from exc

    _run(run_newsletter_campaign, record.pk)
    return record


def run_newsletter_campaign(send_id) -> None:
    """Envia a newsletter: um e-mail individual por destinatário com seu próprio
    link de descadastro. Atualiza os contadores a cada e-mail."""
    record = NewsletterSend.objects.get(pk=send_id)
    audience = get_newsletter_audience()
    interval = getattr(settings, "MARKETING_EMAIL_INTERVAL_SECONDS", 0.6)
    sends = NewsletterSend.objects.filter(pk=send_id)
    sends.update(total_count=len(audience), updated_at=timezone.now())

    try:
        for index, recipient in enumerate(audience):
            # Respeita o limite de requisições por segundo da API de e-mail.
            if index and interval > 0:
                time.sleep(interval)

            ok = send_email(
                recipient.email,
                record.subject,
                "newsletter",
                _newsletter_context(
                    subject=record.subject,
                    message=record.message,
                    name=recipient.name,
                    unsub_url=unsubscribe_url(recipient.email),
                ),
            )
            counter = "sent_count" if ok else "failed_count"
            sends.update(**{counter: F(counter) + 1}, updated_at=timezone.now())
    except Exception:
        sends.update(
            status=NewsletterSendStatus.INTERRUPTED,
            finished_at=timezone.now(),
            updated_at=timezone.now(),
        )
        raise

    sends.update(
        status=NewsletterSendStatus.SENT,
        finished_at=timezone.now(),
        updated_at=timezone.now(),
    )
    record.refresh_from_db()
    logger.info(
        "Newsletter %s: %d enviados, %d falhas.",
        send_id,
        record.sent_count,
        record.failed_count,
    )


def _run(func, *args):
    if settings.NEWSLETTER_RUN_INLINE:
        func(*args)
        return

    def target():
        try:
            func(*args)
        except Exception:
            logger.exception("Falha no envio da newsletter.")
        finally:
            connection.close()

    threading.Thread(target=target, daemon=True).start()
