from .models import OrderStatus

# Regra já adotada pelo dashboard: pedidos ainda não pagos ou cancelados não
# representam venda. Questões como estorno e data de competência continuam fora
# deste recorte até haver decisão de negócio.
SALES_ORDER_STATUSES = (
    OrderStatus.PAID,
    OrderStatus.PREPARING,
    OrderStatus.SHIPPED,
    OrderStatus.DELIVERED,
)
