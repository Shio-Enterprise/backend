import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings


@dataclass(frozen=True)
class ShippingParameters:
    origin: str
    destination: str
    service: str
    weight_grams: str
    length_cm: str
    width_cm: str
    height_cm: str
    declared_value: str
    policy_version: str
    mock_enabled: bool
    base_url: str

    def as_snapshot(self):
        return asdict(self)


def _positive_decimal(value, name):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Configuração logística inválida: {name}.") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"Configuração logística inválida: {name}.")
    return format(parsed.normalize(), "f")


def resolve_shipping_parameters(destination, *, declared_value="0"):
    origin = str(settings.CORREIOS_REMETENTE_CEP).replace("-", "").strip()
    destination = str(destination).replace("-", "").strip()
    service = str(settings.CORREIOS_CODIGO_SERVICO).strip()
    if not re.fullmatch(r"[0-9]{8}", origin):
        raise ValueError("CEP de origem configurado inválido.")
    if not re.fullmatch(r"[0-9]{8}", destination):
        raise ValueError("CEP de destino inválido.")
    if not re.fullmatch(r"[0-9]+", service):
        raise ValueError("Código de serviço configurado inválido.")

    try:
        declared = Decimal(str(declared_value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Valor declarado inválido.") from exc
    if not declared.is_finite() or declared < 0:
        raise ValueError("Valor declarado inválido.")

    policy_version = str(settings.SHIPPING_POLICY_VERSION).strip()
    if not policy_version:
        raise ValueError("Versão da política logística inválida.")
    try:
        declared_value = format(declared.quantize(Decimal("0.01")), "f")
    except InvalidOperation as exc:
        raise ValueError("Valor declarado inválido.") from exc

    return ShippingParameters(
        origin=origin,
        destination=destination,
        service=service,
        weight_grams=_positive_decimal(settings.CORREIOS_PESO_PADRAO_GRAMAS, "peso"),
        length_cm=_positive_decimal(settings.CORREIOS_COMPRIMENTO_CM, "comprimento"),
        width_cm=_positive_decimal(settings.CORREIOS_LARGURA_CM, "largura"),
        height_cm=_positive_decimal(settings.CORREIOS_ALTURA_CM, "altura"),
        declared_value=declared_value,
        policy_version=policy_version,
        mock_enabled=settings.CORREIOS_MOCK_ENABLED,
        base_url=settings.CORREIOS_API_BASE_URL.rstrip("/"),
    )
