"""Regras de uso de cupons derivadas dos pedidos.

Não existe contador de usos no Coupon: um cupom foi usado quando há um
pedido válido com ele. Assim, quando um pedido é cancelado (inclusive por
reserva vencida), o uso volta sem precisar sincronizar nada.
"""

from django.db.models import Q

from orders.models import CustomerOrder, OrderStatus


def valid_order_q(prefix=""):
    """Filtro de pedido que conta como compra: qualquer pedido não cancelado.

    Pedido aguardando pagamento com a reserva já vencida continua contando
    até ser cancelado. Se deixasse de contar, o cliente poderia usar o cupom
    de novo e depois pagar o link antigo, que o webhook ainda aceita.

    `prefix` permite usar a mesma regra a partir de outro modelo, por
    exemplo `Count("orders", filter=valid_order_q("orders__"))` no Coupon.
    """
    return ~Q(**{f"{prefix}status": OrderStatus.CANCELED})


def count_coupon_uses(coupon, user=None):
    """Quantos pedidos válidos usaram o cupom, no total ou só os do usuário."""
    orders = CustomerOrder.objects.filter(valid_order_q(), coupon=coupon)
    if user is not None:
        orders = orders.filter(user=user)
    return orders.count()
