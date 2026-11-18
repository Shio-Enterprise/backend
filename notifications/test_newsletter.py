import re
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from authentication.models import (
    NewsletterSend,
    NewsletterSendStatus,
    NewsletterSubscriber,
)
from notifications.newsletter import (
    NewsletterInProgress,
    _run,
    expire_stale_sends,
    get_newsletter_audience,
    render_newsletter_preview,
    send_newsletter_test,
    start_newsletter_campaign,
)


def send_newsletter_campaign(admin, subject, message):
    """Nos testes o envio roda inline; devolve o registro já concluído."""
    record = start_newsletter_campaign(admin, subject, message)
    record.refresh_from_db()
    return record


User = get_user_model()


class NewsletterAudienceTests(TestCase):
    def test_inscrito_sem_consentimento_ou_cancelado_nao_recebe(self):
        # Elegível
        NewsletterSubscriber.objects.create(email="valido@shio.com", consent_lgpd=True)
        # Sem consentimento LGPD
        NewsletterSubscriber.objects.create(
            email="sem_consentimento@shio.com", consent_lgpd=False
        )
        # Cancelado / descadastrado
        NewsletterSubscriber.objects.create(
            email="cancelado@shio.com",
            consent_lgpd=True,
            unsubscribed_at=timezone.now(),
        )
        # Sem consentimento e cancelado
        NewsletterSubscriber.objects.create(
            email="ambos@shio.com",
            consent_lgpd=False,
            unsubscribed_at=timezone.now(),
        )

        audience = get_newsletter_audience()
        emails = [r.email for r in audience]
        self.assertEqual(emails, ["valido@shio.com"])

    def test_normalizacao_e_deduplicacao(self):
        NewsletterSubscriber.objects.create(
            email="Maria@Exemplo.COM", consent_lgpd=True
        )
        audience = get_newsletter_audience()
        self.assertEqual(len(audience), 1)
        self.assertEqual(audience[0].email, "maria@exemplo.com")

    def test_vincula_nome_de_usuario_cadastrado(self):
        User.objects.create_user(
            email="cliente@shio.com", name="Cliente Especial", password="pass"
        )
        NewsletterSubscriber.objects.create(email="cliente@shio.com", consent_lgpd=True)
        NewsletterSubscriber.objects.create(email="anonimo@shio.com", consent_lgpd=True)

        audience = get_newsletter_audience()
        by_email = {r.email: r.name for r in audience}
        self.assertEqual(by_email["cliente@shio.com"], "Cliente Especial")
        self.assertIsNone(by_email["anonimo@shio.com"])


class NewsletterCampaignTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="admin@shio.com", name="Admin Shio", password="adminpassword"
        )

    def _setup_audience(self):
        NewsletterSubscriber.objects.create(email="ana@x.com", consent_lgpd=True)
        NewsletterSubscriber.objects.create(email="bia@x.com", consent_lgpd=True)

    def test_envia_um_email_por_pessoa_com_link_proprio(self):
        self._setup_audience()
        record = send_newsletter_campaign(
            self.admin, "Coleção Inverno", "Confira as novidades quentes!"
        )

        self.assertEqual(len(mail.outbox), 2)
        for message in mail.outbox:
            # Um destinatário por envio (nunca todos em cópia)
            self.assertEqual(len(message.to), 1)
            self.assertEqual(message.subject, "Coleção Inverno")
            self.assertIn("Confira as novidades quentes!", message.body)

        links = {
            line
            for message in mail.outbox
            for line in message.body.splitlines()
            if "/descadastrar/" in line
        }
        self.assertEqual(len(links), 2)

        self.assertIsInstance(record, NewsletterSend)
        self.assertEqual(record.sent_count, 2)
        self.assertEqual(record.failed_count, 0)
        self.assertEqual(record.sent_by, self.admin)
        self.assertEqual(record.subject, "Coleção Inverno")
        self.assertEqual(record.message, "Confira as novidades quentes!")
        self.assertEqual(record.total_count, 2)
        self.assertEqual(record.status, NewsletterSendStatus.SENT)
        self.assertIsNotNone(record.finished_at)

    def test_falha_em_um_inscrito_nao_interrompe_os_demais(self):
        self._setup_audience()
        with patch(
            "notifications.newsletter.send_email", side_effect=[False, True]
        ) as mocked:
            record = send_newsletter_campaign(
                self.admin, "Assunto", "Mensagem de teste"
            )

        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(record.sent_count, 1)
        self.assertEqual(record.failed_count, 1)

    @override_settings(MARKETING_EMAIL_INTERVAL_SECONDS=0.5)
    def test_pausa_entre_envios_menos_depois_do_ultimo(self):
        self._setup_audience()
        NewsletterSubscriber.objects.create(email="cris@x.com", consent_lgpd=True)
        with patch("notifications.newsletter.time.sleep") as sleeper:
            send_newsletter_campaign(self.admin, "Assunto", "Mensagem")
        self.assertEqual(sleeper.call_count, 2)
        sleeper.assert_called_with(0.5)

    @override_settings(MARKETING_EMAIL_INTERVAL_SECONDS=0)
    def test_sem_pausa_quando_intervalo_zero(self):
        self._setup_audience()
        with patch("notifications.newsletter.time.sleep") as sleeper:
            send_newsletter_campaign(self.admin, "Assunto", "Mensagem")
        sleeper.assert_not_called()

    def test_teste_vai_so_para_admin_e_nao_cria_historico(self):
        self._setup_audience()
        ok = send_newsletter_test(self.admin, "Assunto Teste", "Corpo do teste")
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["admin@shio.com"])
        self.assertEqual(mail.outbox[0].subject, "[Teste] Assunto Teste")
        self.assertEqual(NewsletterSend.objects.count(), 0)

    def test_preview_renderiza_html_com_quebras_e_descadastro(self):
        html = render_newsletter_preview(
            subject="Novidades",
            message="<script>alert(1)</script>\nLinha 2",
            name="Fulano",
        )
        self.assertIn("Novidades", html)
        self.assertNotIn("Olá", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn("<br>", html)
        self.assertIn("Cancelar inscrição", html)
        self.assertIn("/descadastrar/exemplo", html)

    def test_email_comeca_pelo_texto_do_admin_sem_saudacao_fixa(self):
        User.objects.create_user(email="ana@x.com", name="Ana Souza", password="x")
        self._setup_audience()
        send_newsletter_campaign(self.admin, "Assunto", "Chegou a coleção nova.")
        for message in mail.outbox:
            self.assertNotIn("Olá", message.body)
            self.assertNotIn("Olá", message.alternatives[0][0])
            self.assertIn("Chegou a coleção nova.", message.body)

    def test_marcador_nome_vira_primeiro_nome_de_cada_destinatario(self):
        User.objects.create_user(email="ana@x.com", name="Ana Souza", password="x")
        self._setup_audience()
        record = send_newsletter_campaign(
            self.admin, "Assunto", "Olá, {nome}!\nChegou a coleção nova."
        )
        bodies = {m.to[0]: (m.body, m.alternatives[0][0]) for m in mail.outbox}
        self.assertIn("Olá, Ana!", bodies["ana@x.com"][0])
        self.assertIn("Olá, Ana!", bodies["ana@x.com"][1])
        self.assertNotIn("Souza", bodies["ana@x.com"][0])
        # bia@x.com não tem conta: sem nome, o marcador some com a vírgula.
        self.assertIn("Olá!", bodies["bia@x.com"][0])
        self.assertIn("Olá!", bodies["bia@x.com"][1])
        for text, html in bodies.values():
            self.assertNotIn("{nome}", text)
            self.assertNotIn("{nome}", html)
        # O histórico guarda o texto como o admin escreveu.
        self.assertEqual(record.message, "Olá, {nome}!\nChegou a coleção nova.")

    def test_email_de_teste_usa_o_nome_do_admin(self):
        send_newsletter_test(self.admin, "Assunto", "Olá, {nome}!")
        self.assertIn("Olá, Admin!", mail.outbox[0].body)

    def test_nome_com_html_e_escapado(self):
        User.objects.create_user(email="ana@x.com", name="<b>Ana</b>", password="x")
        self._setup_audience()
        send_newsletter_campaign(self.admin, "Assunto", "Olá, {nome}!")
        html = next(m for m in mail.outbox if m.to == ["ana@x.com"]).alternatives[0][0]
        self.assertIn("&lt;b&gt;Ana&lt;/b&gt;", html)
        self.assertNotIn("<b>Ana", html)

    def test_previa_usa_nome_de_exemplo(self):
        html = render_newsletter_preview(subject="Oi", message="Olá, {nome}!")
        self.assertIn("Olá, Maria!", html)
        self.assertNotIn("{nome}", html)

    def test_previa_igual_ao_email_enviado(self):
        User.objects.create_user(email="ana@x.com", name="Ana Souza", password="x")
        NewsletterSubscriber.objects.create(email="ana@x.com", consent_lgpd=True)
        message = "Olá, {nome}!\nChegou a coleção nova."
        send_newsletter_campaign(self.admin, "Coleção", message)
        sent = mail.outbox[0].alternatives[0][0]
        preview = render_newsletter_preview("Coleção", message, name="Ana Souza")

        def sem_link_de_descadastro(html):
            return re.sub(r"/descadastrar/[^\"]+", "/descadastrar/X", html)

        self.assertEqual(
            sem_link_de_descadastro(preview), sem_link_de_descadastro(sent)
        )


class NewsletterSendStateTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="admin@shio.com", name="Admin Shio", password="adminpassword"
        )
        NewsletterSubscriber.objects.create(email="ana@x.com", consent_lgpd=True)
        NewsletterSubscriber.objects.create(email="bia@x.com", consent_lgpd=True)

    def test_registro_existe_como_enviando_antes_do_primeiro_email(self):
        seen = []

        def fake_send(*args, **kwargs):
            record = NewsletterSend.objects.get()
            seen.append((record.status, record.sent_count))
            return True

        with patch("notifications.newsletter.send_email", side_effect=fake_send):
            record = send_newsletter_campaign(self.admin, "Assunto", "Mensagem")

        # O registro já existia no 1º e-mail e o contador sobe a cada envio.
        self.assertEqual(
            seen,
            [(NewsletterSendStatus.SENDING, 0), (NewsletterSendStatus.SENDING, 1)],
        )
        self.assertEqual(record.status, NewsletterSendStatus.SENT)
        self.assertEqual(record.sent_count, 2)

    def test_erro_no_meio_marca_interrompido_e_guarda_o_que_saiu(self):
        with (
            patch(
                "notifications.newsletter.send_email",
                side_effect=[True, RuntimeError("boom")],
            ),
            self.assertRaises(RuntimeError),
        ):
            start_newsletter_campaign(self.admin, "Assunto", "Mensagem")

        record = NewsletterSend.objects.get()
        self.assertEqual(record.status, NewsletterSendStatus.INTERRUPTED)
        self.assertEqual(record.sent_count, 1)
        self.assertIsNotNone(record.finished_at)

    def test_segundo_envio_com_outro_em_andamento_levanta_erro(self):
        NewsletterSend.objects.create(
            subject="Em andamento", message="x", status=NewsletterSendStatus.SENDING
        )
        with self.assertRaises(NewsletterInProgress):
            start_newsletter_campaign(self.admin, "Outro", "Mensagem")
        self.assertEqual(NewsletterSend.objects.count(), 1)
        self.assertEqual(mail.outbox, [])

    @override_settings(NEWSLETTER_STALE_MINUTES=10)
    def test_envio_parado_vira_interrompido_e_libera_novo_envio(self):
        stuck = NewsletterSend.objects.create(
            subject="Travado", message="x", status=NewsletterSendStatus.SENDING
        )
        NewsletterSend.objects.filter(pk=stuck.pk).update(
            updated_at=timezone.now() - timedelta(minutes=11)
        )

        record = send_newsletter_campaign(self.admin, "Novo", "Mensagem")

        stuck.refresh_from_db()
        self.assertEqual(stuck.status, NewsletterSendStatus.INTERRUPTED)
        self.assertEqual(record.status, NewsletterSendStatus.SENT)

    @override_settings(NEWSLETTER_STALE_MINUTES=10)
    def test_envio_recente_nao_e_marcado_como_parado(self):
        NewsletterSend.objects.create(
            subject="Andando", message="x", status=NewsletterSendStatus.SENDING
        )
        self.assertEqual(expire_stale_sends(), 0)
        self.assertEqual(
            NewsletterSend.objects.get().status, NewsletterSendStatus.SENDING
        )

    @override_settings(NEWSLETTER_RUN_INLINE=False)
    def test_fora_dos_testes_roda_em_thread_e_fecha_conexao(self):
        captured = {}

        def fake_thread(target, daemon):
            captured["target"] = target
            captured["daemon"] = daemon
            captured["thread"] = MagicMock()
            return captured["thread"]

        func = MagicMock()
        with patch(
            "notifications.newsletter.threading.Thread", side_effect=fake_thread
        ):
            _run(func, "arg")
        self.assertIs(captured["daemon"], True)
        captured["thread"].start.assert_called_once_with()
        func.assert_not_called()
        with patch("notifications.newsletter.connection.close") as close:
            captured["target"]()
        func.assert_called_once_with("arg")
        close.assert_called_once_with()

    @override_settings(NEWSLETTER_RUN_INLINE=False)
    def test_erro_na_thread_e_registrado_no_log(self):
        captured = {}

        def fake_thread(target, daemon):
            captured["target"] = target
            return MagicMock()

        func = MagicMock(side_effect=RuntimeError("boom"))
        with patch(
            "notifications.newsletter.threading.Thread", side_effect=fake_thread
        ):
            _run(func, "arg")
        with (
            patch("notifications.newsletter.connection.close") as close,
            self.assertLogs("notifications.newsletter", level="ERROR"),
        ):
            captured["target"]()
        close.assert_called_once_with()
