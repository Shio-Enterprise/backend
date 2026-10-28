from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

# Regras definidas na O7 (Sprint 1): o dashboard oferece apenas Mensal e Anual.
# Mensal cobre 30 dias com agrupamento diário; Anual cobre 365 dias com
# agrupamento mensal. Os períodos são calculados no fuso de São Paulo.
BUSINESS_TIMEZONE = ZoneInfo("America/Sao_Paulo")

MENSAL = "mensal"
ANUAL = "anual"

PERIODS = {
    MENSAL: {"days": 30, "group_by": "day"},
    ANUAL: {"days": 365, "group_by": "month"},
}


def resolve_period(period_name):
    """Retorna (início, fim, agrupamento) do período pedido, no fuso de negócio."""
    config = PERIODS[period_name]
    now_local = timezone.now().astimezone(BUSINESS_TIMEZONE)
    start_date = now_local.date() - timedelta(days=config["days"] - 1)
    start = datetime.combine(start_date, datetime.min.time(), tzinfo=BUSINESS_TIMEZONE)
    return start, timezone.now(), config["group_by"]
