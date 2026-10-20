from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from authentication.models import NewsletterSubscriber
from orders.models import Coupon, CouponDiscountType, CouponEmailSend

User = get_user_model()

ROUTES = [
    "admin-coupon-email",
    "admin-coupon-email-preview",
    "admin-coupon-email-test",
    "admin-coupon-email-send",
]


class CouponEmailApiTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="admin@shio.com", password="adminpassword", name="Admin Shio"
        )
        self.user = User.objects.create_user(
            email="user@shio.com", password="userpassword"
        )
        self.coupon = Coupon.objects.create(
            code="VERAO10",
            discount_type=CouponDiscountType.PERCENTAGE,
            discount_value=10,
            is_active=True,
        )
        self.client.force_authenticate(user=self.admin)

    def url(self, name, coupon=None):
        return reverse(f"orders:{name}", args=[(coupon or self.coupon).id])

    def subscribe(self, *emails):
        for email in emails:
            NewsletterSubscriber.objects.create(email=email, consent_lgpd=True)

    def test_resumo_com_publico_e_historico(self):
        self.subscribe("a@x.com", "b@x.com")
        CouponEmailSend.objects.create(
            coupon=self.coupon,
            sent_by=self.admin,
            message="oi",
            sent_count=2,
            failed_count=1,
        )
        response = self.client.get(self.url("admin-coupon-email"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertIsNone(data["block_reason"])
        self.assertEqual(data["audience"]["total"], 2)
        self.assertEqual(data["audience"]["subscribers"], 2)
        for key in ("buyers", "overlap", "suppressed"):
            self.assertIn(key, data["audience"])
        self.assertEqual(len(data["history"]), 1)
        self.assertEqual(data["history"][0]["sent_by_name"], "Admin Shio")
        self.assertEqual(data["history"][0]["sent_count"], 2)
        self.assertEqual(data["history"][0]["failed_count"], 1)
        self.assertIn("sent_at", data["history"][0])

    def test_resumo_mostra_motivo_de_bloqueio(self):
        self.coupon.is_active = False
        self.coupon.save()
        response = self.client.get(self.url("admin-coupon-email"))
        self.assertEqual(response.data["block_reason"], "Cupom inativo.")

    def test_cupom_inexistente_retorna_404(self):
        url = reverse(
            "orders:admin-coupon-email",
            args=["00000000-0000-0000-0000-000000000000"],
        )
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_preview_contem_codigo_e_mensagem(self):
        response = self.client.post(
            self.url("admin-coupon-email-preview"),
            {"message": "Aproveite muito"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("VERAO10", response.data["html"])
        self.assertIn("Aproveite muito", response.data["html"])

    def test_teste_envia_para_admin(self):
        response = self.client.post(
            self.url("admin-coupon-email-test"), {"message": "oi"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"sent": True})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["admin@shio.com"])
        self.assertEqual(CouponEmailSend.objects.count(), 0)

    def test_teste_falha_retorna_502(self):
        with patch("orders.views.send_coupon_test", return_value=False):
            response = self.client.post(
                self.url("admin-coupon-email-test"), {"message": ""}, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(response.data, {"error": "Não foi possível enviar o teste."})

    def test_envio_cria_historico_e_envia(self):
        self.subscribe("a@x.com", "b@x.com")
        response = self.client.post(
            self.url("admin-coupon-email-send"), {"message": "oi"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["sent_count"], 2)
        self.assertEqual(response.data["failed_count"], 0)
        self.assertIn("sent_at", response.data)
        self.assertEqual(len(mail.outbox), 2)
        record = CouponEmailSend.objects.get()
        self.assertEqual(record.sent_by, self.admin)
        self.assertEqual(record.message, "oi")

    def test_envio_recusado_se_cupom_ficou_inativo(self):
        self.subscribe("a@x.com")
        self.coupon.is_active = False
        self.coupon.save()
        response = self.client.post(
            self.url("admin-coupon-email-send"), {"message": "oi"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {"error": "Cupom inativo."})
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(CouponEmailSend.objects.count(), 0)

    def test_envio_sem_publico_retorna_400(self):
        response = self.client.post(
            self.url("admin-coupon-email-send"), {"message": "oi"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {"error": "Ninguém para receber."})
        self.assertEqual(CouponEmailSend.objects.count(), 0)

    def test_mensagem_acima_de_500_retorna_400(self):
        self.subscribe("a@x.com")
        for name in ("preview", "test", "send"):
            response = self.client.post(
                self.url(f"admin-coupon-email-{name}"),
                {"message": "x" * 501},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, name)
        self.assertEqual(len(mail.outbox), 0)

    def test_usuario_comum_recebe_403(self):
        self.client.force_authenticate(user=self.user)
        self.assertEqual(
            self.client.get(self.url("admin-coupon-email")).status_code, 403
        )
        for name in ROUTES[1:]:
            response = self.client.post(self.url(name), {"message": ""}, format="json")
            self.assertEqual(response.status_code, 403, name)

    def test_sem_login_recebe_401(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(
            self.client.get(self.url("admin-coupon-email")).status_code, 401
        )
        for name in ROUTES[1:]:
            response = self.client.post(self.url(name), {"message": ""}, format="json")
            self.assertEqual(response.status_code, 401, name)
