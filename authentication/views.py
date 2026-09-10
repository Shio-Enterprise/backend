import logging

from django.contrib.auth import get_user_model
from django.db import models
from django.db.models import Count, Max, Q, Sum
from django.db.models.functions import Coalesce
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import filters, status, viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from authentication.signals import google_login_completed
from orders.metrics import SALES_ORDER_STATUSES

from .permissions import IsStaffOrSuperUser
from .serializers import (
    AddressSerializer,
    CustomerCRMDetailSerializer,
    CustomerCRMSerializer,
    GoogleAuthSerializer,
    LogoutInputSerializer,
    PasswordLoginSerializer,
    RegisterSerializer,
    TokenRefreshInputSerializer,
    UserSerializer,
)
from .services import GoogleAuthService, InvalidGoogleTokenException

logger = logging.getLogger(__name__)
User = get_user_model()


def get_tokens_for_user(user) -> dict:
    """Gera um par de tokens JWT (access + refresh) para o utilizador."""
    refresh = RefreshToken.for_user(user)
    return {
        "refresh": str(refresh),
        "access": str(refresh.access_token),
    }


_AUTH_SUCCESS_EXAMPLE_NEW = OpenApiExample(
    name="Novo utilizador (201)",
    summary="Primeiro login — conta criada",
    value={
        "access": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
        "refresh": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
        "is_new_user": True,
        "user": {
            "id": 1,
            "email": "user@gmail.com",
            "name": "Nome Completo",
            "avatar_url": "https://lh3.googleusercontent.com/...",
            "is_new_user": True,
            "created_at": "2024-01-01T12:00:00Z",
            "updated_at": "2024-01-01T12:00:00Z",
        },
    },
    response_only=True,
    status_codes=["201"],
)

_AUTH_SUCCESS_EXAMPLE_EXISTING = OpenApiExample(
    name="Utilizador existente (200)",
    summary="Login recorrente",
    value={
        "access": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
        "refresh": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
        "is_new_user": False,
        "user": {
            "id": 1,
            "email": "user@gmail.com",
            "name": "Nome Completo",
            "avatar_url": "https://lh3.googleusercontent.com/...",
            "is_new_user": False,
            "created_at": "2024-01-01T12:00:00Z",
            "updated_at": "2024-01-15T08:30:00Z",
        },
    },
    response_only=True,
    status_codes=["200"],
)


# ─── Views ────────────────────────────────────────────────────────────────────


class GoogleLoginView(APIView):
    """Login com Google OAuth 2.0."""

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(
        tags=["Auth"],
        summary="Login com Google",
        description=(
            "Recebe o `id_token` retornado pelo Google Sign-In e autentica o utilizador.\n\n"
            "**Fluxo:**\n"
            "1. O frontend inicia o Google Sign-In\n"
            "2. O Google retorna um `credential` (id_token JWT)\n"
            "3. O frontend envia esse token para este endpoint\n"
            "4. O backend verifica junto ao Google e retorna tokens JWT + dados do user\n\n"
            "**Comportamento:**\n"
            "- Primeiro login → cria conta e retorna `201 Created` com `is_new_user: true`\n"
            "- Login recorrente → retorna `200 OK` com `is_new_user: false`\n"
            "- Email já existe sem Google → vincula a conta ao Google e retorna `200 OK`"
        ),
        request=GoogleAuthSerializer,
        responses={
            200: OpenApiResponse(
                description="Login bem-sucedido — utilizador existente",
                examples=[_AUTH_SUCCESS_EXAMPLE_EXISTING],
            ),
            201: OpenApiResponse(
                description="Login bem-sucedido — novo utilizador criado",
                examples=[_AUTH_SUCCESS_EXAMPLE_NEW],
            ),
            400: OpenApiResponse(
                description="Pedido inválido — `id_token` em falta ou malformado",
                examples=[
                    OpenApiExample(
                        name="Token em falta",
                        value={
                            "error": "id_token é obrigatório.",
                            "details": {"id_token": ["This field is required."]},
                        },
                        response_only=True,
                        status_codes=["400"],
                    )
                ],
            ),
            401: OpenApiResponse(
                description="Token Google inválido ou expirado",
                examples=[
                    OpenApiExample(
                        name="Token inválido",
                        value={
                            "error": "Token Google inválido ou expirado.",
                            "details": "Token invalid: ...",
                        },
                        response_only=True,
                        status_codes=["401"],
                    )
                ],
            ),
            500: OpenApiResponse(description="Erro interno do servidor"),
        },
        examples=[
            OpenApiExample(
                name="Payload de login",
                summary="Enviar id_token do Google",
                value={"id_token": "<google-id-token-jwt>"},
                request_only=True,
            )
        ],
    )
    def post(self, request):
        serializer = GoogleAuthSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(
                {"error": "id_token é obrigatório.", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        id_token_str = serializer.validated_data["id_token"]

        try:
            user, is_new_user = GoogleAuthService.authenticate_or_create_user(
                id_token_str
            )
        except InvalidGoogleTokenException as e:
            logger.warning(f"Tentativa de login com token inválido: {e}")
            return Response(
                {"error": "Token Google inválido ou expirado.", "details": str(e)},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except Exception as e:
            logger.error(f"Erro inesperado no login com Google: {e}", exc_info=True)
            return Response(
                {"error": "Erro interno no servidor. Tente novamente."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        try:
            google_login_completed.send(sender=None, request=request, user=user)
        except Exception:
            logger.exception("Erro ao executar handlers de login (merge de carrinho)")

        tokens = get_tokens_for_user(user)
        user_data = UserSerializer(user).data
        response_status = status.HTTP_201_CREATED if is_new_user else status.HTTP_200_OK

        return Response(
            {
                "access": tokens["access"],
                "refresh": tokens["refresh"],
                "is_new_user": is_new_user,
                "user": user_data,
            },
            status=response_status,
        )


class RegisterView(APIView):
    """Registo de novo utilizador."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()
            tokens = get_tokens_for_user(user)
            user_data = UserSerializer(user).data
            return Response(
                {
                    "access": tokens["access"],
                    "refresh": tokens["refresh"],
                    "is_new_user": True,
                    "user": user_data,
                },
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class PasswordLoginView(APIView):
    """Login com Email e Senha."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = PasswordLoginSerializer(data=request.data, context={"request": request})
        if serializer.is_valid():
            user = serializer.validated_data["user"]
            tokens = get_tokens_for_user(user)
            user_data = UserSerializer(user).data
            return Response(
                {
                    "access": tokens["access"],
                    "refresh": tokens["refresh"],
                    "is_new_user": False,
                    "user": user_data,
                },
                status=status.HTTP_200_OK,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class TokenRefreshView(APIView):
    """Renovação do access token."""

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(
        tags=["Auth"],
        summary="Renovar access token",
        description=(
            "Usa o `refresh` token para gerar um novo `access` token.\n\n"
            "O `refresh` token tem validade de **7 dias**. O `access` token gerado tem validade de **1 hora**.\n\n"
            "> ⚠️ Com `ROTATE_REFRESH_TOKENS = True`, o refresh token antigo é invalidado e um novo é gerado."
        ),
        request=TokenRefreshInputSerializer,
        responses={
            200: OpenApiResponse(
                description="Novo access token gerado",
                examples=[
                    OpenApiExample(
                        name="Token renovado",
                        value={"access": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."},
                        response_only=True,
                        status_codes=["200"],
                    )
                ],
            ),
            400: OpenApiResponse(description="Campo `refresh` em falta"),
            401: OpenApiResponse(description="Refresh token inválido ou expirado"),
        },
        examples=[
            OpenApiExample(
                name="Payload",
                value={"refresh": "<refresh-token-jwt>"},
                request_only=True,
            )
        ],
    )
    def post(self, request):
        refresh_token = request.data.get("refresh")

        if not refresh_token:
            return Response(
                {"error": "refresh token é obrigatório."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
            user = User.objects.get(id=token["user_id"])
            new_token = RefreshToken.for_user(user)
            return Response({
                "access": str(new_token.access_token),
                "refresh": str(new_token)
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.warning(f"Refresh token inválido: {e}")
            return Response(
                {"error": "Refresh token inválido ou expirado."},
                status=status.HTTP_401_UNAUTHORIZED,
            )


class LogoutView(APIView):
    """Terminar sessão."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Auth"],
        summary="Logout",
        description=(
            "Invalida o `refresh` token, terminando a sessão do utilizador.\n\n"
            "O token é adicionado à **blacklist** do SimpleJWT e não pode mais ser usado.\n\n"
            "**Requer:** `Authorization: Bearer <access_token>` no header."
        ),
        request=LogoutInputSerializer,
        responses={
            200: OpenApiResponse(
                description="Sessão terminada",
                examples=[
                    OpenApiExample(
                        name="Sucesso",
                        value={"message": "Sessão terminada com sucesso."},
                        response_only=True,
                        status_codes=["200"],
                    )
                ],
            ),
            400: OpenApiResponse(
                description="Campo `refresh` em falta ou token já expirado"
            ),
            401: OpenApiResponse(
                description="Não autenticado — access token inválido ou em falta"
            ),
        },
        examples=[
            OpenApiExample(
                name="Payload",
                value={"refresh": "<refresh-token-jwt>"},
                request_only=True,
            )
        ],
    )
    def post(self, request):
        refresh_token = request.data.get("refresh")

        if not refresh_token:
            return Response(
                {"error": "refresh token é obrigatório."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
            return Response(
                {"message": "Sessão terminada com sucesso."},
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            logger.warning(f"Erro ao fazer logout: {e}")
            return Response(
                {"error": "Token inválido ou já expirado."},
                status=status.HTTP_400_BAD_REQUEST,
            )


class MeView(APIView):
    """Dados do utilizador autenticado."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Users"],
        summary="Perfil do utilizador autenticado",
        description=(
            "Retorna os dados do utilizador associado ao JWT enviado no header.\n\n"
            "**Requer:** `Authorization: Bearer <access_token>` no header.\n\n"
            "Use este endpoint para:\n"
            "- Preencher o estado global do utilizador no frontend após login\n"
            "- Exibir nome e avatar no header da plataforma\n"
            "- Verificar se o utilizador está activo"
        ),
        responses={
            200: OpenApiResponse(
                response=UserSerializer,
                description="Dados do utilizador",
                examples=[
                    OpenApiExample(
                        name="Utilizador autenticado",
                        value={
                            "id": 1,
                            "email": "user@gmail.com",
                            "name": "Nome Completo",
                            "avatar_url": "https://lh3.googleusercontent.com/...",
                            "is_new_user": False,
                            "created_at": "2024-01-01T12:00:00Z",
                            "updated_at": "2024-01-15T08:30:00Z",
                        },
                        response_only=True,
                        status_codes=["200"],
                    )
                ],
            ),
            401: OpenApiResponse(
                description="Não autenticado — access token inválido ou em falta"
            ),
        },
    )
    def get(self, request):
        serializer = UserSerializer(request.user)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def patch(self, request):
        user = request.user
        serializer = UserSerializer(user, data=request.data, partial=True)

        if not serializer.is_valid():
            return Response(
                {"error": "Dados inválidos.", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Salva campos do User e do perfil aninhado (phone_number, cpf).
        try:
            serializer.save()
        except models.IntegrityError as exc:
            return Response(
                {"error": "Erro de integridade ao atualizar o perfil.", "details": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(UserSerializer(user).data, status=status.HTTP_200_OK)


class AddressListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        addresses = request.user.addresses.all().order_by("-is_default", "-created_at")
        return Response(AddressSerializer(addresses, many=True).data)

    def post(self, request):
        serializer = AddressSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if serializer.validated_data.get("is_default"):
            request.user.addresses.filter(is_default=True).update(is_default=False)

        address = serializer.save(user=request.user)
        return Response(AddressSerializer(address).data, status=status.HTTP_201_CREATED)


class AddressDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_object(self, request, pk):
        return request.user.addresses.filter(id=pk).first()

    def patch(self, request, pk):
        address = self.get_object(request, pk)
        if not address:
            return Response({"message": "Endereço não encontrado."}, status=status.HTTP_404_NOT_FOUND)

        serializer = AddressSerializer(address, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        if serializer.validated_data.get("is_default"):
            request.user.addresses.filter(is_default=True).update(is_default=False)

        serializer.save()
        return Response(serializer.data)

    def delete(self, request, pk):
        address = self.get_object(request, pk)
        if not address:
            return Response({"message": "Endereço não encontrado."}, status=status.HTTP_404_NOT_FOUND)
        address.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CustomerCRMViewSet(viewsets.ReadOnlyModelViewSet):
    """UC07 – Gerenciar Clientes (CRM)"""

    permission_classes = [IsStaffOrSuperUser]
    filter_backends = [
        DjangoFilterBackend,
        filters.OrderingFilter,
        filters.SearchFilter,
    ]

    search_fields = ["name", "email"]
    ordering_fields = [
        "created_at",
        "total_orders",
        "total_spent",
        "last_purchase_date",
    ]
    ordering = ["-created_at"]

    filterset_fields = {
        "created_at": ["gte", "lte", "exact"],
    }

    def get_queryset(self):
        sales_orders = Q(orders__status__in=SALES_ORDER_STATUSES)
        qs = User.objects.filter(profile__role="CUSTOMER").annotate(
            total_orders=Count("orders", filter=sales_orders),
            total_spent=Coalesce(
                Sum("orders__total_amount", filter=sales_orders),
                0.0,
                output_field=models.DecimalField(),
            ),
            last_purchase_date=Max("orders__created_at", filter=sales_orders),
        )

        min_freq = self.request.query_params.get("min_frequency")
        max_freq = self.request.query_params.get("max_frequency")

        if min_freq is not None:
            qs = qs.filter(total_orders__gte=min_freq)
        if max_freq is not None:
            qs = qs.filter(total_orders__lte=max_freq)

        return qs

    def get_serializer_class(self):
        if self.action == "retrieve":
            return CustomerCRMDetailSerializer
        return CustomerCRMSerializer
