import logging

from django.conf import settings
from drf_spectacular.utils import OpenApiParameter, OpenApiTypes, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .correios import (
    CorreiosAuthenticationError,
    CorreiosCepNotFoundError,
    CorreiosTrackingUnavailableError,
    fetch_address_data_by_cep,
    fetch_agencies_by_city_and_state,
    fetch_shipping_deadline_by_service_and_ceps,
    fetch_shipping_price_by_service_and_ceps,
    format_agencies_list_response,
    format_cep_address_response,
    format_shipping_options_response,
)

logger = logging.getLogger(__name__)


class CepLookupView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Correios"],
        summary="Consulta de Endereço por CEP",
        description="Retorna logradouro, bairro, cidade e UF a partir de um CEP válido.",
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
            503: OpenApiTypes.OBJECT,
        },
    )
    def get(self, request, cep):
        cep_clean = cep.replace("-", "").strip()
        if len(cep_clean) != 8 or not cep_clean.isdigit():
            return Response(
                {"message": "CEP inválido. Informe 8 dígitos numéricos."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            raw_data = fetch_address_data_by_cep(cep_clean)
            return Response(
                format_cep_address_response(raw_data), status=status.HTTP_200_OK
            )
        except CorreiosCepNotFoundError:
            return Response(
                {"message": f"CEP {cep} não encontrado."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except (
            CorreiosAuthenticationError,
            CorreiosTrackingUnavailableError,
            Exception,
        ):
            logger.exception("Falha ao consultar CEP %s nos Correios", cep)
            return Response(
                {"message": "Serviço de CEP temporariamente indisponível."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )


class ShippingOptionsView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Correios"],
        summary="Cálculo de Frete (Preço e Prazo)",
        description=(
            "Consulta preço e prazo de entrega para um CEP de destino.\n\n"
            "- `cep_destino` — obrigatório\n"
            "- `cep_origem` — opcional, usa o CEP do remetente configurado no servidor\n"
            "- `peso` — peso em gramas (opcional, usa o padrão configurado)\n"
            "- `codigo_servico` — código do serviço Correios (opcional, usa o padrão configurado)\n"
        ),
        parameters=[
            OpenApiParameter(name="cep_destino", type=OpenApiTypes.STR, required=True),
            OpenApiParameter(name="cep_origem", type=OpenApiTypes.STR, required=False),
            OpenApiParameter(name="peso", type=OpenApiTypes.STR, required=False),
            OpenApiParameter(
                name="codigo_servico", type=OpenApiTypes.STR, required=False
            ),
        ],
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            503: OpenApiTypes.OBJECT,
        },
    )
    def get(self, request):
        cep_destino = (
            request.query_params.get("cep_destino", "").replace("-", "").strip()
        )
        cep_origem = (
            request.query_params.get("cep_origem", settings.CORREIOS_REMETENTE_CEP)
            .replace("-", "")
            .strip()
        )
        peso = request.query_params.get("peso", settings.CORREIOS_PESO_PADRAO_GRAMAS)
        codigo_servico = request.query_params.get(
            "codigo_servico", settings.CORREIOS_CODIGO_SERVICO
        )

        if not cep_destino or len(cep_destino) != 8 or not cep_destino.isdigit():
            return Response(
                {
                    "message": "cep_destino é obrigatório e deve ter 8 dígitos numéricos."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            prazo_data = fetch_shipping_deadline_by_service_and_ceps(
                codigo_servico, cep_origem, cep_destino
            )
            preco_data = fetch_shipping_price_by_service_and_ceps(
                codigo_servico, cep_origem, cep_destino, peso
            )
            return Response(
                format_shipping_options_response(prazo_data, preco_data),
                status=status.HTTP_200_OK,
            )
        except (CorreiosAuthenticationError, Exception):
            logger.exception("Falha ao calcular frete nos Correios")
            return Response(
                {
                    "message": "Serviço de cálculo de frete temporariamente indisponível."
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )


class AgencySearchView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Correios"],
        summary="Busca de Agências dos Correios",
        description="Lista agências dos Correios por município e UF.",
        parameters=[
            OpenApiParameter(name="municipio", type=OpenApiTypes.STR, required=True),
            OpenApiParameter(name="uf", type=OpenApiTypes.STR, required=True),
            OpenApiParameter(name="page", type=OpenApiTypes.INT, required=False),
            OpenApiParameter(name="size", type=OpenApiTypes.INT, required=False),
        ],
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            503: OpenApiTypes.OBJECT,
        },
    )
    def get(self, request):
        municipio = request.query_params.get("municipio", "").strip()
        uf = request.query_params.get("uf", "").strip().upper()

        if not municipio or not uf:
            return Response(
                {"message": "Os parâmetros 'municipio' e 'uf' são obrigatórios."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            page = int(request.query_params.get("page", 0))
            size = int(request.query_params.get("size", 10))
        except ValueError:
            return Response(
                {
                    "message": "Os parâmetros 'page' e 'size' devem ser números inteiros."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            raw_data = fetch_agencies_by_city_and_state(municipio, uf, page, size)
            return Response(
                format_agencies_list_response(raw_data), status=status.HTTP_200_OK
            )
        except (CorreiosAuthenticationError, Exception):
            logger.exception("Falha ao buscar agências nos Correios")
            return Response(
                {
                    "message": "Serviço de busca de agências temporariamente indisponível."
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
