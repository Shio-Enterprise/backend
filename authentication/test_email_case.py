"""E-mail da conta sem diferença de maiúsculas e minúsculas (issue #97)."""

from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from rest_framework import status
from rest_framework.test import APITestCase

from .admin import CustomUserChangeForm, CustomUserCreationForm
from .models import User
from .services import GoogleAuthService

PASSWORD = "SenhaSegura123!"


class EmailNormalizadoNoModeloTests(TestCase):
    def test_create_user_salva_em_minusculas_e_sem_espacos(self):
        user = User.objects.create_user(
            email="  Maria@GMAIL.com ", name="Maria", password=PASSWORD
        )
        user.refresh_from_db()
        self.assertEqual(user.email, "maria@gmail.com")

    def test_save_direto_tambem_normaliza(self):
        user = User.objects.create(email="Joao@Example.COM", name="João")
        user.refresh_from_db()
        self.assertEqual(user.email, "joao@example.com")

    def test_save_com_update_fields_sem_email_funciona(self):
        user = User.objects.create_user(email="maria@gmail.com", name="Maria")
        user.name = "Maria Silva"
        user.save(update_fields=["name"])
        user.refresh_from_db()
        self.assertEqual(user.name, "Maria Silva")

    def test_banco_impede_email_igual_com_maiusculas(self):
        User.objects.create_user(email="maria@gmail.com", name="Maria")
        with self.assertRaises(IntegrityError):
            # update() não passa pelo save(): testa a regra do próprio banco.
            other = User.objects.create(email="outra@gmail.com", name="Outra")
            User.objects.filter(pk=other.pk).update(email="Maria@gmail.com")


class CadastroTests(APITestCase):
    url = "/api/auth/register/"

    def _register(self, email):
        return self.client.post(
            self.url,
            {"email": email, "name": "Maria", "password": PASSWORD},
            format="json",
        )

    def test_cadastro_duplicado_com_maiusculas_e_recusado(self):
        User.objects.create_user(email="maria@gmail.com", name="Maria")

        response = self._register("Maria@gmail.com")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["email"], ["Este e-mail já está em uso."])
        self.assertEqual(User.objects.count(), 1)

    def test_cadastro_duplicado_exato_continua_recusado(self):
        User.objects.create_user(email="maria@gmail.com", name="Maria")

        response = self._register("maria@gmail.com")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["email"], ["Este e-mail já está em uso."])

    def test_cadastro_salva_email_normalizado(self):
        response = self._register("  Maria@Gmail.COM ")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(User.objects.filter(email="maria@gmail.com").exists())

    def test_email_invalido_ou_vazio_continua_com_o_erro_proprio(self):
        casos = {"joao@gmail": "invalid", "": "blank", "   ": "blank"}
        for email, codigo in casos.items():
            with self.subTest(email=email):
                response = self._register(email)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertNotIn(
                    "Este e-mail já está em uso.", response.json()["email"]
                )
                self.assertEqual(User.objects.count(), 0, codigo)

    def test_corrida_no_cadastro_vira_400_e_nao_500(self):
        # Outra requisição cria a conta entre a validação e o INSERT.
        original = User.objects.create_user

        def create_user_racing(*args, **kwargs):
            original(email="maria@gmail.com", name="Concorrente")
            return original(*args, **kwargs)

        with patch.object(User.objects, "create_user", create_user_racing):
            response = self._register("Maria@gmail.com")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["email"], ["Este e-mail já está em uso."])


class LoginTests(APITestCase):
    url = "/api/auth/login/"

    def setUp(self):
        User.objects.create_user(
            email="maria@gmail.com", name="Maria", password=PASSWORD
        )

    def test_login_com_qualquer_combinacao_de_maiusculas(self):
        for email in ["Maria@gmail.com", "MARIA@GMAIL.COM", " maria@gmail.com "]:
            with self.subTest(email=email):
                response = self.client.post(
                    self.url, {"email": email, "password": PASSWORD}, format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_login_de_conta_inexistente_continua_falhando(self):
        response = self.client.post(
            self.url,
            {"email": "Outra@gmail.com", "password": PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_login_com_senha_errada_continua_falhando(self):
        response = self.client.post(
            self.url,
            {"email": "Maria@gmail.com", "password": "errada-123"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class RecuperacaoDeSenhaTests(APITestCase):
    url = "/api/auth/password-reset/"

    def test_envia_email_com_maiusculas(self):
        User.objects.create_user(
            email="maria@gmail.com", name="Maria", password=PASSWORD
        )

        with patch("authentication.views.send_email") as send:
            response = self.client.post(
                self.url, {"email": "Maria@GMAIL.com"}, format="json"
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["to"], "maria@gmail.com")

    def test_email_sem_conta_responde_igual_e_nao_envia(self):
        with patch("authentication.views.send_email") as send:
            response = self.client.post(
                self.url, {"email": "Ninguem@gmail.com"}, format="json"
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        send.assert_not_called()


@patch(
    "authentication.services.settings.GOOGLE_CLIENT_ID",
    "test-client-id.apps.googleusercontent.com",
)
@patch("authentication.services.id_token.verify_oauth2_token")
class GoogleTests(TestCase):
    def _payload(self, email):
        return {
            "sub": "google-123",
            "email": email,
            "email_verified": True,
            "name": "Maria",
            "picture": "",
            "iss": "accounts.google.com",
            "aud": "test-client-id.apps.googleusercontent.com",
        }

    def test_google_liga_a_conta_existente_ignorando_maiusculas(self, mock_verify):
        existing = User.objects.create_user(
            email="Maria@gmail.com", name="Maria", password=PASSWORD
        )
        mock_verify.return_value = self._payload("MARIA@gmail.com")

        user, is_new = GoogleAuthService.authenticate_or_create_user("token")

        self.assertFalse(is_new)
        self.assertEqual(user.pk, existing.pk)
        self.assertEqual(User.objects.count(), 1)

    def test_google_cria_conta_nova_com_email_normalizado(self, mock_verify):
        mock_verify.return_value = self._payload("Nova@Gmail.com")

        user, is_new = GoogleAuthService.authenticate_or_create_user("token")

        self.assertTrue(is_new)
        user.refresh_from_db()
        self.assertEqual(user.email, "nova@gmail.com")


class DjangoAdminTests(TestCase):
    def test_criar_conta_no_admin_com_maiusculas_de_email_existente_e_recusado(self):
        User.objects.create_user(email="maria@gmail.com", name="Maria")
        form = CustomUserCreationForm(
            data={
                "email": "Maria@gmail.com",
                "name": "Outra",
                "password1": PASSWORD,
                "password2": PASSWORD,
            }
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(User.objects.count(), 1)

    def test_editar_email_no_admin_salva_normalizado(self):
        user = User.objects.create_user(email="maria@gmail.com", name="Maria")
        form = CustomUserChangeForm(
            instance=user,
            data={
                "email": "  Maria.Silva@Gmail.com ",
                "name": "Maria",
                "is_active": True,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        user.refresh_from_db()
        self.assertEqual(user.email, "maria.silva@gmail.com")


class NormalizeUserEmailMigrationTests(TransactionTestCase):
    previous = [("authentication", "0010_normalize_profile_phone")]
    target = [("authentication", "0011_normalize_user_email")]

    def _migrate_to_previous(self):
        executor = MigrationExecutor(connection)
        self.addCleanup(
            lambda: MigrationExecutor(connection).migrate(
                executor.loader.graph.leaf_nodes()
            )
        )
        executor.migrate(self.previous)
        return executor.loader.project_state(self.previous).apps

    def _migrate_to_target(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        with redirect_stdout(StringIO()):
            executor.migrate(self.target)

    def test_converte_emails_existentes_para_minusculas(self):
        apps = self._migrate_to_previous()
        OldUser = apps.get_model("authentication", "User")
        OldUser.objects.create(email=" Maria@Gmail.COM", name="Maria")
        OldUser.objects.create(email="joao@example.com", name="João")

        self._migrate_to_target()

        emails = set(User.objects.values_list("email", flat=True))
        self.assertEqual(emails, {"maria@gmail.com", "joao@example.com"})

    def test_para_com_erro_claro_se_houver_contas_duplicadas(self):
        apps = self._migrate_to_previous()
        OldUser = apps.get_model("authentication", "User")
        OldUser.objects.create(email="maria@gmail.com", name="Maria 1")
        OldUser.objects.create(email="Maria@gmail.com", name="Maria 2")

        with self.assertRaisesMessage(RuntimeError, "maria@gmail.com"):
            self._migrate_to_target()

        # Nada foi alterado: as duas contas continuam como estavam.
        self.assertEqual(
            set(OldUser.objects.values_list("email", flat=True)),
            {"maria@gmail.com", "Maria@gmail.com"},
        )
        # Remove a duplicada para o cleanup conseguir migrar até o fim.
        OldUser.objects.filter(email="Maria@gmail.com").delete()

    def test_duplicado_que_so_difere_por_espacos_tambem_para(self):
        apps = self._migrate_to_previous()
        OldUser = apps.get_model("authentication", "User")
        OldUser.objects.create(email="maria@gmail.com", name="Maria 1")
        OldUser.objects.create(email=" maria@gmail.com ", name="Maria 2")

        with self.assertRaisesMessage(RuntimeError, "maria@gmail.com"):
            self._migrate_to_target()

        OldUser.objects.filter(name="Maria 2").delete()

    def test_migracoes_podem_ser_revertidas(self):
        executor = MigrationExecutor(connection)
        self.addCleanup(
            lambda: MigrationExecutor(connection).migrate(
                executor.loader.graph.leaf_nodes()
            )
        )
        with redirect_stdout(StringIO()):
            executor.migrate(self.previous)
        apps = executor.loader.project_state(self.previous).apps
        OldUser = apps.get_model("authentication", "User")
        # Sem a constraint, o banco volta a aceitar e-mails só com maiúsculas.
        OldUser.objects.create(email="maria@gmail.com", name="Maria 1")
        OldUser.objects.create(email="MARIA@gmail.com", name="Maria 2")
        OldUser.objects.filter(name="Maria 2").delete()
