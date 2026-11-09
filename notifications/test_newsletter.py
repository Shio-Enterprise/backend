from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from authentication.models import NewsletterSend, NewsletterSubscriber
from notifications.newsletter import (
    get_newsletter_audience,
    render_newsletter_preview,
    send_newsletter_campaign,
    send_newsletter_test,
)

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
        self.assertEqual(mail.outbox[0].subject, "Assunto Teste")
        self.assertEqual(NewsletterSend.objects.count(), 0)

    def test_preview_renderiza_html_com_quebras_e_descadastro(self):
        html = render_newsletter_preview(
            subject="Novidades",
            message="<script>alert(1)</script>\nLinha 2",
            name="Fulano",
        )
        self.assertIn("Novidades", html)
        self.assertIn("Olá, Fulano!", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn("<br>", html)
        self.assertIn("Cancelar inscrição", html)
        self.assertIn("/descadastrar/exemplo", html)
