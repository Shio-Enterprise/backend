"""Liberação automática de reservas de estoque vencidas.

O projeto não tem fila de tarefas: a liberação roda dentro das próprias
requisições e no comando expire_stale_orders.
"""

import logging
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from orders.models import CartItem, CustomerOrder, OrderStatus
from orders.services import release_if_expired

logger = logging.getLogger(__name__)

_sweep_lock = threading.Lock()
_last_sweep = None


def expired_reservations(*, variation_ids=None):
    """Pedidos com reserva vencida há mais que a tolerância."""
    cutoff = timezone.now() - timedelta(
        seconds=settings.STOCK_RESERVATION_GRACE_SECONDS
    )
    orders = CustomerOrder.objects.filter(
        status=OrderStatus.AWAITING_PAYMENT, reservation_expires_at__lt=cutoff
    )
    if variation_ids is not None:
        orders = orders.filter(items__variation_id__in=variation_ids).distinct()
    return orders.order_by("reservation_expires_at")


def release_expired_reservations(*, variation_ids=None, batch_size=100):
    """Libera reservas vencidas e retorna quantas liberou.

    Com `variation_ids`, só os pedidos que seguram essas variações. Cada
    pedido é liberado na própria transação: uma falha vai para o log e não
    impede os demais.
    """
    order_ids = list(
        expired_reservations(variation_ids=variation_ids).values_list("pk", flat=True)[
            :batch_size
        ]
    )
    released = 0
    for order_id in order_ids:
        try:
            if release_if_expired(CustomerOrder.objects.get(pk=order_id)):
                released += 1
        except Exception:
            logger.exception("Falha ao liberar a reserva do pedido %s.", order_id)
    return released


def release_quietly(*, variation_ids=None, batch_size=100):
    """Como release_expired_reservations, mas nunca quebra a requisição."""
    try:
        return release_expired_reservations(
            variation_ids=variation_ids, batch_size=batch_size
        )
    except Exception:
        logger.exception("Falha ao liberar reservas vencidas.")
        return 0


def sweep_expired_reservations():
    """Varredura em lote, no máximo uma por intervalo em cada processo."""
    global _last_sweep
    if not _sweep_lock.acquire(blocking=False):
        return 0
    try:
        now = time.monotonic()
        interval = settings.RESERVATION_SWEEP_INTERVAL_SECONDS
        if _last_sweep is not None and now - _last_sweep < interval:
            return 0
        _last_sweep = now
        return release_quietly()
    finally:
        _sweep_lock.release()


def cart_variation_ids(request):
    """Variações do carrinho de quem fez a requisição (logado ou não)."""
    if request.user.is_authenticated:
        return list(
            CartItem.objects.filter(
                cart__user=request.user, cart__status="ACTIVE"
            ).values_list("variation_id", flat=True)
        )
    return list(request.session.get("cart", {}).keys())
