from django.conf import settings
from django.test import SimpleTestCase, override_settings

from notifications.backends import (
    CONSOLE_BACKEND,
    RESEND_BACKEND,
    select_email_backend,
    warn_if_email_disabled,
)

LOCMEM_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


class SelectEmailBackendTests(SimpleTestCase):
    def test_com_chave_usa_resend(self):
        self.assertEqual(select_email_backend("re_123"), RESEND_BACKEND)

    def test_sem_chave_usa_console(self):
        self.assertEqual(select_email_backend(""), CONSOLE_BACKEND)

    def test_chave_so_com_espacos_usa_console(self):
        self.assertEqual(select_email_backend("   "), CONSOLE_BACKEND)


class WarnIfEmailDisabledTests(SimpleTestCase):
    @override_settings(DEBUG=False, EMAIL_BACKEND=CONSOLE_BACKEND)
    def test_avisa_em_producao_sem_chave(self):
        with self.assertLogs("notifications", level="WARNING") as logs:
            warn_if_email_disabled()
        self.assertIn(
            "RESEND_API_KEY ausente: e-mails não serão enviados, apenas exibidos no log.",
            logs.output[0],
        )

    @override_settings(DEBUG=True, EMAIL_BACKEND=CONSOLE_BACKEND)
    def test_nao_avisa_em_desenvolvimento(self):
        with self.assertNoLogs("notifications", level="WARNING"):
            warn_if_email_disabled()

    @override_settings(DEBUG=False, EMAIL_BACKEND=RESEND_BACKEND)
    def test_nao_avisa_com_resend_configurado(self):
        with self.assertNoLogs("notifications", level="WARNING"):
            warn_if_email_disabled()


class AmbienteDeTesteTests(SimpleTestCase):
    def test_testes_nao_enviam_emails_de_verdade(self):
        self.assertEqual(settings.EMAIL_BACKEND, LOCMEM_BACKEND)

    def test_remetente_padrao_e_da_shio(self):
        # Padrão do Django seria "webmaster@localhost".
        self.assertNotEqual(settings.DEFAULT_FROM_EMAIL, "webmaster@localhost")
