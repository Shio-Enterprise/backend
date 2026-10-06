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
from .services import build_overview, link_anonymous_events

User = get_user_model()

PERIOD_PARAMETER = OpenApiParameter(
    name="period",
    description="Período: `mensal` (30 dias, agrupado por dia) ou `anual` (365 dias, agrupado por mês).",
    required=False,
    type=str,
    enum=[MENSAL, ANUAL],
    default=MENSAL,
)


def _get_period(request):
    period_name = request.query_params.get("period", MENSAL)
    if period_name not in PERIODS:
        raise ValidationError({"period": "Use `mensal` ou `anual`."})
    return period_name


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
        parameters=[PERIOD_PARAMETER],
        responses={
            200: OpenApiResponse(description="Métricas consolidadas do período."),
            400: OpenApiResponse(description="Período inválido."),
            403: OpenApiResponse(description="Apenas administradores."),
        },
    )
    def get(self, request):
        return Response(build_overview(_get_period(request)))


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
        parameters=[PERIOD_PARAMETER],
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
        start, _end, _group_by = resolve_period(_get_period(self.request))
        return (
            SiteEvent.objects.filter(user=user, occurred_at__gte=start)
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
