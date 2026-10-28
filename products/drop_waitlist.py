"""E-mail de abertura do drop para a lista de espera.

O projeto não tem fila de tarefas: o envio roda numa varredura dentro das
próprias requisições (sweep_drop_openings) e no comando notify_drop_openings.
"""

import logging
import threading
import time

from django.conf import settings
from django.db import connection
from django.db.models import Q
from django.utils import timezone

from notifications.email import send_email

from .availability import is_drop_open_for_sale
from .models import DropCampaign, DropWaitlist

logger = logging.getLogger(__name__)

_sweep_lock = threading.Lock()
_last_sweep = None


def open_drops_with_pending_waitlist():
    """Drops abertos para venda com alguém na lista ainda não avisado."""
    now = timezone.now()
    return (
        DropCampaign.objects.filter(
            is_public=True,
            is_active=True,
            waitlist_entries__notified_at__isnull=True,
        )
        .filter(Q(launch_date__isnull=True) | Q(launch_date__lte=now))
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=now))
        .distinct()
    )


def send_drop_waitlist_emails(drop_id) -> int:
    """Envia o e-mail de abertura aos pendentes do drop; devolve quantos foram enviados."""
    drop = DropCampaign.objects.filter(pk=drop_id).first()
    if drop is None or not is_drop_open_for_sale(drop):
        return 0

    drop_url = f"{settings.FRONTEND_URL}/drop/{drop.slug}"
    subject = f"O drop {drop.name} abriu! — Shio"

    pending = DropWaitlist.objects.filter(
        drop=drop, notified_at__isnull=True, user__is_active=True
    ).select_related("user")

    sent = 0
    failed = 0
    for entry in pending:
        # Reivindica antes de enviar: execuções concorrentes não duplicam.
        claimed = DropWaitlist.objects.filter(
            pk=entry.pk, notified_at__isnull=True
        ).update(notified_at=timezone.now())
        if not claimed:
            continue
        if sent or failed:
            time.sleep(settings.MARKETING_EMAIL_INTERVAL_SECONDS)
        ok = send_email(
            entry.user.email,
            subject,
            "drop_waitlist",
            {"nome": entry.user.name, "drop": drop.name, "drop_url": drop_url},
        )
        if ok:
            sent += 1
        else:
            failed += 1
            # Volta para a fila: a próxima varredura tenta de novo.
            DropWaitlist.objects.filter(pk=entry.pk).update(notified_at=None)

    logger.info(
        "Lista de espera do drop %s: %d enviados, %d falhas.", drop_id, sent, failed
    )
    return sent


def notify_open_drops() -> int:
    """Envia o e-mail de todos os drops abertos com lista pendente."""
    drop_ids = list(open_drops_with_pending_waitlist().values_list("pk", flat=True))
    return sum(send_drop_waitlist_emails(drop_id) for drop_id in drop_ids)


def _run(func, *args):
    if settings.DROP_WAITLIST_RUN_INLINE:
        func(*args)
        return

    def target():
        try:
            func(*args)
        except Exception:
            logger.exception("Falha ao enviar o e-mail da lista de espera.")
        finally:
            connection.close()

    threading.Thread(target=target, daemon=True).start()


def sweep_drop_openings():
    """Varredura no máximo uma vez por intervalo em cada processo; nunca quebra
    a requisição. O envio roda numa thread, fora da requisição."""
    global _last_sweep
    if not _sweep_lock.acquire(blocking=False):
        return
    try:
        now = time.monotonic()
        interval = settings.DROP_WAITLIST_SWEEP_INTERVAL_SECONDS
        if _last_sweep is not None and now - _last_sweep < interval:
            return
        _last_sweep = now
        _run(notify_open_drops)
    except Exception:
        logger.exception("Falha ao enviar o e-mail da lista de espera.")
    finally:
        _sweep_lock.release()
