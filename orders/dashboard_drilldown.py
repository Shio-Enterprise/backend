"""Seleção de pedidos para os detalhamentos do dashboard."""

from uuid import UUID

from django.db.models import DecimalField, ExpressionWrapper, F, OuterRef, Subquery, Sum
from rest_framework.exceptions import ValidationError

from .metrics import (
    created_orders,
    metric_orders,
    positive_sales,
    refunds,
    resolve_period,
    sale_items,
)
from .models import OrderStatus, PaymentMethod

ITEM_METRICS = {
    "product_units",
    "product_revenue",
    "drop_item_revenue",
    "category_item_revenue",
}
ORDER_METRICS = {
    "all",
    "valid_sales",
    "gross_revenue",
    "refunds",
    "net_revenue",
    "average_ticket",
    "status",
    "payment_method",
    *ITEM_METRICS,
}
DETAIL_FILTERS = ("period", "start_date", "end_date", "drop", "category")
DRILLDOWN_FILTERS = (*DETAIL_FILTERS, "customer", "search")


def _detail_params(params):
    return {name: params[name] for name in DRILLDOWN_FILTERS if params.get(name)}


def _required_uuid(params, name):
    value = params.get(name)
    if not value:
        raise ValidationError({name: "Este parâmetro é obrigatório para a métrica."})
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise ValidationError({name: "Deve ser um UUID válido."}) from exc


def _unclassified(params, selector):
    value = params.get("unclassified", "false")
    if value not in {"true", "false"}:
        raise ValidationError({"unclassified": "Use true ou false."})
    if value == "true" and params.get(selector):
        raise ValidationError(
            {"unclassified": f"Não combine true com {selector}."}
        )
    return value == "true"


def _orders_for_items(sales, items):
    item_revenue = ExpressionWrapper(
        F("quantity") * F("unit_price"),
        output_field=DecimalField(max_digits=20, decimal_places=2),
    )
    per_order = (
        items.filter(order_id=OuterRef("pk"))
        .order_by()
        .values("order_id")
        .annotate(
            units=Sum("quantity"),
            item_revenue=Sum(item_revenue),
        )
    )
    return sales.filter(pk__in=items.values("order_id")).annotate(
        metric_units=Subquery(per_order.values("units")[:1]),
        metric_item_revenue=Subquery(per_order.values("item_revenue")[:1]),
    )


def dashboard_order_queryset(params):
    """Retorna pedidos, seletor normalizado e base de data do agregado clicado."""
    metric = params.get("metric", "all") or "all"
    if metric not in ORDER_METRICS:
        raise ValidationError({"metric": "Métrica desconhecida."})

    if metric == "all":
        queryset = metric_orders(params)
        return queryset.order_by("-payment__paid_at", "-pk"), metric, "payment.paid_at"

    filters = _detail_params(params)
    start, end, _ = resolve_period(filters)
    if metric == "status":
        selected_status = params.get("status")
        if selected_status not in OrderStatus.values:
            raise ValidationError({"status": "Informe um status válido."})
        queryset = created_orders(filters, start, end).filter(status=selected_status)
        return queryset.order_by("-created_at", "-pk"), metric, "order_created_at"

    orders = metric_orders(filters, start, end)
    sales = positive_sales(orders)
    if metric in {"valid_sales", "gross_revenue"}:
        queryset = sales
    elif metric == "refunds":
        queryset = refunds(orders)
    elif metric in {"net_revenue", "average_ticket"}:
        queryset = orders
    elif metric == "payment_method":
        method = params.get("payment_method")
        if method not in PaymentMethod.values:
            raise ValidationError(
                {"payment_method": "Informe um método de pagamento válido."}
            )
        queryset = sales.filter(payment__method=method)
    else:
        items = sale_items(filters, start, end)
        if metric in {"product_units", "product_revenue"}:
            if _unclassified(params, "product_id"):
                items = items.filter(variation__product_id__isnull=True)
            else:
                items = items.filter(
                    variation__product_id=_required_uuid(params, "product_id")
                )
        elif metric == "drop_item_revenue":
            if _unclassified(params, "drop"):
                items = items.filter(variation__product__drop_id__isnull=True)
            else:
                items = items.filter(
                    variation__product__drop_id=_required_uuid(params, "drop")
                )
        elif metric == "category_item_revenue":
            if _unclassified(params, "category"):
                items = items.filter(variation__product__category_id__isnull=True)
            else:
                items = items.filter(
                    variation__product__category_id=_required_uuid(params, "category")
                )
        queryset = _orders_for_items(sales, items)

    return queryset.order_by("-payment__paid_at", "-pk"), metric, "payment.paid_at"
