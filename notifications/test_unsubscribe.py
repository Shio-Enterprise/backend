from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from notifications.unsubscribe import (
    make_unsubscribe_token,
    read_unsubscribe_token,
    unsubscribe_url,
)


class UnsubscribeTokenTests(SimpleTestCase):
    def test_token_ida_e_volta_normaliza_email(self):
        token = make_unsubscribe_token("  Ana@X.COM ")
        self.assertEqual(read_unsubscribe_token(token), "ana@x.com")

    def test_token_invalido_retorna_none(self):
        token = make_unsubscribe_token("ana@x.com")
        trocado = token[:-1] + ("A" if token[-1] != "A" else "B")
        for ruim in ["lixo", "", trocado]:
            self.assertIsNone(read_unsubscribe_token(ruim))

    @override_settings(FRONTEND_URL="https://x")
    def test_unsubscribe_url_usa_frontend_url(self):
        url = unsubscribe_url("ana@x.com")
        token = make_unsubscribe_token("ana@x.com")
        self.assertEqual(url, f"https://x/descadastrar/{token}")

    def test_token_nunca_expira(self):
        token = make_unsubscribe_token("ana@x.com")
        dez_anos = 10 * 365 * 24 * 3600
        with patch(
            "django.core.signing.time.time", return_value=9_999_999_999 + dez_anos
        ):
            self.assertEqual(read_unsubscribe_token(token), "ana@x.com")
