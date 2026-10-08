from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def normalize_money(value):
    """Valida reais e arredonda centavos sem passar por ponto flutuante."""
    try:
        amount = Decimal(str(value).replace(",", "."))
        if not amount.is_finite() or amount < 0:
            raise ValueError("Valor monetário inválido.")
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if amount > Decimal("99999999.99"):
            raise ValueError("Valor monetário acima do limite.")
        return amount
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Valor monetário inválido.") from exc


def money_to_cents(value):
    return int(normalize_money(value) * Decimal("100"))
