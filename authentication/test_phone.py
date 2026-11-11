from django.test import SimpleTestCase
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User, UserProfile
from .phone import (
    MOBILE_NINE_DIGITS_MESSAGE,
    PHONE_INVALID_MESSAGE,
    format_phone,
    international_phone,
    normalize_phone,
)


class NormalizePhoneTests(SimpleTestCase):
    def test_aceita_celular_e_fixo_em_qualquer_formato(self):
        cases = {
            "61999999999": "61999999999",
            "(61) 99999-9999": "61999999999",
            " 61 9 9999 9999 ": "61999999999",
            "+55 (61) 99999-9999": "61999999999",
            "5561999999999": "61999999999",
            "(11) 3333-4444": "1133334444",
            "+55 11 3333-4444": "1133334444",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_phone(value), expected)

    def test_recusa_telefone_invalido(self):
        values = [
            "999999999",  # sem DDD
            "619999999999",  # dígito a mais
            "01999999999",  # DDD com zero
            "60999999999",  # DDD com zero
            "61899999999",  # 11 dígitos sem o 9
            "6119999999",  # fixo começando com 1
            "11111111111",
            "abc",
            "()-",
        ]
        for value in values:
            with (
                self.subTest(value=value),
                self.assertRaisesMessage(ValidationError, PHONE_INVALID_MESSAGE),
            ):
                normalize_phone(value)

    def test_celular_antigo_de_8_digitos_pede_o_9(self):
        for value in ("(61) 9999-9999", "6188887777", "+55 61 7777-6666"):
            with (
                self.subTest(value=value),
                self.assertRaisesMessage(ValidationError, MOBILE_NINE_DIGITS_MESSAGE),
            ):
                normalize_phone(value)

    def test_vazio_vira_none(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertIsNone(normalize_phone(value))

    def test_formata_para_exibir(self):
        self.assertEqual(format_phone("61999999999"), "(61) 99999-9999")
        self.assertEqual(format_phone("1133334444"), "(11) 3333-4444")
        self.assertEqual(format_phone("telefone antigo"), "telefone antigo")
        # Antigo com 10 ou 11 dígitos mas inválido não ganha máscara.
        self.assertEqual(format_phone("6199999999"), "6199999999")
        self.assertEqual(format_phone("01999999999"), "01999999999")
        self.assertIsNone(format_phone(None))

    def test_formato_internacional_para_o_gateway(self):
        self.assertEqual(international_phone("61999999999"), "+5561999999999")
        self.assertEqual(international_phone("(61) 99999-9999"), "+5561999999999")
        self.assertEqual(international_phone("lixo"), "lixo")
        self.assertEqual(international_phone(None), "")


class MePhoneAPITests(APITestCase):
    url = "/api/auth/me/"

    def setUp(self):
        self.user = User.objects.create_user(
            email="fone@example.com", name="Fone", google_id="google-fone"
        )
        token = RefreshToken.for_user(self.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_salva_digitos_e_devolve_formatado(self):
        for value in ("(61) 99999-9999", "+55 61 99999 9999", "61999999999"):
            with self.subTest(value=value):
                response = self.client.patch(
                    self.url, {"phone_number": value}, format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response.json()["phone_number"], "(61) 99999-9999")
                self.user.refresh_from_db()
                self.assertEqual(self.user.profile.phone_number, "61999999999")

    def test_telefone_invalido_nao_salva_nada(self):
        UserProfile.objects.create(user=self.user, phone_number="61999999999")
        response = self.client.patch(
            self.url, {"name": "Não salvar", "phone_number": "1234"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json()["details"]["phone_number"], [PHONE_INVALID_MESSAGE]
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.name, "Fone")
        self.assertEqual(self.user.profile.phone_number, "61999999999")

    def test_telefone_vazio_limpa_o_campo(self):
        UserProfile.objects.create(user=self.user, phone_number="61999999999")
        response = self.client.patch(self.url, {"phone_number": ""}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.json()["phone_number"])
        self.user.refresh_from_db()
        self.assertIsNone(self.user.profile.phone_number)

    def test_telefone_antigo_fora_do_padrao_volta_como_esta(self):
        UserProfile.objects.create(user=self.user, phone_number="ligar à tarde")
        response = self.client.get(self.url)
        self.assertEqual(response.json()["phone_number"], "ligar à tarde")
