from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import generics, serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from authentication.permissions import IsStaffOrSuperUser

from .models import SiteEvent
from .periods import ANUAL, MENSAL, PERIODS, resolve_period
from .serializers import SiteEventCreateSerializer
from .services import build_overview

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
    class Meta:
        model = SiteEvent
        fields = [
            "id",
            "event_type",
            "path",
            "occurred_at",
            "product",
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
