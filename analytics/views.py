from django.contrib.auth import get_user_model
from django.db.models import Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import generics, serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from authentication.permissions import IsStaffOrSuperUser

from .models import SiteEvent
from .periods import ANUAL, MENSAL, PERIODS, resolve_period
from .serializers import SiteEventCreateSerializer
from .services import (
    build_overview,
    build_product_access,
    build_shared_interest,
    build_user_products,
    link_anonymous_events,
    search_products,
)

User = get_user_model()

PERIOD_PARAMETER = OpenApiParameter(
    name="period",
    description="Período: `mensal` (30 dias, agrupado por dia) ou `anual` (365 dias, agrupado por mês).",
    required=False,
    type=str,
    enum=[MENSAL, ANUAL],
    default=MENSAL,
)


YEAR_PARAMETER = OpenApiParameter(
    name="year",
    description="Ano do calendário. No anual, define o ano inteiro. No mensal, acompanha `month`.",
    required=False,
    type=int,
)
MONTH_PARAMETER = OpenApiParameter(
    name="month",
    description="Mês de 1 a 12. Com `year`, o mensal passa a ser esse mês, agrupado por dia.",
    required=False,
    type=int,
)
CALENDAR_PARAMETERS = [PERIOD_PARAMETER, YEAR_PARAMETER, MONTH_PARAMETER]


def _get_period(request):
    period_name = request.query_params.get("period", MENSAL)
    if period_name not in PERIODS:
        raise ValidationError({"period": "Use `mensal` ou `anual`."})
    return period_name


def _optional_int(request, name):
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValidationError({name: "Valor inválido."})


def _selection(request):
    period_name = _get_period(request)
    year = _optional_int(request, "year")
    month = _optional_int(request, "month")
    try:
        resolve_period(period_name, year=year, month=month)
    except ValueError as exc:
        message = str(exc)
        if message.startswith("Mês") or message.startswith("Esse"):
            field = "month"
        elif message.startswith("Ano"):
            field = "year"
        else:
            field = "period"
        raise ValidationError({field: message})
    return period_name, year, month


def _get_page(request):
    raw = request.query_params.get("page", "1")
    try:
        page = int(raw)
    except (TypeError, ValueError):
        raise ValidationError({"page": "Página inválida."})
    if page < 1:
        raise ValidationError({"page": "Página inválida."})
    return page


class SiteEventCreateView(APIView):
    """Registra um evento de navegação enviado pelo site. Público."""

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Analytics"],
        summary="Registrar evento do site",
        description=(
            "Registra um evento de navegação (visualização de página, de produto, "
            "carrinho e checkout). Não exige login. Visitantes sem login devem enviar "
            "`anonymous_id`. Usuários autenticados têm o evento associado à conta."
        ),
        request=SiteEventCreateSerializer,
        responses={
            201: OpenApiResponse(description="Evento registrado."),
            400: OpenApiResponse(description="Dados inválidos."),
        },
    )
    def post(self, request):
        serializer = SiteEventCreateSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        SiteEvent.objects.create(
            event_type=data["event_type"],
            user=request.user if request.user.is_authenticated else None,
            anonymous_id=data.get("anonymous_id") or None,
            product=data.get("product"),
            variation=data.get("variation"),
            path=data.get("path", ""),
        )
        return Response(status=status.HTTP_201_CREATED)


class LinkAnonymousEventsSerializer(serializers.Serializer):
    anonymous_id = serializers.CharField(max_length=64)


class LinkAnonymousEventsView(APIView):
    """Vincula a navegação anônima deste navegador à conta que acabou de fazer login."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Analytics"],
        summary="Vincular eventos anônimos à conta",
        description=(
            "Chamado logo após o login. Associa à conta autenticada os eventos que foram "
            "registrados antes do login com o mesmo `anonymous_id`. Só deve ser chamado "
            "quando o visitante consentiu com o registro de navegação."
        ),
        request=LinkAnonymousEventsSerializer,
        responses={
            200: OpenApiResponse(description="Quantidade de eventos vinculados."),
            400: OpenApiResponse(description="Identificador inválido."),
            401: OpenApiResponse(description="Login necessário."),
        },
    )
    def post(self, request):
        serializer = LinkAnonymousEventsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        linked = link_anonymous_events(
            request.user, serializer.validated_data["anonymous_id"]
        )
        return Response({"linked": linked})


class SiteEventOverviewView(APIView):
    """Visão geral do comportamento no site, usada pelo dashboard admin."""

    permission_classes = [IsStaffOrSuperUser]

    @extend_schema(
        tags=["Analytics"],
        summary="Visão geral de métricas de comportamento",
        description=(
            "Retorna visitantes únicos (cadastrados e anônimos), total de eventos por "
            "tipo, série temporal no período e os produtos mais visualizados. "
            "Mensal: 30 dias agrupados por dia. Anual: 365 dias agrupados por mês. "
            "Período calculado no fuso America/Sao_Paulo."
        ),
        parameters=CALENDAR_PARAMETERS,
        responses={
            200: OpenApiResponse(description="Métricas consolidadas do período."),
            400: OpenApiResponse(description="Período inválido."),
            403: OpenApiResponse(description="Apenas administradores."),
        },
    )
    def get(self, request):
        period_name, year, month = _selection(request)
        return Response(build_overview(period_name, year=year, month=month))


class SharedProductInterestView(APIView):
    """Produtos que reuniram as mesmas contas, pela navegação do período."""

    permission_classes = [IsStaffOrSuperUser]

    @extend_schema(
        tags=["Analytics"],
        summary="Interesse em comum por produto",
        description=(
            "Agrupa as contas que viram, colocaram no carrinho, iniciaram o checkout "
            "ou compraram o mesmo produto no período. Cada conta aparece com o sinal mais forte. "
            "Visitantes sem conta entram só na contagem."
        ),
        parameters=CALENDAR_PARAMETERS,
        responses={
            200: OpenApiResponse(description="Produtos e as contas reunidas em cada um."),
            400: OpenApiResponse(description="Período inválido."),
            403: OpenApiResponse(description="Apenas administradores."),
        },
    )
    def get(self, request):
        period_name, year, month = _selection(request)
        return Response({"products": build_shared_interest(period_name, year=year, month=month)})


class ProductSearchView(APIView):
    """Busca produtos pelo nome para o filtro de acessos."""

    permission_classes = [IsStaffOrSuperUser]

    @extend_schema(
        tags=["Analytics"],
        summary="Buscar produtos pelo nome",
        parameters=[
            OpenApiParameter(
                name="q",
                description="Trecho do nome do produto.",
                required=True,
                type=str,
            )
        ],
        responses={
            200: OpenApiResponse(description="Até 10 produtos, com id e nome."),
            400: OpenApiResponse(description="Texto de busca curto demais."),
            403: OpenApiResponse(description="Apenas administradores."),
        },
    )
    def get(self, request):
        query = request.query_params.get("q", "").strip()
        if len(query) < 2:
            raise ValidationError({"q": "Informe pelo menos 2 caracteres."})
        return Response(search_products(query))


class ProductAccessView(APIView):
    """Acessos de um produto no período: ação, horário, conta e variação."""

    permission_classes = [IsStaffOrSuperUser]

    @extend_schema(
        tags=["Analytics"],
        summary="Acessos de um produto",
        parameters=CALENDAR_PARAMETERS,
        responses={
            200: OpenApiResponse(description="Totais por ação e a página de acessos."),
            400: OpenApiResponse(description="Período ou página inválidos."),
            403: OpenApiResponse(description="Apenas administradores."),
            404: OpenApiResponse(description="Produto não encontrado."),
        },
    )
    def get(self, request, product_id):
        period_name, year, month = _selection(request)
        payload = build_product_access(
            product_id, period_name, _get_page(request), year=year, month=month
        )
        if payload is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(payload)


class UserProductAccessView(APIView):
    """Produtos que uma conta acessou no período."""

    permission_classes = [IsStaffOrSuperUser]

    @extend_schema(
        tags=["Analytics"],
        summary="Produtos acessados por uma conta",
        parameters=CALENDAR_PARAMETERS,
        responses={
            200: OpenApiResponse(description="Produtos com a contagem de cada ação."),
            400: OpenApiResponse(description="Período inválido."),
            403: OpenApiResponse(description="Apenas administradores."),
            404: OpenApiResponse(description="Usuário não encontrado."),
        },
    )
    def get(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)
        period_name, year, month = _selection(request)
        return Response({"products": build_user_products(user, period_name, year=year, month=month)})


class SiteEventTimelineSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(
        source="product.name", read_only=True, default=None
    )

    class Meta:
        model = SiteEvent
        fields = [
            "id",
            "event_type",
            "path",
            "occurred_at",
            "product",
            "product_name",
            "variation",
        ]
        read_only_fields = fields


class UserSiteEventTimelineView(generics.ListAPIView):
    """Linha do tempo de eventos de um usuário específico, para o admin."""

    permission_classes = [IsStaffOrSuperUser]
    serializer_class = SiteEventTimelineSerializer

    @extend_schema(
        tags=["Analytics"],
        summary="Linha do tempo de eventos de um usuário",
        description=(
            "Lista os eventos de navegação de um usuário cadastrado no período, do "
            "mais recente para o mais antigo, com paginação."
        ),
        parameters=CALENDAR_PARAMETERS,
        responses={
            200: SiteEventTimelineSerializer(many=True),
            400: OpenApiResponse(description="Período inválido."),
            403: OpenApiResponse(description="Apenas administradores."),
            404: OpenApiResponse(description="Usuário não encontrado."),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return SiteEvent.objects.none()
        user = get_object_or_404(User, pk=self.kwargs["user_id"])
        period_name, year, month = _selection(self.request)
        start, end, _group_by = resolve_period(period_name, year=year, month=month)
        return (
            SiteEvent.objects.filter(user=user, occurred_at__gte=start, occurred_at__lte=end)
            .select_related("product", "variation")
            .order_by("-occurred_at")
        )


class UserSearchResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "name", "email"]
        read_only_fields = fields


class UserSearchView(generics.ListAPIView):
    """Busca usuários por nome ou e-mail, para escolher a linha do tempo no dashboard."""

    permission_classes = [IsStaffOrSuperUser]
    serializer_class = UserSearchResultSerializer
    pagination_class = None
    MIN_QUERY_LENGTH = 2
    MAX_RESULTS = 10

    @extend_schema(
        tags=["Analytics"],
        summary="Buscar usuários por nome ou e-mail",
        description=(
            "Retorna até 10 usuários cujo nome ou e-mail contém o texto informado "
            "(sem diferenciar maiúsculas). O texto precisa ter pelo menos 2 caracteres."
        ),
        parameters=[
            OpenApiParameter(
                name="q",
                description="Trecho do nome ou do e-mail.",
                required=True,
                type=str,
            )
        ],
        responses={
            200: UserSearchResultSerializer(many=True),
            400: OpenApiResponse(description="Texto de busca curto demais."),
            403: OpenApiResponse(description="Apenas administradores."),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return User.objects.none()
        query = self.request.query_params.get("q", "").strip()
        if len(query) < self.MIN_QUERY_LENGTH:
            raise ValidationError(
                {"q": f"Informe pelo menos {self.MIN_QUERY_LENGTH} caracteres."}
            )
        return User.objects.filter(
            Q(name__icontains=query) | Q(email__icontains=query)
        ).order_by("name", "email")[: self.MAX_RESULTS]
