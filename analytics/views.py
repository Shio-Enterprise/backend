from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import generics, serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from authentication.permissions import IsStaffOrSuperUser

from .models import SiteEvent
from .periods import ANUAL, MENSAL, PERIODS, resolve_period
from .serializers import (
    ANONYMOUS_ID_REQUIRED,
    AnalyticsConsentCreateSerializer,
    SiteEventCreateSerializer,
)
from .services import (
    build_overview,
    current_consent,
    has_analytics_consent,
    link_anonymous_events,
    record_analytics_consent,
)

User = get_user_model()

CONSENT_REQUIRED_MESSAGE = "Registro de navegação não autorizado pelo visitante."


def _require_consent(user, anonymous_id):
    if settings.ANALYTICS_CONSENT_ENFORCED and not has_analytics_consent(
        user, anonymous_id
    ):
        raise PermissionDenied(CONSENT_REQUIRED_MESSAGE)


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
            "`anonymous_id`. Usuários autenticados têm o evento associado à conta. "
            "Com `ANALYTICS_CONSENT_ENFORCED` ativo, exige aceite registrado na "
            "versão vigente do banner."
        ),
        request=SiteEventCreateSerializer,
        responses={
            201: OpenApiResponse(description="Evento registrado."),
            400: OpenApiResponse(description="Dados inválidos."),
            403: OpenApiResponse(description="Sem consentimento de navegação."),
        },
    )
    def post(self, request):
        serializer = SiteEventCreateSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        _require_consent(request.user, data.get("anonymous_id"))
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
            "registrados antes do login com o mesmo `anonymous_id`, junto com o "
            "histórico de consentimento desse navegador. Só deve ser chamado quando o "
            "visitante consentiu com o registro de navegação; com "
            "`ANALYTICS_CONSENT_ENFORCED` ativo, o aceite é conferido aqui."
        ),
        request=LinkAnonymousEventsSerializer,
        responses={
            200: OpenApiResponse(description="Quantidade de eventos vinculados."),
            400: OpenApiResponse(description="Identificador inválido."),
            401: OpenApiResponse(description="Login necessário."),
            403: OpenApiResponse(description="Sem consentimento de navegação."),
        },
    )
    def post(self, request):
        serializer = LinkAnonymousEventsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        anonymous_id = serializer.validated_data["anonymous_id"]
        _require_consent(request.user, anonymous_id)
        linked = link_anonymous_events(request.user, anonymous_id)
        return Response({"linked": linked})


ANONYMOUS_ID_PARAMETER = OpenApiParameter(
    name="anonymous_id",
    description="Identificador anônimo do navegador. Obrigatório para visitantes sem login.",
    required=False,
    type=str,
)


def _consent_payload(consent):
    return {
        "choice": consent.choice if consent else None,
        "version": consent.version if consent else None,
        "current_version": settings.ANALYTICS_CONSENT_VERSION,
        "created_at": consent.created_at.isoformat() if consent else None,
    }


class AnalyticsConsentView(APIView):
    """Consulta e registra a escolha do banner de privacidade (navegação)."""

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Analytics"],
        summary="Consultar consentimento de navegação",
        description=(
            "Retorna a escolha mais recente da conta autenticada ou, para visitantes, "
            "do `anonymous_id` informado. `choice` é `null` quando não há escolha "
            "registrada. Se `version` for diferente de `current_version`, o banner "
            "deve ser exibido de novo."
        ),
        parameters=[ANONYMOUS_ID_PARAMETER],
        responses={
            200: OpenApiResponse(description="Escolha vigente."),
            400: OpenApiResponse(description="Identificador anônimo ausente."),
        },
    )
    def get(self, request):
        anonymous_id = request.query_params.get("anonymous_id", "").strip()
        if not request.user.is_authenticated and not anonymous_id:
            raise ValidationError(ANONYMOUS_ID_REQUIRED)
        consent = current_consent(request.user, anonymous_id or None)
        return Response(_consent_payload(consent))

    @extend_schema(
        tags=["Analytics"],
        summary="Registrar consentimento de navegação",
        description=(
            "Registra `granted` (aceito) ou `denied` (recusado) na versão vigente do "
            "banner, mantendo o histórico. Ao recusar, os eventos já gravados da conta "
            "e deste navegador são anonimizados."
        ),
        request=AnalyticsConsentCreateSerializer,
        responses={
            201: OpenApiResponse(description="Escolha registrada."),
            400: OpenApiResponse(description="Dados inválidos."),
        },
    )
    def post(self, request):
        serializer = AnalyticsConsentCreateSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        consent = record_analytics_consent(
            data["choice"], request.user, data.get("anonymous_id") or None
        )
        return Response(_consent_payload(consent), status=status.HTTP_201_CREATED)


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
