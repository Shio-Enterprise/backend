from datetime import timedelta

from django.db.models import Count, Max, Q
from django.db.models.functions import TruncDay, TruncMonth
from django.utils import timezone

from products.models import Product

from .models import SiteEvent, SiteEventType
from .periods import BUSINESS_TIMEZONE, resolve_period

TOP_PRODUCTS_LIMIT = 5
SHARED_PRODUCT_LIMIT = 8
SHARED_USER_LIMIT = 8
ACCESS_PAGE_SIZE = 20
PRODUCT_SEARCH_LIMIT = 10
INTEREST_RANK = {
    SiteEventType.PRODUCT_VIEW: 1,
    SiteEventType.REMOVE_FROM_CART: 2,
    SiteEventType.ADD_TO_CART: 3,
    SiteEventType.CHECKOUT_STARTED: 4,
    SiteEventType.PURCHASE: 5,
}

# Eventos das telas administrativas ficam na linha do tempo do usuário, mas não entram nas métricas gerais.
ADMIN_PATH_FILTER = Q(path="/admin") | Q(path__startswith="/admin/")


def link_anonymous_events(user, anonymous_id):
    """Vincula os eventos anônimos deste identificador à conta que acabou de fazer login."""
    return SiteEvent.objects.filter(
        anonymous_id=anonymous_id, user__isnull=True
    ).update(user=user)


def _buckets(start, end, group_by):
    """Lista de datas de início de cada intervalo do período, sem lacunas."""
    start_date = start.astimezone(BUSINESS_TIMEZONE).date()
    end_date = end.astimezone(BUSINESS_TIMEZONE).date()

    if group_by == "day":
        return [
            start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)
        ]

    buckets = []
    current = start_date.replace(day=1)
    last_month = end_date.replace(day=1)
    while current <= last_month:
        buckets.append(current)
        current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    return buckets


def build_overview(period_name, *, year=None, month=None):
    start, end, group_by = resolve_period(period_name, year=year, month=month)
    events = SiteEvent.objects.filter(
        occurred_at__gte=start, occurred_at__lte=end
    ).exclude(ADMIN_PATH_FILTER)

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
        for bucket in _buckets(start, end, group_by)
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


def build_shared_interest(period_name, *, year=None, month=None):
    """Agrupa, por produto, as contas que navegaram a mesma peça no período."""
    start, end, _group_by = resolve_period(period_name, year=year, month=month)
    events = SiteEvent.objects.filter(
        occurred_at__gte=start,
        occurred_at__lte=end,
        product__isnull=False,
        event_type__in=list(INTEREST_RANK),
    ).exclude(ADMIN_PATH_FILTER)

    products = {}
    for row in events.filter(user__isnull=False).values(
        "product_id", "product__name", "user_id", "user__email", "event_type"
    ):
        product = products.setdefault(
            row["product_id"],
            {
                "product_id": row["product_id"],
                "name": row["product__name"],
                "users": {},
                "anonymous": 0,
            },
        )
        person = product["users"].setdefault(
            row["user_id"],
            {
                "id": row["user_id"],
                "email": row["user__email"],
                "interest": row["event_type"],
            },
        )
        if INTEREST_RANK[row["event_type"]] > INTEREST_RANK[person["interest"]]:
            person["interest"] = row["event_type"]

    anonymous_rows = (
        events.filter(user__isnull=True)
        .exclude(anonymous_id__isnull=True)
        .exclude(anonymous_id="")
        .values("product_id", "product__name")
        .annotate(total=Count("anonymous_id", distinct=True))
    )
    for row in anonymous_rows:
        product = products.setdefault(
            row["product_id"],
            {
                "product_id": row["product_id"],
                "name": row["product__name"],
                "users": {},
                "anonymous": 0,
            },
        )
        product["anonymous"] = row["total"]

    shared = [item for item in products.values() if len(item["users"]) >= 2]
    ranked = sorted(
        shared,
        key=lambda item: (-len(item["users"]), -item["anonymous"], item["name"] or ""),
    )[:SHARED_PRODUCT_LIMIT]

    payload = []
    for item in ranked:
        users = sorted(
            item["users"].values(),
            key=lambda person: (
                -INTEREST_RANK[person["interest"]],
                person["email"] or "",
            ),
        )
        payload.append(
            {
                "product_id": item["product_id"],
                "name": item["name"],
                "accounts": len(item["users"]),
                "anonymous": item["anonymous"],
                "users": users[:SHARED_USER_LIMIT],
            }
        )
    return payload


def search_products(query):
    return list(
        Product.objects.filter(name__icontains=query)
        .order_by("name")
        .values("id", "name")[:PRODUCT_SEARCH_LIMIT]
    )


def _variation_label(variation):
    if variation is None:
        return None
    if variation.color:
        return f"{variation.size} / {variation.color}"
    return variation.size


def build_product_access(product_id, period_name, page, *, year=None, month=None):
    """Acessos de um produto: ação, horário, conta e variação."""
    product = Product.objects.filter(pk=product_id).first()
    if product is None:
        return None
    start, end, _group_by = resolve_period(period_name, year=year, month=month)
    events = (
        SiteEvent.objects.filter(
            product=product,
            occurred_at__gte=start,
            occurred_at__lte=end,
            event_type__in=list(INTEREST_RANK),
        )
        .exclude(ADMIN_PATH_FILTER)
        .select_related("user", "variation")
        .order_by("-occurred_at")
    )
    totals = {event_type: 0 for event_type in INTEREST_RANK}
    for row in events.values("event_type").annotate(total=Count("id")):
        totals[row["event_type"]] = row["total"]
    offset = (page - 1) * ACCESS_PAGE_SIZE
    accesses = []
    for event in events[offset : offset + ACCESS_PAGE_SIZE]:
        accesses.append(
            {
                "id": str(event.id),
                "event_type": event.event_type,
                "occurred_at": event.occurred_at,
                "path": event.path,
                "email": event.user.email if event.user_id else None,
                "variation": _variation_label(event.variation),
            }
        )
    return {
        "product_id": str(product.id),
        "name": product.name,
        "count": sum(totals.values()),
        "totals": totals,
        "accesses": accesses,
    }


def build_user_products(user, period_name, *, year=None, month=None):
    """Produtos que uma conta acessou no período, com a contagem de cada ação."""
    start, end, _group_by = resolve_period(period_name, year=year, month=month)
    rows = (
        SiteEvent.objects.filter(
            user=user,
            product__isnull=False,
            occurred_at__gte=start,
            occurred_at__lte=end,
            event_type__in=list(INTEREST_RANK),
        )
        .exclude(ADMIN_PATH_FILTER)
        .values("product_id", "product__name", "event_type")
        .annotate(total=Count("id"), last_access=Max("occurred_at"))
    )
    field_by_type = {
        SiteEventType.PRODUCT_VIEW: "views",
        SiteEventType.ADD_TO_CART: "added_to_cart",
        SiteEventType.REMOVE_FROM_CART: "removed_from_cart",
        SiteEventType.CHECKOUT_STARTED: "checkouts",
        SiteEventType.PURCHASE: "purchases",
    }
    products = {}
    for row in rows:
        item = products.setdefault(
            row["product_id"],
            {
                "product_id": str(row["product_id"]),
                "name": row["product__name"],
                "views": 0,
                "added_to_cart": 0,
                "removed_from_cart": 0,
                "checkouts": 0,
                "purchases": 0,
                "last_access": None,
            },
        )
        field = field_by_type.get(row["event_type"])
        if field:
            item[field] = row["total"]
        if item["last_access"] is None or row["last_access"] > item["last_access"]:
            item["last_access"] = row["last_access"]
    ranked = sorted(products.values(), key=lambda item: item["last_access"], reverse=True)
    return ranked
