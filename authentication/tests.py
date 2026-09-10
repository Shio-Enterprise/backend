"""
Testes unitários e de integração para o app de autenticação.

Executar com:
    python manage.py test authentication
    ou
    pytest authentication/tests.py -v
"""

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from .models import NewsletterSubscriber, UserProfile, UserRole
from .services import GoogleAuthService, InvalidGoogleTokenException

User = get_user_model()


# ─── Helpers ──────────────────────────────────────────────────────────────────


def make_google_payload(
    sub="123456789",
    email="test@example.com",
    name="Test User",
    picture="https://example.com/avatar.jpg",
    email_verified=True,
):
    """Cria um payload simulado do Google para testes."""
    return {
        "sub": sub,
        "email": email,
        "name": name,
        "picture": picture,
        "email_verified": email_verified,
        "aud": "test-client-id.apps.googleusercontent.com",
    }


# ─── Testes do GoogleAuthService ──────────────────────────────────────────────


class GoogleAuthServiceTests(TestCase):
    """Testes para a camada de serviço de autenticação Google."""

    @patch("authentication.services.id_token.verify_oauth2_token")
    @patch(
        "authentication.services.settings.GOOGLE_CLIENT_ID",
        "test-client-id.apps.googleusercontent.com",
    )
    def test_novo_utilizador_criado_no_primeiro_login(self, mock_verify):
        """Deve criar um novo utilizador quando o google_id não existe."""
        mock_verify.return_value = make_google_payload()

        user, is_new = GoogleAuthService.authenticate_or_create_user("fake-token")

        self.assertTrue(is_new)
        self.assertEqual(user.email, "test@example.com")
        self.assertEqual(user.name, "Test User")
        self.assertEqual(user.google_id, "123456789")
        self.assertTrue(user.is_new_user)
        self.assertFalse(user.has_usable_password())

    @patch("authentication.services.id_token.verify_oauth2_token")
    @patch(
        "authentication.services.settings.GOOGLE_CLIENT_ID",
        "test-client-id.apps.googleusercontent.com",
    )
    def test_utilizador_existente_nao_duplicado(self, mock_verify):
        """Deve retornar o utilizador existente no segundo login."""
        mock_verify.return_value = make_google_payload()

        # Primeiro login
        user1, is_new1 = GoogleAuthService.authenticate_or_create_user("fake-token")
        # Segundo login
        user2, is_new2 = GoogleAuthService.authenticate_or_create_user("fake-token")

        self.assertTrue(is_new1)
        self.assertFalse(is_new2)
        self.assertEqual(user1.pk, user2.pk)
        self.assertEqual(User.objects.count(), 1)

    @patch("authentication.services.id_token.verify_oauth2_token")
    @patch(
        "authentication.services.settings.GOOGLE_CLIENT_ID",
        "test-client-id.apps.googleusercontent.com",
    )
    def test_conta_existente_vinculada_ao_google(self, mock_verify):
        """Deve vincular google_id a uma conta criada sem Google."""
        # Cria conta prévia sem google_id
        existing_user = User.objects.create_user(
            email="test@example.com",
            name="Existing User",
            password="some-password",
        )

        mock_verify.return_value = make_google_payload()
        user, is_new = GoogleAuthService.authenticate_or_create_user("fake-token")

        self.assertFalse(is_new)
        self.assertEqual(user.pk, existing_user.pk)
        user.refresh_from_db()
        self.assertEqual(user.google_id, "123456789")

    @patch("authentication.services.id_token.verify_oauth2_token")
    @patch(
        "authentication.services.settings.GOOGLE_CLIENT_ID",
        "test-client-id.apps.googleusercontent.com",
    )
    def test_token_invalido_levanta_excecao(self, mock_verify):
        """Deve lançar InvalidGoogleTokenException para token inválido."""
        mock_verify.side_effect = ValueError("Token invalid")

        with self.assertRaises(InvalidGoogleTokenException):
            GoogleAuthService.authenticate_or_create_user("invalid-token")

    @patch("authentication.services.id_token.verify_oauth2_token")
    @patch(
        "authentication.services.settings.GOOGLE_CLIENT_ID",
        "test-client-id.apps.googleusercontent.com",
    )
    def test_email_nao_verificado_levanta_excecao(self, mock_verify):
        """Deve rejeitar tokens de contas sem email verificado."""
        mock_verify.return_value = make_google_payload(email_verified=False)

        with self.assertRaises(InvalidGoogleTokenException):
            GoogleAuthService.authenticate_or_create_user("fake-token")


# ─── Testes dos Endpoints da API ──────────────────────────────────────────────


class GoogleLoginViewTests(APITestCase):
    """Testes de integração para o endpoint POST /api/auth/google/."""

    url = "/api/auth/google/"

    @patch("authentication.services.id_token.verify_oauth2_token")
    @patch(
        "authentication.services.settings.GOOGLE_CLIENT_ID",
        "test-client-id.apps.googleusercontent.com",
    )
    def test_novo_utilizador_retorna_201(self, mock_verify):
        """Deve retornar 201 com tokens e dados do user no primeiro login."""
        mock_verify.return_value = make_google_payload()

        response = self.client.post(
            self.url, {"id_token": "valid-token"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()
        self.assertIn("access", data)
        self.assertIn("refresh", data)
        self.assertTrue(data["is_new_user"])
        self.assertEqual(data["user"]["email"], "test@example.com")

    @patch("authentication.services.id_token.verify_oauth2_token")
    @patch(
        "authentication.services.settings.GOOGLE_CLIENT_ID",
        "test-client-id.apps.googleusercontent.com",
    )
    def test_utilizador_existente_retorna_200(self, mock_verify):
        """Deve retornar 200 no segundo login."""
        mock_verify.return_value = make_google_payload()

        self.client.post(self.url, {"id_token": "valid-token"}, format="json")
        response = self.client.post(
            self.url, {"id_token": "valid-token"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.json()["is_new_user"])

    def test_sem_id_token_retorna_400(self):
        """Deve retornar 400 se id_token não for enviado."""
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("authentication.services.id_token.verify_oauth2_token")
    @patch(
        "authentication.services.settings.GOOGLE_CLIENT_ID",
        "test-client-id.apps.googleusercontent.com",
    )
    def test_token_invalido_retorna_401(self, mock_verify):
        """Deve retornar 401 para token inválido."""
        mock_verify.side_effect = ValueError("Token invalid")

        response = self.client.post(self.url, {"id_token": "bad-token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class MeViewTests(APITestCase):
    """Testes para o endpoint GET /api/auth/me/."""

    url = "/api/auth/me/"

    def setUp(self):
        self.user = User.objects.create_user(
            email="me@example.com",
            name="Me User",
            google_id="google-123",
        )
        refresh = RefreshToken.for_user(self.user)
        self.access_token = str(refresh.access_token)

    def test_utilizador_autenticado_recebe_dados(self):
        """Deve retornar os dados do utilizador com token válido."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access_token}")
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["email"], "me@example.com")

    def test_sem_token_retorna_401(self):
        """Deve retornar 401 sem Authorization header."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_patch_updates_phone_and_cpf(self):
        """Deve atualizar o telefone e CPF do utilizador autenticado."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access_token}")

        response = self.client.patch(
            self.url,
            {
                "phone_number": "61999999999",
                "cpf": "12345678910",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["phone_number"], "61999999999")
        self.assertEqual(response.json()["cpf"], "12345678910")

        self.user.refresh_from_db()
        self.assertEqual(self.user.profile.phone_number, "61999999999")
        self.assertEqual(self.user.profile.cpf, "12345678910")


class TokenRefreshViewTests(APITestCase):
    """Testes para o endpoint POST /api/auth/token/refresh/."""

    url = "/api/auth/token/refresh/"

    def setUp(self):
        self.user = User.objects.create_user(
            email="refresh@example.com", name="Refresh User"
        )
        self.refresh_token = str(RefreshToken.for_user(self.user))

    def test_renovacao_com_sucesso(self):
        """Deve gerar um novo access token dado um refresh token válido."""
        response = self.client.post(
            self.url, {"refresh": self.refresh_token}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.json())

    def test_renovacao_sem_token(self):
        """Deve retornar 400 se o refresh token não for enviado."""
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_renovacao_com_token_invalido(self):
        """Deve retornar 401 para um token inválido."""
        response = self.client.post(self.url, {"refresh": "token-falso"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class AuthModelTests(TestCase):
    def test_is_admin_true_for_staff(self):
        user = User.objects.create_user(
            email="staff@example.com",
            name="Staff User",
            is_staff=True,
        )
        self.assertTrue(user.is_admin)

    def test_is_admin_true_for_superuser(self):
        user = User.objects.create_superuser(
            email="super@example.com",
            name="Super User",
            password="test1234",
        )
        self.assertTrue(user.is_admin)

    def test_is_admin_true_for_role_admin(self):
        user = User.objects.create_user(
            email="roleadmin@example.com",
            name="Role Admin",
        )
        UserProfile.objects.create(user=user, role=UserRole.ADMIN)
        self.assertTrue(user.is_admin)

    def test_is_admin_false_for_customer(self):
        user = User.objects.create_user(
            email="customer@example.com",
            name="Customer User",
        )
        UserProfile.objects.create(user=user, role=UserRole.CUSTOMER)
        self.assertFalse(user.is_admin)


class PermissionTests(TestCase):
    def test_staff_user_has_admin_permission(self):
        user = User.objects.create_user(
            email="staffperm@example.com",
            name="Staff Perm",
            is_staff=True,
        )
        request = SimpleNamespace(user=user)
        from authentication.permissions import IsStaffOrSuperUser

        self.assertTrue(IsStaffOrSuperUser().has_permission(request, None))

    def test_admin_role_user_has_admin_permission(self):
        user = User.objects.create_user(
            email="roleperm@example.com",
            name="Role Perm",
        )
        UserProfile.objects.create(user=user, role=UserRole.ADMIN)
        request = SimpleNamespace(user=user)
        from authentication.permissions import IsStaffOrSuperUser

        self.assertTrue(IsStaffOrSuperUser().has_permission(request, None))

    def test_customer_user_does_not_have_admin_permission(self):
        user = User.objects.create_user(
            email="customerperm@example.com",
            name="Customer Perm",
        )
        UserProfile.objects.create(user=user, role=UserRole.CUSTOMER)
        request = SimpleNamespace(user=user)
        from authentication.permissions import IsStaffOrSuperUser

        self.assertFalse(IsStaffOrSuperUser().has_permission(request, None))


class LogoutViewTests(APITestCase):
    """Testes para o endpoint POST /api/auth/logout/."""

    url = "/api/auth/logout/"

    def setUp(self):
        self.user = User.objects.create_user(
            email="logout@example.com", name="Logout User"
        )
        self.refresh = RefreshToken.for_user(self.user)
        self.access = str(self.refresh.access_token)

    def test_logout_com_sucesso(self):
        """Deve invalidar o token e retornar 200."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access}")
        response = self.client.post(
            self.url, {"refresh": str(self.refresh)}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_logout_sem_autenticacao_retorna_401(self):
        """Não deve permitir logout se não enviar Authorization header (access token)."""
        response = self.client.post(
            self.url, {"refresh": str(self.refresh)}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_logout_sem_refresh_token_retorna_400(self):
        """Deve retornar 400 se o corpo do request não tiver o refresh token."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access}")
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class NewsletterSubscriberModelTests(TestCase):
    def test_cria_assinante_com_consentimento(self):
        subscriber = NewsletterSubscriber.objects.create(
            email="fan@shio.com", consent_lgpd=True
        )
        self.assertTrue(subscriber.consent_lgpd)
        self.assertIsNotNone(subscriber.subscribed_at)

    def test_email_e_unico(self):
        NewsletterSubscriber.objects.create(email="dup@shio.com", consent_lgpd=True)
        with self.assertRaises(Exception):
            NewsletterSubscriber.objects.create(email="dup@shio.com", consent_lgpd=True)
