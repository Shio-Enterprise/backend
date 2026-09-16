"""
Política única de disponibilidade de drops.

Define três conceitos:

- Visível: aparece no catálogo público (listagem e detalhe).
  Só depende de is_public. Um drop Rascunho, Programado, Encerrado ou
  Esgotado continua aparecendo na loja — nenhum desses estados esconde o
  drop, eles só afetam se dá para comprar. Só is_public=False (Privado)
  resulta em 404 para o público.
- Aberto para venda (uso interno, ignora max_quantity): visível AND
  is_active AND dentro da janela [launch_date, end_date]. É o estado "pronto
  pra vender, sem considerar quanto já foi vendido" — usado no checkout para
  separar esse tipo de indisponibilidade (400) do limite de unidades (409,
  checado atomicamente à parte).
- Vendável: aberto para venda AND (max_quantity é nulo OU unidades já
  vendidas < max_quantity). É o que decide se o botão de comprar fica
  habilitado.

Produtos sem drop (product.drop is None) não são afetados por esta política:
seguem apenas o próprio Product.is_active.

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
    return queryset.filter(is_public=True)


def is_drop_visible(drop) -> bool:
    if drop is None:
        return True
    return bool(drop.is_public)


def is_drop_open_for_sale(drop) -> bool:
    """Drop pronto pra vender, ignorando o limite de max_quantity (checado à
    parte, atomicamente, no checkout — ver is_drop_sellable)."""
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
    if not is_drop_open_for_sale(drop):
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


def is_product_open_for_sale(product) -> bool:
    if not product.is_active:
        return False
    return is_drop_open_for_sale(product.drop)


def is_product_sellable(product) -> bool:
    if not product.is_active:
        return False
    return is_drop_sellable(product.drop)


def visible_products_queryset(queryset):
    """Filtra produtos ativos cujo drop (se houver) seja público."""
    visible_drop_q = Q(drop__isnull=True) | Q(drop__is_public=True)
    return queryset.filter(is_active=True).filter(visible_drop_q)
