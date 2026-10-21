"""Envio dos avisos "Avise-me quando chegar"."""

import logging
import threading
import time
from contextlib import contextmanager

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from notifications.email import send_email

from .availability import is_product_sellable
from .models import Product, ProductVariation, StockAlert

logger = logging.getLogger(__name__)


def send_stock_alerts(variation_id) -> int:
    """Envia o aviso aos pendentes da variação; devolve quantos foram enviados."""
    variation = (
        ProductVariation.objects.select_related("product", "product__drop")
        .filter(pk=variation_id)
        .first()
    )
    if (
        variation is None
        or variation.stock_quantity <= 0
        or not is_product_sellable(variation.product)
    ):
        return 0

    product = variation.product
    size = f"{variation.size} / {variation.color}" if variation.color else variation.size
    product_url = f"{settings.FRONTEND_URL}/product/{product.id}"
    subject = f"Chegou! {product.name} — Shio"

    pending = StockAlert.objects.filter(
        variation=variation, notified_at__isnull=True, user__is_active=True
    ).select_related("user")

    sent = 0
    failed = 0
    for alert in pending:
        # Reivindica antes de enviar: execuções concorrentes não duplicam.
        claimed = StockAlert.objects.filter(
            pk=alert.pk, notified_at__isnull=True
        ).update(notified_at=timezone.now())
        if not claimed:
            continue
        if sent or failed:
            time.sleep(settings.MARKETING_EMAIL_INTERVAL_SECONDS)
        ok = send_email(
            alert.user.email,
            subject,
            "stock_alert",
            {
                "nome": alert.user.name,
                "produto": product.name,
                "tamanho": size,
                "product_url": product_url,
            },
        )
        if ok:
            sent += 1
        else:
            failed += 1
            StockAlert.objects.filter(pk=alert.pk).update(notified_at=None)

    logger.info(
        "Avisos de reposição da variação %s: %d enviados, %d falhas.",
        variation_id,
        sent,
        failed,
    )
    return sent


def _run(func, *args):
    if settings.STOCK_ALERTS_RUN_INLINE:
        func(*args)
        return

    def target():
        try:
            func(*args)
        except Exception:
            logger.exception("Falha ao enviar avisos de reposição.")
        finally:
            connection.close()

    threading.Thread(target=target, daemon=True).start()


def schedule_variation_alerts(variation_id) -> None:
    transaction.on_commit(lambda: _run(send_stock_alerts, variation_id))


def schedule_product_alerts(product) -> None:
    variation_ids = ProductVariation.objects.filter(
        product=product,
        stock_quantity__gt=0,
        stock_alerts__notified_at__isnull=True,
    ).values_list("pk", flat=True).distinct()
    for variation_id in list(variation_ids):
        schedule_variation_alerts(variation_id)


@contextmanager
def watch_sellable(products):
    """Avisa os pendentes dos produtos que passarem a ser vendáveis no bloco."""
    not_sellable = [p.pk for p in products if not is_product_sellable(p)]
    yield
    if not not_sellable:
        return
    for product in Product.objects.select_related("drop").filter(pk__in=not_sellable):
        if is_product_sellable(product):
            schedule_product_alerts(product)
