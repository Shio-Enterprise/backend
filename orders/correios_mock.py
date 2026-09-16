"""Respostas simuladas da API dos Correios.

Usadas quando ``settings.CORREIOS_MOCK_ENABLED`` é True, permitindo que todo o
fluxo (cálculo de frete, CEP, pré-postagem, rastreio e agências) funcione sem
credenciais reais. As estruturas imitam o JSON cru retornado pelos Correios,
para que as funções de formatação em ``correios.py`` continuem sendo exercidas.
"""

from datetime import UTC, datetime, timedelta

MOCK_TRACKING_CODE = "AA123456785BR"


def mock_token_response() -> str:
    return "mock-correios-token"


def mock_cep_response(cep: str) -> dict:
    return {
        "cep": cep,
        "logradouro": "Rua Exemplo Simulada",
        "complemento": "",
        "bairro": "Centro",
        "localidade": "Brasília",
        "uf": "DF",
    }


def mock_prazo_response() -> dict:
    data_maxima = (datetime.now(tz=UTC) + timedelta(days=5)).strftime("%d/%m/%Y")
    return {
        "prazoEntrega": 5,
        "dataMaxima": data_maxima,
        "entregaDomiciliar": "S",
        "entregaSabado": "N",
    }


def mock_preco_response(peso_gramas: str) -> dict:
    return {
        "pcFinal": "25,90",
        "psCobrado": str(peso_gramas),
    }


def mock_prepostagem_response() -> dict:
    return {
        "id": "mock-prepostagem-id",
        "codigoObjeto": MOCK_TRACKING_CODE,
        "objeto": {"codigoObjeto": MOCK_TRACKING_CODE},
    }


def mock_prepostagem_details_response() -> dict:
    return {
        "id": "mock-prepostagem-id",
        "objeto": {"codigoObjeto": MOCK_TRACKING_CODE},
    }


def mock_tracking_response(tracking_code: str) -> dict:
    now = datetime.now(tz=UTC)
    return {
        "objetos": [
            {
                "codObjeto": tracking_code,
                "dtPrevista": (now + timedelta(days=3)).isoformat(),
                "eventos": [
                    {
                        "dtHrCriado": now.isoformat(),
                        "descricao": "Objeto postado",
                        "detalhe": "",
                        "unidade": {
                            "endereco": {"cidade": "Brasília", "uf": "DF"},
                        },
                    }
                ],
            }
        ]
    }


def mock_agencies_response() -> dict:
    return {
        "itens": [
            {
                "nome": "Agência Central Simulada",
                "endereco": {
                    "logradouro": "Praça Exemplo",
                    "numero": "100",
                    "bairro": "Centro",
                    "localidade": "Brasília",
                    "uf": "DF",
                    "cep": "70000000",
                },
                "horarios": {
                    "funcionamento": "Segunda a Sexta",
                    "iniExpediente": "09:00",
                    "fimExpediente": "17:00",
                },
            }
        ],
        "page": {"totalElements": 1, "totalPages": 1},
    }
