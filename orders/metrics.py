import datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.exceptions import ValidationError

from .models import CustomerOrder, OrderItem, OrderStatus, PaymentStatus

METRICS_TIMEZONE = ZoneInfo("America/Sao_Paulo")
VALID_SALE_Q = Q(status=OrderStatus.DELIVERED, payment__status=PaymentStatus.PAID)
REFUND_Q = Q(payment__status=PaymentStatus.REFUNDED)


def _local_midnight(value: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(value, datetime.time.min, tzinfo=METRICS_TIMEZONE)


def resolve_period(params):
    """Resolve o intervalo [start, end) e a granularidade no timezone comercial."""
    today = timezone.now().astimezone(METRICS_TIMEZONE).date()
    preset = params.get("period", "monthly")
    if preset not in {"monthly", "annual"}:
        raise ValidationError("period deve ser monthly ou annual.")
    default_days = 30 if preset == "monthly" else 365

    raw_start = params.get("start_date")
    raw_end = params.get("end_date")
    start_date = (
        parse_date(raw_start)
        if raw_start
        else today - datetime.timedelta(days=default_days - 1)
    )
    end_date = parse_date(raw_end) if raw_end else today
    if (raw_start and start_date is None) or (raw_end and end_date is None):
        raise ValidationError("start_date e end_date devem usar o formato YYYY-MM-DD.")
    if start_date > end_date:
        raise ValidationError("start_date não pode ser posterior a end_date.")

    granularity = "month" if preset == "annual" else "day"
    return (
        _local_midnight(start_date),
        _local_midnight(end_date + datetime.timedelta(days=1)),
        granularity,
    )


def apply_dimensions(queryset: QuerySet, params) -> QuerySet:
    """Filtra pedidos sem multiplicá-los pelos itens associados."""
    filters = dimension_filters(params)
    if customer_id := filters.get("user_id"):
        queryset = queryset.filter(user_id=customer_id)
    item_filters = _item_filters(filters)
    if item_filters:
        queryset = queryset.filter(
            Exists(OrderItem.objects.filter(order_id=OuterRef("pk"), **item_filters))
        )

    search = params.get("search", "").strip()
    if search:
        matching_items = OrderItem.objects.filter(order_id=OuterRef("pk")).filter(
            Q(product_name__icontains=search)
            | Q(variation__product__name__icontains=search)
            | Q(variation__product__drop__name__icontains=search)
            | Q(variation__product__category__name__icontains=search)
        )
        queryset = queryset.alias(matching_search_item=Exists(matching_items))
        queryset = queryset.filter(
            Q(user__name__icontains=search)
            | Q(user__email__icontains=search)
            | Q(matching_search_item=True)
        )
    return queryset


def dimension_filters(params):
    mappings = {
        "drop": "items__variation__product__drop_id",
        "category": "items__variation__product__category_id",
        "customer": "user_id",
    }
    filters = {}
    for name, lookup in mappings.items():
        value = params.get(name)
        if not value:
            continue
        if name in {"drop", "category"}:
            try:
                UUID(str(value))
            except ValueError as exc:
                raise ValidationError(f"{name} deve ser um UUID válido.") from exc
        elif name == "customer":
            try:
                value = int(value)
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    "customer deve ser um ID inteiro válido."
                ) from exc
            if value < 1:
                raise ValidationError("customer deve ser um ID inteiro válido.")
        filters[lookup] = value
    return filters


def _item_filters(filters):
    """Converte os filtros de pedido em filtros aplicáveis ao mesmo OrderItem."""
    return {
        lookup.removeprefix("items__"): value
        for lookup, value in filters.items()
        if lookup.startswith("items__")
    }


def metric_orders(params, start=None, end=None) -> QuerySet:
    """Pedidos comerciais na janela de pagamento: venda válida ou reembolso."""
    if start is None or end is None:
        start, end, _ = resolve_period(params)
    queryset = CustomerOrder.objects.filter(
        payment__paid_at__gte=start,
        payment__paid_at__lt=end,
    ).filter(VALID_SALE_Q | REFUND_Q)
    return apply_dimensions(queryset, params)


def created_orders(params, start=None, end=None) -> QuerySet:
    """Todos os pedidos criados na janela, inclusive os ainda não pagos."""
    if start is None or end is None:
        start, end, _ = resolve_period(params)
    queryset = CustomerOrder.objects.filter(created_at__gte=start, created_at__lt=end)
    return apply_dimensions(queryset, params)


def positive_sales(queryset: QuerySet) -> QuerySet:
    return queryset.filter(VALID_SALE_Q)


def refunds(queryset: QuerySet) -> QuerySet:
    return queryset.filter(REFUND_Q)


def sale_items(params, start=None, end=None) -> QuerySet:
    """Itens de vendas válidas; drop e categoria restringem o próprio item."""
    orders = positive_sales(metric_orders(params, start, end))
    return OrderItem.objects.filter(
        order_id__in=orders.values("pk"), **_item_filters(dimension_filters(params))
    )


def revenue_value(order: CustomerOrder) -> Decimal:
    value = Decimal(order.total_amount)
    return -value if order.payment.status == PaymentStatus.REFUNDED else value


def is_recurring_customer(user, params=None) -> bool:
    if params is None:
        orders = CustomerOrder.objects.filter(user=user).filter(VALID_SALE_Q)
    else:
        orders = positive_sales(metric_orders(params)).filter(user=user)
    purchased = set(
        orders.values_list("items__variation__product__drop_id", flat=True).exclude(
            items__variation__product__drop_id__isnull=True
        )
    )
    if len(purchased) < 2:
        return False

    from products.models import DropCampaign

    all_ids = list(
        DropCampaign.objects.order_by("launch_date", "created_at", "id").values_list(
            "id", flat=True
        )
    )
    positions = sorted(
        all_ids.index(drop_id) for drop_id in purchased if drop_id in all_ids
    )
    return any(
        current == previous + 1 for previous, current in zip(positions, positions[1:])
    )
