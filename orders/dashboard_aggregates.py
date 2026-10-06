"""Agregados comerciais e operacionais do dashboard administrativo detalhado."""

import datetime
from collections import defaultdict
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import TruncDay, TruncMonth

from products.models import DropCampaign, ProductVariation

from .metrics import (
    METRICS_TIMEZONE,
    created_orders,
    metric_orders,
    positive_sales,
    refunds,
    resolve_period,
    sale_items,
)
from .models import OrderItem

RANKING_LIMIT = 10
STOCK_ATTENTION_LIMIT = 50
ZERO = Decimal("0.00")


def _item_revenue_expression():
    return ExpressionWrapper(
        F("quantity") * F("unit_price"),
        output_field=DecimalField(max_digits=20, decimal_places=2),
    )


def _financial(sales, refunded):
    gross = sales.aggregate(total=Sum("total_amount"))["total"] or ZERO
    refund_total = refunded.aggregate(total=Sum("total_amount"))["total"] or ZERO
    count = sales.count()
    net = gross - refund_total
    return {
        "gross_revenue": gross,
        "refunds": refund_total,
        "net_revenue": net,
        "average_ticket": round(net / count, 2) if count else ZERO,
        "valid_sales": count,
    }


def _bucket_dates(start, end, granularity):
    current = start.date()
    last = (end - datetime.timedelta(microseconds=1)).date()
    if granularity == "month":
        current = current.replace(day=1)
        last = last.replace(day=1)
    while current <= last:
        yield current
        if granularity == "month":
            current = (current.replace(day=28) + datetime.timedelta(days=4)).replace(
                day=1
            )
        else:
            current += datetime.timedelta(days=1)


def _sales_series(sales, refunded, start, end, granularity):
    trunc = (
        TruncMonth("payment__paid_at", tzinfo=METRICS_TIMEZONE)
        if granularity == "month"
        else TruncDay("payment__paid_at", tzinfo=METRICS_TIMEZONE)
    )
    gross_points = {
        row["bucket"].date(): row
        for row in sales.annotate(bucket=trunc)
        .values("bucket")
        .annotate(valid_sales=Count("id"), gross_revenue=Sum("total_amount"))
    }
    refund_points = {
        row["bucket"].date(): row["refunds"]
        for row in refunded.annotate(bucket=trunc)
        .values("bucket")
        .annotate(refunds=Sum("total_amount"))
    }
    series = []
    for bucket in _bucket_dates(start, end, granularity):
        gross_point = gross_points.get(bucket, {})
        gross = gross_point.get("gross_revenue") or ZERO
        refund_total = refund_points.get(bucket) or ZERO
        series.append(
            {
                "period": bucket.isoformat(),
                "gross_revenue": gross,
                "refunds": refund_total,
                "net_revenue": gross - refund_total,
                "valid_sales": gross_point.get("valid_sales", 0),
            }
        )
    return series


def _product_rankings(items):
    revenue = _item_revenue_expression()
    product_rows = (
        items.filter(variation__product__isnull=False)
        .values("variation__product_id", "variation__product__name")
        .annotate(units=Sum("quantity"), revenue=Sum(revenue))
    )

    def rows(ordering):
        return [
            {
                "product_id": row["variation__product_id"],
                "product_name": row["variation__product__name"],
                "units": row["units"],
                "revenue": row["revenue"],
            }
            for row in product_rows.order_by(*ordering)[:RANKING_LIMIT]
        ]

    unclassified = items.filter(variation__product__isnull=True).aggregate(
        units=Sum("quantity"), revenue=Sum(revenue)
    )
    return {
        "by_units": rows(("-units", "-revenue", "variation__product_id")),
        "by_revenue": rows(("-revenue", "-units", "variation__product_id")),
        "unclassified": {
            "units": unclassified["units"] or 0,
            "revenue": unclassified["revenue"] or ZERO,
        },
    }


def _dimension_revenue(items, dimension):
    id_field = f"variation__product__{dimension}_id"
    name_field = f"variation__product__{dimension}__name"
    rows = (
        items.values(id_field, name_field)
        .annotate(units=Sum("quantity"), revenue=Sum(_item_revenue_expression()))
        .order_by("-revenue", id_field)
    )
    return [
        {
            f"{dimension}_id": row[id_field],
            "name": row[name_field] or "Sem classificação",
            "units": row["units"],
            "revenue": row["revenue"],
        }
        for row in rows
    ]


def _orders_by_status(params, start, end):
    rows = (
        created_orders(params, start, end)
        .values("status")
        .annotate(orders=Count("id"))
        .order_by("status")
    )
    return {"date_basis": "order_created_at", "rows": list(rows)}


def _sales_by_payment_method(sales):
    rows = (
        sales.values("payment__method")
        .annotate(valid_sales=Count("id"), gross_revenue=Sum("total_amount"))
        .order_by("payment__method")
    )
    return [
        {
            "method": row["payment__method"],
            "valid_sales": row["valid_sales"],
            "gross_revenue": row["gross_revenue"],
        }
        for row in rows
    ]


def _recurring_customer_count(sales):
    """Replica a regra de dois drops consecutivos sem consulta por cliente."""
    drop_positions = {
        drop_id: position
        for position, drop_id in enumerate(
            DropCampaign.objects.order_by("launch_date", "created_at", "id")
            .values_list("id", flat=True)
        )
    }
    if len(drop_positions) < 2:
        return 0

    purchased = defaultdict(set)
    rows = (
        OrderItem.objects.filter(order_id__in=sales.values("pk"))
        .filter(order__user__is_staff=False, order__user__is_superuser=False)
        .exclude(variation__product__drop_id__isnull=True)
        .values_list("order__user_id", "variation__product__drop_id")
        .distinct()
    )
    for user_id, drop_id in rows:
        if drop_id in drop_positions:
            purchased[user_id].add(drop_positions[drop_id])
    return sum(
        any(position + 1 in positions for position in positions)
        for positions in purchased.values()
    )


def _customers(start, end, sales):
    User = get_user_model()
    customers = User.objects.filter(is_staff=False, is_superuser=False)
    return {
        "total_registered": customers.count(),
        "new_in_period": customers.filter(
            created_at__gte=start, created_at__lt=end
        ).count(),
        "recurring_customers": _recurring_customer_count(sales),
    }


def _stock(params):
    variations = ProductVariation.objects.all()
    if drop_id := params.get("drop"):
        variations = variations.filter(product__drop_id=drop_id)
    if category_id := params.get("category"):
        variations = variations.filter(product__category_id=category_id)
    counts = variations.aggregate(
        low_count=Count(
            "id", filter=Q(stock_quantity__gt=0, stock_quantity__lt=10)
        ),
        out_count=Count("id", filter=Q(stock_quantity=0)),
    )
    attention = variations.filter(stock_quantity__lt=10).select_related("product")
    attention = attention.order_by("stock_quantity", "product__name", "id")[
        :STOCK_ATTENTION_LIMIT
    ]
    return {
        **counts,
        "attention": [
            {
                "variation_id": variation.id,
                "product_id": variation.product_id,
                "product_name": variation.product.name,
                "size": variation.size,
                "color": variation.color,
                "sku": variation.sku,
                "stock_quantity": variation.stock_quantity,
                "admin_path": f"/admin/stock/{variation.product_id}",
            }
            for variation in attention
        ],
    }


def dashboard_detail_aggregates(params):
    """Calcula o retrato detalhado com uma única resolução de período/filtros."""
    start, end, granularity = resolve_period(params)
    orders = metric_orders(params, start, end)
    sales = positive_sales(orders)
    refunded = refunds(orders)
    items = sale_items(params, start, end)
    return {
        "period": {
            "start_date": start.date().isoformat(),
            "end_date": (end - datetime.timedelta(microseconds=1)).date().isoformat(),
            "granularity": granularity,
            "timezone": "America/Sao_Paulo",
        },
        "financial": _financial(sales, refunded),
        "sales_series": _sales_series(sales, refunded, start, end, granularity),
        "product_rankings": _product_rankings(items),
        "item_revenue": {
            "by_drop": _dimension_revenue(items, "drop"),
            "by_category": _dimension_revenue(items, "category"),
            "basis": "order_item_quantity_times_unit_price",
        },
        "orders_by_status": _orders_by_status(params, start, end),
        "sales_by_payment_method": _sales_by_payment_method(sales),
        "customers": _customers(start, end, sales),
        "stock": _stock(params),
    }
