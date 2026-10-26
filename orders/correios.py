import base64
import logging
from datetime import datetime

import requests
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from redis.exceptions import RedisError

from . import correios_mock
from .integrations.correios_client import CorreiosClient
from .integrations.correios_token_provider import CorreiosTokenProvider

logger = logging.getLogger(__name__)

CORREIOS_TOKEN_CACHE_KEY = "correios_access_token"
correios_client = CorreiosClient(transport=requests)


def correios_url(path: str) -> str:
    return correios_client.build_correios_url(path)


class CorreiosAuthenticationError(Exception):
    pass


class CorreiosTrackingUnavailableError(Exception):
    pass


class CorreiosPrePostagemError(Exception):
    pass


class CorreiosCepNotFoundError(Exception):
    pass


def build_basic_auth_header(username: str, password: str) -> str:
    credentials = f"{username}:{password}"
    encoded_credentials = base64.b64encode(credentials.encode()).decode()
    return f"Basic {encoded_credentials}"


def calculate_token_ttl_in_seconds(expires_at_iso: str) -> int:
    expires_at = datetime.fromisoformat(expires_at_iso)
    now = datetime.now(tz=expires_at.tzinfo)
    remaining_seconds = int((expires_at - now).total_seconds())
    return max(remaining_seconds - 60, 0)


def _load_new_correios_access_token() -> tuple[str, int]:
    if settings.CORREIOS_MOCK_ENABLED:
        return correios_mock.mock_token_response(), 60

    username = settings.CORREIOS_USERNAME
    password = settings.CORREIOS_PASSWORD
    cartao_postagem = settings.CORREIOS_CARTAO_POSTAGEM
    payload = {"numero": cartao_postagem}

    # requests can send basic auth via `auth` tuple
    response = correios_client.request(
        "POST",
        "token/v1/autentica/cartaopostagem",
        json=payload,
        auth=(username, password),
        timeout=10,
    )
    response.raise_for_status()

    data = response.json()
    token = data.get("token")
    expires_at = data.get("expiraEm")

    if not token or not expires_at:
        raise CorreiosAuthenticationError(
            "Resposta de autenticação dos Correios inválida."
        )

    ttl = calculate_token_ttl_in_seconds(expires_at)
    return token, ttl


correios_token_provider = CorreiosTokenProvider(_load_new_correios_access_token)


def request_new_correios_access_token() -> str:
    try:
        return correios_token_provider.refresh_token()
    except RedisError as exc:
        raise CorreiosAuthenticationError("Cache dos Correios indisponível.") from exc


def get_valid_correios_access_token() -> str:
    try:
        return correios_token_provider.get_token()
    except RedisError as exc:
        raise CorreiosAuthenticationError("Cache dos Correios indisponível.") from exc


def _authenticated_request(method: str, path: str, **kwargs):
    token = get_valid_correios_access_token()
    response = correios_client.request(method, path, token=token, **kwargs)
    if response.status_code == 401:
        correios_token_provider.invalidate_if_current(token)
        token = get_valid_correios_access_token()
        response = correios_client.request(method, path, token=token, **kwargs)
    return response


def fetch_tracking_data_from_correios(tracking_code: str) -> dict:
    if settings.CORREIOS_MOCK_ENABLED:
        return correios_mock.mock_tracking_response(tracking_code)

    response = _authenticated_request(
        "GET", f"srorastro/v1/objetos/{tracking_code}", timeout=10
    )

    response.raise_for_status()
    return response.json()


def extract_most_recent_event_description(events: list[dict]) -> str:
    if not events:
        return ""
    return events[0].get("descricao", "")


def format_single_tracking_event(raw_event: dict) -> dict:
    unit = raw_event.get("unidade", {})
    address = unit.get("endereco", {})
    city = address.get("cidade", "")
    state = address.get("uf", "")
    location = f"{city} - {state}" if city and state else city or state

    return {
        "data": raw_event.get("dtHrCriado"),
        "descricao": raw_event.get("descricao", ""),
        "detalhe": raw_event.get("detalhe", ""),
        "local": location,
    }


def format_tracking_events_list(raw_events: list[dict]) -> list[dict]:
    return [format_single_tracking_event(event) for event in raw_events]


def build_tracking_response_from_correios_data(
    tracking_code: str, correios_data: dict
) -> dict:
    objects = correios_data.get("objetos", [])
    if not objects:
        return build_empty_tracking_response(tracking_code)

    object_data = objects[0]
    raw_events = object_data.get("eventos", [])

    return {
        "tracking_code": tracking_code,
        "status_atual": extract_most_recent_event_description(raw_events),
        "previsao_entrega": object_data.get("dtPrevista"),
        "eventos": format_tracking_events_list(raw_events),
    }


def build_empty_tracking_response(tracking_code: str) -> dict:
    return {
        "tracking_code": tracking_code,
        "status_atual": "",
        "previsao_entrega": None,
        "eventos": [],
    }


def build_not_shipped_response() -> dict:
    return {
        "tracking_code": None,
        "status": "not_shipped",
        "eventos": [],
    }


def get_order_tracking_data(tracking_code: str | None) -> dict:
    if not tracking_code:
        return build_not_shipped_response()

    correios_data = fetch_tracking_data_from_correios(tracking_code)
    return build_tracking_response_from_correios_data(tracking_code, correios_data)


def build_remetente_data_from_settings() -> dict:
    return {
        "nome": settings.CORREIOS_REMETENTE_NOME,
        "email": settings.CORREIOS_REMETENTE_EMAIL,
        "cpfCnpj": settings.CORREIOS_REMETENTE_CPF_CNPJ,
        "endereco": {
            "cep": settings.CORREIOS_REMETENTE_CEP,
            "logradouro": settings.CORREIOS_REMETENTE_LOGRADOURO,
            "numero": settings.CORREIOS_REMETENTE_NUMERO,
            "complemento": settings.CORREIOS_REMETENTE_COMPLEMENTO,
            "bairro": settings.CORREIOS_REMETENTE_BAIRRO,
            "cidade": settings.CORREIOS_REMETENTE_CIDADE,
            "uf": settings.CORREIOS_REMETENTE_UF,
        },
    }


def build_destinatario_data_from_order(order) -> dict:
    user = order.user
    cpf_cnpj = ""
    try:
        profile = user.profile
        if profile and profile.cpf:
            cpf_cnpj = profile.cpf
    except ObjectDoesNotExist:
        cpf_cnpj = ""

    return {
        "nome": user.name,
        "email": user.email,
        "cpfCnpj": cpf_cnpj,
        "endereco": {
            "cep": order.shipping_zip_code.replace("-", ""),
            "logradouro": order.shipping_street,
            "numero": order.shipping_number,
            "complemento": order.shipping_complement or "",
            "bairro": order.shipping_neighborhood,
            "cidade": order.shipping_city,
            "uf": order.shipping_state,
        },
    }


def build_prepostagem_payload_for_order(order) -> dict:
    return {
        "codigoServico": settings.CORREIOS_CODIGO_SERVICO,
        "codigoFormatoObjetoInformado": "1",
        "alturaInformada": "",
        "larguraInformada": "",
        "comprimentoInformado": "",
        "diametroInformado": "",
        "pesoInformado": settings.CORREIOS_PESO_PADRAO_GRAMAS,
        "remetente": build_remetente_data_from_settings(),
        "destinatario": build_destinatario_data_from_order(order),
    }


def extract_tracking_code_from_prepostagem_response(data: dict) -> str | None:
    objeto = data.get("objeto") or {}
    return objeto.get("codigoObjeto") or data.get("codigoObjeto")


def fetch_prepostagem_details_from_correios(prepostagem_id: str) -> dict:
    if settings.CORREIOS_MOCK_ENABLED:
        return correios_mock.mock_prepostagem_details_response()

    response = _authenticated_request(
        "GET", f"prepostagem/v1/prepostagens/{prepostagem_id}", timeout=10
    )

    response.raise_for_status()
    return response.json()


def create_prepostagem_at_correios(order) -> dict:
    if settings.CORREIOS_MOCK_ENABLED:
        return correios_mock.mock_prepostagem_response()

    payload = build_prepostagem_payload_for_order(order)
    headers = {
        "Content-Type": "application/json",
    }

    response = _authenticated_request(
        "POST",
        "prepostagem/v1/prepostagens",
        headers=headers,
        json=payload,
        timeout=15,
    )

    if not response.ok:
        try:
            error_detail = response.json()
        except Exception:
            error_detail = response.text
        raise CorreiosPrePostagemError(
            f"Falha ao criar pré-postagem: HTTP {response.status_code} - {error_detail}"
        )

    return response.json()


def fetch_address_data_by_cep(cep: str) -> dict:
    if settings.CORREIOS_MOCK_ENABLED:
        return correios_mock.mock_cep_response(cep.replace("-", "").strip())

    response = _authenticated_request(
        "GET", f"cep/v1/enderecos/{cep.replace('-', '').strip()}", timeout=10
    )

    if response.status_code == 404:
        raise CorreiosCepNotFoundError(f"CEP {cep} não encontrado.")

    response.raise_for_status()
    return response.json()


def format_cep_address_response(raw_data: dict) -> dict:
    return {
        "cep": raw_data.get("cep"),
        "logradouro": raw_data.get("logradouro"),
        "complemento": raw_data.get("complemento"),
        "bairro": raw_data.get("bairro"),
        "cidade": raw_data.get("localidade"),
        "uf": raw_data.get("uf"),
    }


def fetch_shipping_deadline_by_service_and_ceps(
    codigo_servico: str, cep_origem: str, cep_destino: str
) -> dict:
    if settings.CORREIOS_MOCK_ENABLED:
        return correios_mock.mock_prazo_response()

    params = {
        "cepOrigem": cep_origem.replace("-", ""),
        "cepDestino": cep_destino.replace("-", ""),
    }

    response = _authenticated_request(
        "GET",
        f"prazo/v1/nacional/{codigo_servico}",
        params=params,
        timeout=10,
    )

    response.raise_for_status()
    return response.json()


def fetch_shipping_price_by_service_and_ceps(
    codigo_servico: str,
    cep_origem: str,
    cep_destino: str,
    peso_gramas: str,
    comprimento: str = "20",
    largura: str = "20",
    altura: str = "20",
    vl_declarado: str = "0",
) -> dict:
    if settings.CORREIOS_MOCK_ENABLED:
        return correios_mock.mock_preco_response(peso_gramas)

    params = {
        "cepDestino": cep_destino.replace("-", ""),
        "cepOrigem": cep_origem.replace("-", ""),
        "psObjeto": peso_gramas,
        "tpObjeto": "2",
        "comprimento": comprimento,
        "largura": largura,
        "altura": altura,
        "vlDeclarado": vl_declarado,
    }

    response = _authenticated_request(
        "GET",
        f"preco/v1/nacional/{codigo_servico}",
        params=params,
        timeout=10,
    )

    response.raise_for_status()
    return response.json()


def format_shipping_options_response(prazo_data: dict, preco_data: dict) -> dict:
    return {
        "prazo_dias": prazo_data.get("prazoEntrega"),
        "data_maxima_entrega": prazo_data.get("dataMaxima"),
        "entrega_domiciliar": prazo_data.get("entregaDomiciliar") == "S",
        "entrega_sabado": prazo_data.get("entregaSabado") == "S",
        "preco_final": preco_data.get("pcFinal"),
        "peso_cobrado": preco_data.get("psCobrado"),
    }


def fetch_agencies_by_city_and_state(
    municipio: str, uf: str, page: int = 0, size: int = 10
) -> dict:
    if settings.CORREIOS_MOCK_ENABLED:
        return correios_mock.mock_agencies_response()

    params = {
        "municipio": municipio,
        "uf": uf,
        "status": "2",
        "page": page,
        "size": size,
    }

    response = _authenticated_request(
        "GET", "agencia/v1/unidades", params=params, timeout=10
    )

    response.raise_for_status()
    return response.json()


def format_single_agency(raw_agency: dict) -> dict:
    endereco = raw_agency.get("endereco", {})
    horarios = raw_agency.get("horarios", {})
    return {
        "nome": raw_agency.get("nome"),
        "endereco": {
            "logradouro": endereco.get("logradouro"),
            "numero": endereco.get("numero"),
            "bairro": endereco.get("bairro"),
            "cidade": endereco.get("localidade"),
            "uf": endereco.get("uf"),
            "cep": endereco.get("cep"),
        },
        "horario_funcionamento": horarios.get("funcionamento"),
        "inicio_expediente": horarios.get("iniExpediente"),
        "fim_expediente": horarios.get("fimExpediente"),
    }


def format_agencies_list_response(raw_data: dict) -> dict:
    itens = raw_data.get("itens", [])
    page_info = raw_data.get("page", {})
    return {
        "agencias": [format_single_agency(agency) for agency in itens],
        "total": page_info.get("totalElements", 0),
        "paginas": page_info.get("totalPages", 0),
    }


def dispatch_order_and_get_tracking_code(order) -> str:
    prepostagem_data = create_prepostagem_at_correios(order)

    tracking_code = extract_tracking_code_from_prepostagem_response(prepostagem_data)

    if not tracking_code:
        prepostagem_id = prepostagem_data.get("id")
        if prepostagem_id:
            details = fetch_prepostagem_details_from_correios(prepostagem_id)
            tracking_code = extract_tracking_code_from_prepostagem_response(details)

    if not tracking_code:
        raise CorreiosPrePostagemError(
            "Pré-postagem criada mas código de rastreio não encontrado na resposta dos Correios."
        )

    return tracking_code
