import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import TruncDay, TruncMonth
from django.utils import timezone

from .models import AnalyticsConsent, AnalyticsConsentChoice, SiteEvent, SiteEventType
from .periods import BUSINESS_TIMEZONE, resolve_period

TOP_PRODUCTS_LIMIT = 5

# Eventos das telas administrativas ficam na linha do tempo do usuário, mas não entram nas métricas gerais.
ADMIN_PATH_FILTER = Q(path="/admin") | Q(path__startswith="/admin/")


def link_anonymous_events(user, anonymous_id):
    """Vincula à conta só os eventos anônimos. O consentimento da conta vem do POST /consent/."""
    return SiteEvent.objects.filter(
        anonymous_id=anonymous_id, user__isnull=True
    ).update(user=user)


def _owner_filter(user, anonymous_id):
    """Registros da conta ou, se ainda não vinculados a ninguém, deste navegador."""
    owner = Q(pk__in=[])
    if user is not None and user.is_authenticated:
        owner |= Q(user=user)
    if anonymous_id:
        owner |= Q(anonymous_id=anonymous_id, user__isnull=True)
    return owner


def current_consent(user=None, anonymous_id=None):
    return (
        AnalyticsConsent.objects.filter(_owner_filter(user, anonymous_id))
        .order_by("-created_at")
        .first()
    )


def has_analytics_consent(user=None, anonymous_id=None):
    """Indica se a escolha vigente é um aceite na versão atual do banner."""
    consent = current_consent(user, anonymous_id)
    return (
        consent is not None
        and consent.choice == AnalyticsConsentChoice.GRANTED
        and consent.version == settings.ANALYTICS_CONSENT_VERSION
    )


def anonymize_site_events(user=None, anonymous_id=None):
    """Troca conta e identificador por um UUID novo, único por pessoa recusada."""
    return SiteEvent.objects.filter(_owner_filter(user, anonymous_id)).update(
        user=None, anonymous_id=str(uuid.uuid4())
    )


@transaction.atomic
def record_analytics_consent(choice, user=None, anonymous_id=None):
    """Registra a escolha; ao recusar, a navegação já gravada deixa de identificar a pessoa."""
    consent = AnalyticsConsent.objects.create(
        user=user if user is not None and user.is_authenticated else None,
        anonymous_id=anonymous_id or None,
        choice=choice,
        version=settings.ANALYTICS_CONSENT_VERSION,
    )
    if choice == AnalyticsConsentChoice.DENIED:
        anonymize_site_events(user, anonymous_id)
    return consent


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
