"""Liberação automática de reservas de estoque vencidas.

O projeto não tem fila de tarefas: a liberação roda dentro das próprias
requisições e no comando expire_stale_orders.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from orders.models import CustomerOrder, OrderStatus
from orders.services import release_if_expired

logger = logging.getLogger(__name__)


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
