from calendar import monthrange
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

# Sem mês e ano, o recorte segue a O7: mensal são os últimos 30 dias por dia,
# anual os últimos 365 por mês. Com mês e ano, o recorte é o calendário pedido.
# Tudo no fuso de São Paulo.
BUSINESS_TIMEZONE = ZoneInfo("America/Sao_Paulo")

MENSAL = "mensal"
ANUAL = "anual"

PERIODS = {
    MENSAL: {"days": 30, "group_by": "day"},
    ANUAL: {"days": 365, "group_by": "month"},
}


def resolve_period(period_name, *, year=None, month=None):
    """Retorna (início, fim, agrupamento) do período pedido, no fuso de negócio.

    Sem ano, mantém a janela móvel. Com ano, o anual é aquele ano e o mensal
    exige também o mês.
    """
    config = PERIODS[period_name]
    today = timezone.now().astimezone(BUSINESS_TIMEZONE).date()

    if year is None and month is None:
        start_date = today - timedelta(days=config["days"] - 1)
        start = datetime.combine(start_date, datetime.min.time(), tzinfo=BUSINESS_TIMEZONE)
        return start, timezone.now(), config["group_by"]

    if year is None or year < today.year - 20 or year > today.year:
        raise ValueError("Ano inválido.")

    if period_name == ANUAL:
        return _calendar_window(
            date_start=date(year, 1, 1),
            end_date=date(year, 12, 31),
            group_by="month",
        )

    if month is None or month < 1 or month > 12:
        raise ValueError("Mês inválido.")
    start_date = date(year, month, 1)
    if start_date > today:
        raise ValueError("Esse mês ainda não começou.")
    end_date = date(year, month, monthrange(year, month)[1])
    return _calendar_window(date_start=start_date, end_date=end_date, group_by="day")


def _calendar_window(*, date_start, end_date, group_by):
    start = datetime.combine(date_start, datetime.min.time(), tzinfo=BUSINESS_TIMEZONE)
    end = datetime.combine(end_date, time(23, 59, 59), tzinfo=BUSINESS_TIMEZONE)
    now = timezone.now()
    if end > now:
        end = now
    return start, end, group_by
