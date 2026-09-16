"""
Serviço de autenticação com Google OAuth 2.0.

Fluxo completo:
1. O frontend inicia o Google Sign-In e recebe um `id_token` (credential).
2. O frontend envia esse token para o backend via POST /api/auth/google/.
3. Este serviço verifica o token junto à API do Google.
4. Se válido, extrai email, nome e google_id do payload.
5. Cria o utilizador no banco se for a primeira vez, ou busca o existente.
6. Retorna o utilizador e se é um registo novo.
"""

import logging

from django.conf import settings
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from .models import User, UserProfile

logger = logging.getLogger(__name__)


class GoogleAuthException(Exception):
    """Exceção base para erros de autenticação com Google."""

    pass


class InvalidGoogleTokenException(GoogleAuthException):
    """Token do Google inválido, expirado ou de cliente incorreto."""

    pass


class GoogleAuthService:
    """
    Serviço responsável por verificar tokens do Google e
    gerir o ciclo de vida do utilizador na plataforma.
    """

    @staticmethod
    def verify_google_token(token: str) -> dict:
        """
        Verifica o id_token junto à Google e retorna o payload.

        Args:
            token: O id_token JWT fornecido pelo Google Sign-In.

        Returns:
            dict com os dados do utilizador (email, name, sub, picture, etc.)

        Raises:
            InvalidGoogleTokenException: Se o token for inválido ou expirado.
        """
        try:
            id_info = id_token.verify_oauth2_token(
                token,
                google_requests.Request(),
                settings.GOOGLE_CLIENT_ID,
                clock_skew_in_seconds=10,  # Tolera 10s de diferença de relógio
            )

            # Garantia extra: verificar que o token foi emitido para a nossa app
            if id_info.get("aud") != settings.GOOGLE_CLIENT_ID:
                raise InvalidGoogleTokenException(
                    "Token não foi emitido para esta aplicação."
                )

            # Verificar que o email foi confirmado pelo Google
            if not id_info.get("email_verified", False):
                raise InvalidGoogleTokenException(
                    "Email do utilizador não verificado pelo Google."
                )

            return id_info

        except ValueError as e:
            logger.warning(f"Token Google inválido: {e}")
            raise InvalidGoogleTokenException(f"Token inválido: {str(e)}")
        except Exception as e:
            logger.error(f"Erro ao verificar token Google: {e}")
            raise InvalidGoogleTokenException(f"Erro na verificação: {str(e)}")

    @classmethod
    def authenticate_or_create_user(cls, id_token_str: str) -> tuple[User, bool]:
        """
        Verifica o token do Google e cria ou autentica o utilizador.

        Lógica:
        - Se o google_id já existe no banco → utilizador existente (is_new=False)
        - Se o email já existe mas sem google_id → vincula a conta ao Google
        - Se nem email nem google_id existem → cria novo utilizador

        Args:
            id_token_str: O id_token JWT do Google.

        Returns:
            Tuple (User, is_new_user: bool)

        Raises:
            InvalidGoogleTokenException: Se o token for inválido.
        """
        # 1. Verifica o token junto ao Google
        google_payload = cls.verify_google_token(id_token_str)

        google_id = google_payload.get("sub")  # ID único do Google
        email = google_payload.get("email", "").lower()
        name = google_payload.get("name", "")
        avatar_url = google_payload.get("picture", "")

        if not google_id or not email:
            raise InvalidGoogleTokenException(
                "Payload do Google não contém sub ou email."
            )

        # 2. Tenta encontrar por google_id (login recorrente normal)
        try:
            user = User.objects.get(google_id=google_id)

            # Atualiza dados que podem ter mudado no Google (nome, avatar)
            updated_fields = []
            if user.name != name and name:
                user.name = name
                updated_fields.append("name")
            if user.avatar_url != avatar_url and avatar_url:
                user.avatar_url = avatar_url
                updated_fields.append("avatar_url")

            # Garante que is_new_user fica False nos logins recorrentes
            if user.is_new_user:
                user.is_new_user = False
                updated_fields.append("is_new_user")

            if updated_fields:
                updated_fields.append("updated_at")
                user.save(update_fields=updated_fields)

            logger.info(f"Login recorrente: {email}")
            return user, False

        except User.DoesNotExist:
            pass

        # 3. Tenta encontrar por email (conta já criada sem Google, ou email duplicado)
        try:
            user = User.objects.get(email=email)

            # Vincula o google_id a uma conta existente
            user.google_id = google_id
            user.avatar_url = avatar_url or user.avatar_url
            if not user.name and name:
                user.name = name
            user.is_new_user = False
            user.save(
                update_fields=[
                    "google_id",
                    "avatar_url",
                    "name",
                    "is_new_user",
                    "updated_at",
                ]
            )

            logger.info(f"Conta existente vinculada ao Google: {email}")
            return user, False

        except User.DoesNotExist:
            pass

        # 4. Cria novo utilizador
        user = User.objects.create_user(
            email=email,
            name=name,
            google_id=google_id,
            avatar_url=avatar_url,
            is_new_user=True,
            password=None,
        )

        # Garantir que existe um UserProfile associado ao utilizador criado
        try:
            UserProfile.objects.get_or_create(user=user)
        except Exception:
            logger.exception(
                "Falha ao criar/obter UserProfile para novo utilizador Google"
            )

        logger.info(f"Novo utilizador criado via Google: {email}")
        return user, True
