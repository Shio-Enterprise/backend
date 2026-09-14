"""
Política única de disponibilidade de drops (issue #6).

Define dois conceitos distintos e intencionalmente diferentes:

- Visível: aparece no catálogo público (listagem e detalhe).
  is_public AND is_active AND dentro da janela [launch_date, end_date].
- Vendável: pode ser adicionado ao carrinho / comprado.
  Visível AND (max_quantity é nulo OU unidades já vendidas < max_quantity).

Um drop esgotado (max_quantity atingido) continua visível na loja — só a
compra é bloqueada. Produtos sem drop (product.drop is None) não são afetados
por esta política: seguem apenas o próprio Product.is_active.

Sem reservas/TTL: a contagem de unidades vendidas soma OrderItem.quantity de
todos os pedidos não CANCELED (mesmo critério já usado para o estoque, que é
debitado na criação do pedido e não na confirmação do pagamento). Por isso não
há rotina de liberação por expiração — cancelar o pedido já libera a
quantidade, pois deixa de ser contada.
"""

from django.db.models import Q, Sum
from django.utils import timezone


def visible_drops_queryset(queryset):
    """Filtra um queryset de DropCampaign para os drops publicamente visíveis."""
    now = timezone.now()
    return (
        queryset.filter(is_public=True, is_active=True)
        .filter(Q(launch_date__isnull=True) | Q(launch_date__lte=now))
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=now))
    )


def is_drop_visible(drop) -> bool:
    if drop is None:
        return True

    if not (drop.is_public and drop.is_active):
        return False

    now = timezone.now()
    if drop.launch_date and drop.launch_date > now:
        return False
    if drop.end_date and drop.end_date < now:
        return False
    return True


def get_drop_sold_quantity(drop) -> int:
    """Soma de unidades já vendidas do drop (pedidos não CANCELED)."""
    from orders.models import OrderItem, OrderStatus

    total = (
        OrderItem.objects.filter(variation__product__drop=drop)
        .exclude(order__status=OrderStatus.CANCELED)
        .aggregate(total=Sum("quantity"))["total"]
    )
    return total or 0


def is_drop_sellable(drop, sold_quantity=None) -> bool:
    if drop is None:
        return True
    if not is_drop_visible(drop):
        return False
    if drop.max_quantity is None:
        return True
    if sold_quantity is None:
        sold_quantity = get_drop_sold_quantity(drop)
    return sold_quantity < drop.max_quantity


def is_product_visible(product) -> bool:
    if not product.is_active:
        return False
    return is_drop_visible(product.drop)


def is_product_sellable(product) -> bool:
    if not product.is_active:
        return False
    return is_drop_sellable(product.drop)


def visible_products_queryset(queryset):
    """Filtra produtos ativos cujo drop (se houver) esteja visível."""
    now = timezone.now()
    visible_drop_q = Q(drop__isnull=True) | (
        Q(drop__is_public=True, drop__is_active=True)
        & (Q(drop__launch_date__isnull=True) | Q(drop__launch_date__lte=now))
        & (Q(drop__end_date__isnull=True) | Q(drop__end_date__gte=now))
    )
    return queryset.filter(is_active=True).filter(visible_drop_q)
