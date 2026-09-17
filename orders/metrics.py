import datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.exceptions import ValidationError

from .models import CustomerOrder, OrderStatus, PaymentStatus

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
    """Aplica os filtros compartilhados antes de qualquer agregação."""
    queryset = queryset.filter(**dimension_filters(params))
    search = params.get("search", "").strip()
    if search:
        queryset = queryset.filter(
            Q(user__name__icontains=search)
            | Q(user__email__icontains=search)
            | Q(items__product_name__icontains=search)
            | Q(items__variation__product__name__icontains=search)
            | Q(items__variation__product__drop__name__icontains=search)
            | Q(items__variation__product__category__name__icontains=search)
        )
    return queryset.distinct()


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
        if name in {"drop", "category", "customer"}:
            try:
                UUID(str(value))
            except ValueError as exc:
                raise ValidationError(f"{name} deve ser um UUID válido.") from exc
        filters[lookup] = value
    return filters


def metric_orders(params, start=None, end=None) -> QuerySet:
    if start is None or end is None:
        start, end, _ = resolve_period(params)
    queryset = CustomerOrder.objects.filter(
        payment__paid_at__gte=start,
        payment__paid_at__lt=end,
    ).filter(VALID_SALE_Q | REFUND_Q)
    return apply_dimensions(queryset, params)


def positive_sales(queryset: QuerySet) -> QuerySet:
    return queryset.filter(VALID_SALE_Q)


def refunds(queryset: QuerySet) -> QuerySet:
    return queryset.filter(REFUND_Q)


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
