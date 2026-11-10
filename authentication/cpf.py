import re

from rest_framework.exceptions import ValidationError


def normalize_cpf(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None

    cpf = re.sub(r"[^0-9]", "", value)
    if len(cpf) != 11 or len(set(cpf)) == 1:
        raise ValidationError("CPF inválido.")

    for size in (9, 10):
        total = sum(int(cpf[i]) * (size + 1 - i) for i in range(size))
        if int(cpf[size]) != (total * 10 % 11) % 10:
            raise ValidationError("CPF inválido.")
    return cpf
