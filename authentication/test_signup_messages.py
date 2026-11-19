"""Mensagens de erro do cadastro e da nova senha em português (issue #96)."""

from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from .models import User
from .serializers import PasswordResetConfirmSerializer

PASSWORD = "SenhaSegura123!"


class CadastroMensagensTests(APITestCase):
    url = "/api/auth/register/"

    def _register(self, **overrides):
        data = {"email": "maria@gmail.com", "name": "Maria", "password": PASSWORD}
        data.update(overrides)
        return self.client.post(self.url, data, format="json")

    def test_email_em_branco(self):
        response = self._register(email="")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["email"], ["Informe o e-mail."])

    def test_email_ausente(self):
        response = self.client.post(
            self.url, {"name": "Maria", "password": PASSWORD}, format="json"
        )
        self.assertEqual(response.data["email"], ["Informe o e-mail."])

    def test_email_sem_ponto_com(self):
        response = self._register(email="joao@gmail")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["email"],
            ["Informe um e-mail válido, como nome@exemplo.com."],
        )

    def test_email_ja_cadastrado(self):
        User.objects.create_user(email="maria@gmail.com", name="Maria")
        response = self._register(email="Maria@gmail.com")
        self.assertEqual(response.data["email"], ["Este e-mail já está em uso."])

    def test_senha_curta(self):
        response = self._register(password="Ab1!x")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["password"],
            ["A senha precisa ter pelo menos 8 caracteres."],
        )

    def test_senha_comum(self):
        response = self._register(password="password123")
        self.assertEqual(response.data["password"], ["Essa senha é muito comum."])

    def test_senha_so_numeros_mostra_todos_os_motivos(self):
        response = self._register(password="12345678")
        self.assertEqual(
            response.data["password"],
            ["Essa senha é muito comum.", "A senha não pode ter só números."],
        )

    def test_senha_em_branco(self):
        response = self._register(password="")
        self.assertEqual(response.data["password"], ["Informe a senha."])

    def test_nome_em_branco(self):
        response = self._register(name="")
        self.assertEqual(response.data["name"], ["Informe o nome."])

    def test_campos_nulos(self):
        response = self._register(email=None, name=None, password=None)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["email"], ["Informe o e-mail."])
        self.assertEqual(response.data["name"], ["Informe o nome."])
        self.assertEqual(response.data["password"], ["Informe a senha."])

    def test_varios_campos_com_erro_aparecem_juntos(self):
        response = self._register(email="joao@gmail", password="123")
        self.assertIn("email", response.data)
        self.assertIn("password", response.data)

    def test_email_longo_demais(self):
        response = self._register(email="a" * 250 + "@x.com")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["email"],
            ["O e-mail pode ter no máximo 254 caracteres."],
        )

    def test_nome_longo_demais(self):
        # Sem o limite no serializer, o Postgres recusava e a resposta era 500.
        response = self._register(name="A" * 256)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["name"], ["O nome pode ter no máximo 255 caracteres."]
        )

    def test_nome_com_255_caracteres_e_aceito(self):
        response = self._register(name="A" * 255)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_cadastro_valido_continua_funcionando(self):
        response = self._register()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class NovaSenhaMensagensTests(TestCase):
    def _errors(self, new_password):
        serializer = PasswordResetConfirmSerializer(
            data={"uidb64": "x", "token": "y", "new_password": new_password}
        )
        self.assertFalse(serializer.is_valid())
        return serializer.errors["new_password"]

    def test_nova_senha_curta(self):
        self.assertEqual(
            self._errors("Ab1!x"), ["A senha precisa ter pelo menos 8 caracteres."]
        )

    def test_nova_senha_so_numeros(self):
        self.assertIn("A senha não pode ter só números.", self._errors("80417263"))

    def test_nova_senha_em_branco(self):
        self.assertEqual(self._errors(""), ["Informe a senha."])

    def test_nova_senha_nula(self):
        self.assertEqual(self._errors(None), ["Informe a senha."])


class ValidadorSemTraducaoTests(TestCase):
    @override_settings(
        AUTH_PASSWORD_VALIDATORS=[
            {
                "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
                "OPTIONS": {"min_length": 12},
            },
            {"NAME": "authentication.test_signup_messages.SemMaiusculaValidator"},
        ]
    )
    def test_usa_min_length_configurado_e_mantem_mensagem_sem_traducao(self):
        serializer = PasswordResetConfirmSerializer(
            data={"uidb64": "x", "token": "y", "new_password": "abc"}
        )
        self.assertFalse(serializer.is_valid())
        self.assertEqual(
            serializer.errors["new_password"],
            [
                "A senha precisa ter pelo menos 12 caracteres.",
                "Use ao menos uma letra maiúscula.",
            ],
        )


class SemMaiusculaValidator:
    """Validador de exemplo com código que não está na tabela de tradução."""

    def validate(self, password, user=None):
        from django.core.exceptions import ValidationError

        if password == password.lower():
            raise ValidationError(
                "Use ao menos uma letra maiúscula.", code="password_no_upper"
            )

    def get_help_text(self):
        return ""
