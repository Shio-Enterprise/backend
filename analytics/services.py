from datetime import timedelta

from django.db.models import Count
from django.db.models.functions import TruncDay, TruncMonth
from django.utils import timezone

from .models import SiteEvent, SiteEventType
from .periods import BUSINESS_TIMEZONE, resolve_period

TOP_PRODUCTS_LIMIT = 5


def _buckets(start, group_by):
    """Lista de datas de início de cada intervalo do período, sem lacunas."""
    today = timezone.now().astimezone(BUSINESS_TIMEZONE).date()
    start_date = start.astimezone(BUSINESS_TIMEZONE).date()

    if group_by == "day":
        return [
            start_date + timedelta(days=i) for i in range((today - start_date).days + 1)
        ]

    buckets = []
    current = start_date.replace(day=1)
    last_month = today.replace(day=1)
    while current <= last_month:
        buckets.append(current)
        current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    return buckets


def build_overview(period_name):
    start, end, group_by = resolve_period(period_name)
    events = SiteEvent.objects.filter(occurred_at__gte=start, occurred_at__lte=end)

    totals = {
        row["event_type"]: row["total"]
        for row in events.order_by().values("event_type").annotate(total=Count("id"))
    }
    by_type = {
        event_type: totals.get(event_type, 0) for event_type in SiteEventType.values
    }

    trunc = TruncDay if group_by == "day" else TruncMonth
    bucket_counts = {
        row["bucket"].astimezone(BUSINESS_TIMEZONE).date(): row["total"]
        for row in events.order_by()
        .annotate(bucket=trunc("occurred_at", tzinfo=BUSINESS_TIMEZONE))
        .values("bucket")
        .annotate(total=Count("id"))
    }
    series = [
        {"date": bucket.isoformat(), "total": bucket_counts.get(bucket, 0)}
        for bucket in _buckets(start, group_by)
    ]

    top_products = [
        {
            "product_id": row["product_id"],
            "name": row["product__name"],
            "views": row["total"],
        }
        for row in events.filter(
            event_type=SiteEventType.PRODUCT_VIEW, product__isnull=False
        )
        .order_by()
        .values("product_id", "product__name")
        .annotate(total=Count("id"))
        .order_by("-total")[:TOP_PRODUCTS_LIMIT]
    ]

    return {
        "period": period_name,
        "group_by": group_by,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "visitors": {
            "registered": events.filter(user__isnull=False)
            .order_by()
            .values("user_id")
            .distinct()
            .count(),
            "anonymous": events.filter(user__isnull=True)
            .order_by()
            .values("anonymous_id")
            .distinct()
            .count(),
        },
        "events_by_type": by_type,
        "series": series,
        "top_products": top_products,
    }
