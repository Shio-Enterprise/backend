from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import mail
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from authentication.models import (
    NewsletterSend,
    NewsletterSendStatus,
    NewsletterSubscriber,
    UserProfile,
    UserRole,
)
from notifications.unsubscribe import make_unsubscribe_token

User = get_user_model()

ROUTES = [
    "authentication:admin-newsletter-summary",
    "authentication:admin-newsletter-preview",
    "authentication:admin-newsletter-test",
    "authentication:admin-newsletter-send",
]


class NewsletterApiTests(APITestCase):
    def setUp(self):
        # Superusuário (tem todas as permissões)
        self.superadmin = User.objects.create_superuser(
            email="superadmin@shio.com",
            password="adminpassword",
            name="Super Admin",
        )

        # Admin com permissão explícita de manage_customers
        self.admin = User.objects.create_user(
            email="admin@shio.com",
            password="adminpassword",
            name="Admin Shio",
            is_staff=True,
        )
        UserProfile.objects.create(user=self.admin, role=UserRole.ADMIN)

        # Admin sem permissão de manage_customers
        self.limited_admin = User.objects.create_user(
            email="limited@shio.com",
            password="adminpassword",
            name="Admin Limitado",
            is_staff=True,
        )
        UserProfile.objects.create(user=self.limited_admin, role=UserRole.ADMIN)
        # Remove a permissão manage_customers
        cust_perm = Permission.objects.filter(
            content_type__app_label="authentication",
            codename="manage_customers",
        ).first()
        if cust_perm:
            self.limited_admin.user_permissions.remove(cust_perm)

        # Usuário comum (cliente)
        self.customer = User.objects.create_user(
            email="cliente@shio.com", password="clientepassword", name="Cliente Normal"
        )
        UserProfile.objects.create(user=self.customer, role=UserRole.CUSTOMER)

        self.client.force_authenticate(user=self.admin)

    def subscribe(self, *emails):
        for email in emails:
            NewsletterSubscriber.objects.create(email=email, consent_lgpd=True)

    def test_resumo_com_total_e_historico(self):
        self.subscribe("a@x.com", "b@x.com")
        NewsletterSend.objects.create(
            subject="Novidades",
            message="Linha 1",
            sent_by=self.admin,
            sent_count=2,
            failed_count=0,
        )

        url = reverse("authentication:admin-newsletter-summary")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["total_subscribers"], 2)
        self.assertEqual(len(resp.data["history"]), 1)
        self.assertEqual(resp.data["history"][0]["subject"], "Novidades")
        self.assertEqual(resp.data["history"][0]["sent_by_name"], "Admin Shio")
        self.assertEqual(resp.data["history"][0]["sent_count"], 2)
        self.assertEqual(resp.data["history"][0]["failed_count"], 0)

    def test_preview_retorna_html_renderizado(self):
        url = reverse("authentication:admin-newsletter-preview")
        resp = self.client.post(
            url,
            {"subject": "Lançamento", "message": "Texto promocional aqui"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("html", resp.data)
        self.assertIn("Lançamento", resp.data["html"])
        self.assertIn("Texto promocional aqui", resp.data["html"])

    def test_teste_envia_para_o_proprio_admin(self):
        url = reverse("authentication:admin-newsletter-test")
        resp = self.client.post(
            url,
            {"subject": "Teste Drop", "message": "Mensagem para validar layout"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, {"sent": True, "to": "admin@shio.com"})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["admin@shio.com"])
        self.assertEqual(mail.outbox[0].subject, "[Teste] Teste Drop")
        self.assertEqual(NewsletterSend.objects.count(), 0)

    def test_teste_falha_retorna_502(self):
        url = reverse("authentication:admin-newsletter-test")
        with patch("authentication.views.send_newsletter_test", return_value=False):
            resp = self.client.post(
                url,
                {"subject": "Assunto", "message": "Mensagem"},
                format="json",
            )
        self.assertEqual(resp.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(resp.data, {"error": "Não foi possível enviar o teste."})

    def test_envio_real_envia_e_cria_historico(self):
        self.subscribe("ana@x.com", "bia@x.com")
        url = reverse("authentication:admin-newsletter-send")
        resp = self.client.post(
            url,
            {"subject": "Drop Exclusivo", "message": "Disponível a partir de hoje!"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        # Nos testes o envio roda inline, então já volta concluído.
        self.assertEqual(resp.data["status"], NewsletterSendStatus.SENT)
        self.assertEqual(resp.data["sent_count"], 2)
        self.assertEqual(resp.data["failed_count"], 0)
        self.assertEqual(resp.data["total_count"], 2)
        self.assertEqual(resp.data["subject"], "Drop Exclusivo")
        self.assertIn("sent_at", resp.data)
        self.assertIn("id", resp.data)

        self.assertEqual(len(mail.outbox), 2)
        record = NewsletterSend.objects.get()
        self.assertEqual(record.subject, "Drop Exclusivo")
        self.assertEqual(record.sent_by, self.admin)
        self.assertEqual(record.sent_count, 2)
        self.assertEqual(record.failed_count, 0)

    def test_envio_com_outro_em_andamento_retorna_409(self):
        self.subscribe("ana@x.com")
        NewsletterSend.objects.create(
            subject="Em andamento",
            message="x",
            sent_by=self.admin,
            status=NewsletterSendStatus.SENDING,
        )
        url = reverse("authentication:admin-newsletter-send")
        resp = self.client.post(
            url, {"subject": "Outro", "message": "Mensagem"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            resp.data, {"error": "Já existe um envio de newsletter em andamento."}
        )
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(NewsletterSend.objects.count(), 1)

    def test_resumo_mostra_status_do_envio(self):
        NewsletterSend.objects.create(
            subject="Em andamento",
            message="x",
            sent_by=self.admin,
            status=NewsletterSendStatus.SENDING,
            total_count=10,
            sent_count=3,
        )
        resp = self.client.get(reverse("authentication:admin-newsletter-summary"))
        item = resp.data["history"][0]
        self.assertEqual(item["status"], NewsletterSendStatus.SENDING)
        self.assertEqual(item["total_count"], 10)
        self.assertEqual(item["sent_count"], 3)
        self.assertIsNone(item["finished_at"])

    def test_envio_sem_publico_retorna_400(self):
        url = reverse("authentication:admin-newsletter-send")
        resp = self.client.post(
            url,
            {"subject": "Sem Público", "message": "Mensagem"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data, {"error": "Nenhum inscrito elegível para receber."})
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(NewsletterSend.objects.count(), 0)

    def test_validacao_campos_obrigatorios(self):
        url = reverse("authentication:admin-newsletter-send")
        # Sem assunto
        resp = self.client.post(url, {"subject": "", "message": "Texto"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        # Sem mensagem
        resp = self.client.post(
            url, {"subject": "Assunto", "message": ""}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_usuario_sem_permissao_de_admin_nao_consegue_enviar(self):
        self.client.force_authenticate(user=self.customer)
        for route_name in ROUTES:
            with self.subTest(route=route_name):
                if route_name.endswith("-summary"):
                    resp = self.client.get(reverse(route_name))
                else:
                    resp = self.client.post(
                        reverse(route_name),
                        {"subject": "x", "message": "y"},
                        format="json",
                    )
                self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_usuario_anonimo_recebe_401(self):
        self.client.force_authenticate(user=None)
        for route_name in ROUTES:
            with self.subTest(route=route_name):
                if route_name.endswith("-summary"):
                    resp = self.client.get(reverse(route_name))
                else:
                    resp = self.client.post(
                        reverse(route_name),
                        {"subject": "x", "message": "y"},
                        format="json",
                    )
                self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_admin_sem_permissao_de_clientes_recebe_403(self):
        self.client.force_authenticate(user=self.limited_admin)
        for route_name in ROUTES:
            with self.subTest(route=route_name):
                if route_name.endswith("-summary"):
                    resp = self.client.get(reverse(route_name))
                else:
                    resp = self.client.post(
                        reverse(route_name),
                        {"subject": "x", "message": "y"},
                        format="json",
                    )
                self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_link_cancelamento_invalido_ou_adulterado_nao_cancela_outro(self):
        """Critério mínimo: link de cancelamento inválido/adulterado não descadastra ninguém."""
        sub = NewsletterSubscriber.objects.create(
            email="vitima@shio.com", consent_lgpd=True
        )
        token_valido = make_unsubscribe_token("vitima@shio.com")
        token_adulterado = token_valido + "hacked"

        unsub_url = reverse(
            "authentication:marketing-unsubscribe", args=[token_adulterado]
        )
        resp = self.client.post(unsub_url)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

        # Garante que o status do assinante não foi alterado
        sub.refresh_from_db()
        self.assertIsNone(sub.unsubscribed_at)
        self.assertTrue(sub.consent_lgpd)
