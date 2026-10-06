from contextlib import redirect_stdout
from datetime import date
from io import StringIO

from django.conf import settings
from django.core import mail
from django.core.mail.backends.base import BaseEmailBackend
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings

from notifications.backends import (
    CONSOLE_BACKEND,
    RESEND_BACKEND,
    select_email_backend,
    warn_if_email_disabled,
)
from notifications.email import mask_email, send_email

LOCMEM_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
FAILING_BACKEND = "notifications.tests.FailingBackend"


class FailingBackend(BaseEmailBackend):
    """Simula o provedor recusando o envio; a mensagem inclui o endereço,
    como fazem os erros reais do anymail."""

    def send_messages(self, email_messages):
        recipients = ", ".join(r for m in email_messages for r in m.to)
        raise ConnectionError(f"provedor recusou {recipients}")


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


class MaskEmailTests(SimpleTestCase):
    def test_mostra_dois_primeiros_caracteres(self):
        self.assertEqual(mask_email("arthur@gmail.com"), "ar***@gmail.com")

    def test_usuario_de_dois_caracteres(self):
        self.assertEqual(mask_email("ab@x.com"), "a***@x.com")

    def test_usuario_de_um_caractere(self):
        self.assertEqual(mask_email("a@x.com"), "***@x.com")

    def test_sem_arroba(self):
        self.assertEqual(mask_email("invalido"), "***")


@override_settings(DEFAULT_FROM_EMAIL="Shio <nao-responda@shio.test>")
class SendEmailTests(SimpleTestCase):
    def test_envia_texto_e_html_com_dados_corretos(self):
        ok = send_email("cliente@teste.com", "Assunto X", "teste", {"nome": "Maria"})

        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["cliente@teste.com"])
        self.assertEqual(msg.subject, "Assunto X")
        self.assertEqual(msg.from_email, "Shio <nao-responda@shio.test>")
        self.assertIn("Olá, Maria!", msg.body)
        self.assertIn("não responda", msg.body)
        self.assertIn(str(date.today().year), msg.body)
        self.assertNotIn("<table", msg.body)

        html, mimetype = msg.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn("Olá, Maria!", html)
        self.assertIn("SHIO", html)
        self.assertIn("não responda", html)
        self.assertIn("<title>Assunto X</title>", html)

    def test_aceita_lista_de_destinatarios(self):
        ok = send_email(["a@teste.com", "b@teste.com"], "Assunto", "teste")

        self.assertTrue(ok)
        self.assertEqual(mail.outbox[0].to, ["a@teste.com", "b@teste.com"])

    def test_sem_nome_usa_saudacao_generica(self):
        send_email("cliente@teste.com", "Assunto", "teste")

        self.assertIn("Olá!", mail.outbox[0].body)

    def test_escapa_html_so_na_versao_html(self):
        send_email(
            "cliente@teste.com", "Assunto", "teste", {"nome": "Tom & <b>Jerry</b>"}
        )

        msg = mail.outbox[0]
        self.assertIn("Tom & <b>Jerry</b>", msg.body)
        html, _ = msg.alternatives[0]
        self.assertIn("Tom &amp; &lt;b&gt;Jerry&lt;/b&gt;", html)

    def test_sucesso_loga_destinatario_mascarado(self):
        with self.assertLogs("notifications", level="INFO") as logs:
            send_email("cliente@teste.com", "Assunto", "teste")

        log = "\n".join(logs.output)
        self.assertIn("cl***@teste.com", log)
        self.assertNotIn("cliente@teste.com", log)

    @override_settings(EMAIL_BACKEND=FAILING_BACKEND)
    def test_falha_do_provedor_nao_propaga_e_loga_mascarado(self):
        with self.assertLogs("notifications", level="ERROR") as logs:
            ok = send_email("cliente@teste.com", "Assunto", "teste")

        self.assertFalse(ok)
        log = "\n".join(logs.output)
        self.assertIn("cl***@teste.com", log)
        self.assertIn("ConnectionError", log)
        self.assertNotIn("cliente@teste.com", log)

    def test_template_inexistente_retorna_false(self):
        with self.assertLogs("notifications", level="ERROR"):
            ok = send_email("cliente@teste.com", "Assunto", "nao_existe")

        self.assertFalse(ok)
        self.assertEqual(len(mail.outbox), 0)

    def test_sem_destinatario_retorna_false(self):
        for vazio in ("", [], [""]):
            with self.subTest(to=vazio):
                with self.assertLogs("notifications", level="WARNING"):
                    ok = send_email(vazio, "Assunto", "teste")
                self.assertFalse(ok)
        self.assertEqual(len(mail.outbox), 0)


class EnviarEmailTesteCommandTests(SimpleTestCase):
    def test_envia_e_informa_sucesso(self):
        out = StringIO()

        call_command("enviar_email_teste", "dev@teste.com", stdout=out)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["dev@teste.com"])
        self.assertEqual(mail.outbox[0].subject, "Teste de envio — Shio")
        self.assertIn("E-mail de teste enviado para dev@teste.com.", out.getvalue())
        self.assertNotIn("não enviado", out.getvalue())

    @override_settings(EMAIL_BACKEND=FAILING_BACKEND)
    def test_falha_gera_command_error(self):
        with self.assertLogs("notifications", level="ERROR"):
            with self.assertRaises(CommandError):
                call_command("enviar_email_teste", "dev@teste.com", stdout=StringIO())

    @override_settings(EMAIL_BACKEND=CONSOLE_BACKEND)
    def test_avisa_quando_backend_e_console(self):
        out = StringIO()

        # O backend de console imprime o e-mail inteiro no stdout; descartar.
        with redirect_stdout(StringIO()):
            call_command("enviar_email_teste", "dev@teste.com", stdout=out)

        self.assertIn("não enviado", out.getvalue())
