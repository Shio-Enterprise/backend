import re

from rest_framework.exceptions import ValidationError

PHONE_INVALID_MESSAGE = "Telefone inválido. Informe DDD e número."
MOBILE_NINE_DIGITS_MESSAGE = (
    "Celular precisa ter 9 dígitos depois do DDD. Confira se falta o 9 no começo."
)


def phone_digits(value: str | None) -> str | None:
    """Telefone brasileiro só com dígitos (DDD + número) ou None se não for válido."""
    if value is None:
        return None
    digits = re.sub(r"[^0-9]", "", value)
    # Aceita quem digitou com o código do país (+55).
    if len(digits) in (12, 13) and digits.startswith("55"):
        digits = digits[2:]
    if len(digits) not in (10, 11) or len(set(digits)) == 1:
        return None
    # DDD vai de 11 a 99 e nenhum dos dois dígitos é zero.
    if "0" in digits[:2]:
        return None
    # Celular tem 9 dígitos começando por 9; fixo tem 8 começando de 2 a 5.
    if len(digits) == 11 and digits[2] != "9":
        return None
    if len(digits) == 10 and digits[2] not in "2345":
        return None
    return digits


def normalize_phone(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    digits = phone_digits(value)
    if digits is None:
        raise ValidationError(_invalid_message(value))
    return digits


def _invalid_message(value: str) -> str:
    # Celular antigo de 8 dígitos (antes do 9 extra): (61) 9999-9999.
    digits = re.sub(r"[^0-9]", "", value)
    if len(digits) == 12 and digits.startswith("55"):
        digits = digits[2:]
    if len(digits) == 10 and "0" not in digits[:2] and digits[2] in "6789":
        return MOBILE_NINE_DIGITS_MESSAGE
    return PHONE_INVALID_MESSAGE


def format_phone(value: str | None) -> str | None:
    """(61) 99999-9999 ou (61) 3333-4444. Valor fora do padrão volta como está."""
    if value and re.fullmatch(r"[0-9]{10,11}", value):
        return f"({value[:2]}) {value[2:-4]}-{value[-4:]}"
    return value


def international_phone(value: str | None) -> str:
    """+5561999999999 para gateways. Valor fora do padrão volta como está."""
    digits = phone_digits(value)
    if digits is None:
        return value or ""
    return f"+55{digits}"
