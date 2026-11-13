from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from authentication.models import NewsletterSubscriber, UserProfile, UserRole
from orders.models import CustomerOrder, OrderItem, OrderRecoveryEmailSend, OrderStatus
from orders.order_email import (
    SUGGESTED_MESSAGES,
    order_email_subject,
    order_send_block_reason,
    render_order_email_preview,
    send_order_email_test,
    send_order_recovery_email,
)

User = get_user_model()

ROUTES = [
    "admin-order-email",
    "admin-order-email-preview",
    "admin-order-email-test",
    "admin-order-email-send",
]


def make_order(user, order_status=OrderStatus.AWAITING_PAYMENT, total="200.00"):
    order = CustomerOrder.objects.create(
        user=user,
        status=order_status,
        subtotal=Decimal(total),
        shipping_cost=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        total_amount=Decimal(total),
        shipping_zip_code="01001-000",
        shipping_street="Praça da Sé",
        shipping_number="100",
        shipping_neighborhood="Sé",
        shipping_city="São Paulo",
        shipping_state="SP",
    )
    OrderItem.objects.create(
        order=order,
        quantity=2,
        unit_price=Decimal("100.00"),
        product_name="Camiseta Shio Oversized",
        sku_snapshot="CAM-SHIO-M",
    )
    return order


def make_admin(email, name, permission_codenames=()):
    user = User.objects.create_user(
        email=email,
        password="adminpassword",
        name=name,
        is_staff=True,
    )
    UserProfile.objects.create(user=user, role=UserRole.ADMIN)
    if permission_codenames:
        perms = Permission.objects.filter(
            content_type__app_label="authentication",
            codename__in=permission_codenames,
        )
        user.user_permissions.set(perms)
    else:
        user.user_permissions.clear()
    return user


@override_settings(FRONTEND_URL="https://shio.com.br")
class OrderRecoveryEmailServiceTests(TestCase):
    def setUp(self):
        self.admin = make_admin(
            "admin@shio.com", "Admin Shio", permission_codenames=("manage_orders",)
        )
        self.customer = User.objects.create_user(
            email="maria@example.com", password="userpass123", name="Maria"
        )
        self.other_customer = User.objects.create_user(
            email="joao@example.com", password="userpass123", name="João"
        )

    def test_preview_contem_itens_total_link_e_escapa_html(self):
        order = make_order(self.customer, OrderStatus.AWAITING_PAYMENT, total="200.00")
        html = render_order_email_preview(
            order, "<script>alert(1)</script>\nFinalize sua compra"
        )
        self.assertIn("Olá, Maria!", html)
        self.assertIn("Camiseta Shio Oversized", html)
        self.assertIn("R$ 200,00", html)
        self.assertIn("https://shio.com.br", html)
        self.assertIn(f"https://shio.com.br/my-orders/{order.id}", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("<br>", html)

    def test_envio_vai_so_para_cliente_do_pedido_e_grava_historico(self):
        order = make_order(self.customer, OrderStatus.AWAITING_PAYMENT, total="200.00")
        record = send_order_recovery_email(order, self.admin, "Volte para concluir!")
        self.assertIsNotNone(record)
        self.assertEqual(record.order, order)
        self.assertEqual(record.sent_by, self.admin)
        self.assertEqual(record.message, "Volte para concluir!")

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ["maria@example.com"])
        self.assertEqual(sent.subject, order_email_subject(order))
        self.assertIn("Camiseta Shio Oversized", sent.body)
        self.assertIn("R$ 200,00", sent.body)
        self.assertIn("https://shio.com.br", sent.body)
        self.assertNotIn("joao@example.com", sent.body)

    def test_bloqueia_status_invalido_e_descadastrado(self):
        for forbidden in (
            OrderStatus.PAID,
            OrderStatus.PREPARING,
            OrderStatus.SHIPPED,
            OrderStatus.DELIVERED,
        ):
            order = make_order(self.customer, forbidden)
            self.assertIsNotNone(order_send_block_reason(order))

        pending = make_order(self.customer, OrderStatus.AWAITING_PAYMENT)
        self.assertIsNone(order_send_block_reason(pending))

        NewsletterSubscriber.objects.create(
            email="MARIA@example.com",
            consent_lgpd=True,
            unsubscribed_at=timezone.now(),
        )
        self.assertEqual(
            order_send_block_reason(pending),
            "O cliente pediu para não receber e-mails.",
        )

    def test_teste_vai_so_para_admin_e_nao_grava_historico(self):
        order = make_order(self.customer, OrderStatus.CANCELED)
        self.assertTrue(send_order_email_test(order, self.admin, "Prévia"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["admin@shio.com"])
        self.assertTrue(mail.outbox[0].subject.startswith("[Teste] "))
        self.assertEqual(OrderRecoveryEmailSend.objects.count(), 0)


@override_settings(FRONTEND_URL="https://shio.com.br")
class OrderRecoveryEmailApiTests(APITestCase):
    def setUp(self):
        self.admin = make_admin(
            "admin@shio.com", "Admin Pedidos", permission_codenames=("manage_orders",)
        )
        self.catalog_admin = make_admin(
            "catalog@shio.com",
            "Admin Catálogo",
            permission_codenames=("manage_catalog",),
        )
        self.customer = User.objects.create_user(
            email="cliente@example.com", password="userpass123", name="Ana Cliente"
        )
        self.awaiting_order = make_order(
            self.customer, OrderStatus.AWAITING_PAYMENT, total="200.00"
        )
        self.canceled_order = make_order(
            self.customer, OrderStatus.CANCELED, total="200.00"
        )
        self.client.force_authenticate(user=self.admin)

    def url(self, name, order=None):
        target = order or self.awaiting_order
        return reverse(f"orders:{name}", args=[target.id])

    def test_resumo_retorna_destinatario_sugestao_e_historico(self):
        OrderRecoveryEmailSend.objects.create(
            order=self.awaiting_order,
            sent_by=self.admin,
            message="Oi Ana",
        )
        response = self.client.get(self.url("admin-order-email"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertIsNone(data["block_reason"])
        self.assertFalse(data["unsubscribed"])
        self.assertEqual(
            data["recipient"],
            {"name": "Ana Cliente", "email": "cliente@example.com"},
        )
        self.assertEqual(
            data["suggested_message"],
            SUGGESTED_MESSAGES[OrderStatus.AWAITING_PAYMENT],
        )
        self.assertEqual(len(data["history"]), 1)
        self.assertEqual(data["history"][0]["sent_by_name"], "Admin Pedidos")
        self.assertIn("sent_at", data["history"][0])

    def test_envio_para_aguardando_pagamento_e_cancelado(self):
        for order in (self.awaiting_order, self.canceled_order):
            mail.outbox.clear()
            response = self.client.post(
                self.url("admin-order-email-send", order),
                {"message": "Conclua seu pedido!"},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            self.assertEqual(response.data["recipient_email"], "cliente@example.com")
            self.assertEqual(response.data["sent_by_name"], "Admin Pedidos")
            self.assertIn("sent_at", response.data)
            self.assertEqual(len(mail.outbox), 1)
            self.assertEqual(mail.outbox[0].to, ["cliente@example.com"])
            self.assertIn("Conclua seu pedido!", mail.outbox[0].body)
            self.assertEqual(order.recovery_email_sends.count(), 1)

    def test_recusa_envio_para_outros_status(self):
        for order_status in (
            OrderStatus.PAID,
            OrderStatus.PREPARING,
            OrderStatus.SHIPPED,
            OrderStatus.DELIVERED,
        ):
            order = make_order(self.customer, order_status)
            summary = self.client.get(self.url("admin-order-email", order))
            self.assertEqual(summary.status_code, status.HTTP_200_OK)
            self.assertIsNotNone(summary.data["block_reason"])

            response = self.client.post(
                self.url("admin-order-email-send", order),
                {"message": "Teste"},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertIn("error", response.data)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(OrderRecoveryEmailSend.objects.count(), 0)

    def test_recusa_envio_quando_cliente_descadastrado(self):
        NewsletterSubscriber.objects.create(
            email="cliente@example.com",
            consent_lgpd=True,
            unsubscribed_at=timezone.now(),
        )
        summary = self.client.get(self.url("admin-order-email"))
        self.assertTrue(summary.data["unsubscribed"])
        self.assertEqual(
            summary.data["block_reason"],
            "O cliente pediu para não receber e-mails.",
        )

        response = self.client.post(
            self.url("admin-order-email-send"),
            {"message": "Oi"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(OrderRecoveryEmailSend.objects.count(), 0)

    def test_serializers_admin_incluem_registro_de_envio_sem_expor_ao_cliente(self):
        OrderRecoveryEmailSend.objects.create(
            order=self.awaiting_order,
            sent_by=self.admin,
            message="Lembrete",
        )
        list_resp = self.client.get(reverse("orders:admin_orders_list"))
        self.assertEqual(list_resp.status_code, status.HTTP_200_OK)
        item = next(
            row
            for row in list_resp.data
            if str(row["id"]) == str(self.awaiting_order.id)
        )
        self.assertIsNotNone(item["last_recovery_email_sent_at"])

        detail_resp = self.client.get(
            reverse("orders:admin_orders_detail", args=[self.awaiting_order.id])
        )
        self.assertEqual(detail_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(detail_resp.data["recovery_email_sends"]), 1)
        self.assertEqual(
            detail_resp.data["recovery_email_sends"][0]["sent_by_name"],
            "Admin Pedidos",
        )

        self.client.force_authenticate(user=self.customer)
        customer_resp = self.client.get(
            reverse("orders:user_orders_detail", args=[self.awaiting_order.id])
        )
        self.assertEqual(customer_resp.status_code, status.HTTP_200_OK)
        self.assertNotIn("recovery_email_sends", customer_resp.data)

    def test_preview_e_teste_e_falha_502(self):
        preview = self.client.post(
            self.url("admin-order-email-preview"),
            {"message": "Texto customizado"},
            format="json",
        )
        self.assertEqual(preview.status_code, status.HTTP_200_OK)
        self.assertIn("Texto customizado", preview.data["html"])
        self.assertIn("Camiseta Shio Oversized", preview.data["html"])

        test_resp = self.client.post(
            self.url("admin-order-email-test"),
            {"message": "Mensagem de teste"},
            format="json",
        )
        self.assertEqual(test_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["admin@shio.com"])
        self.assertEqual(OrderRecoveryEmailSend.objects.count(), 0)

        with patch("orders.views.send_order_recovery_email", return_value=None):
            fail_resp = self.client.post(
                self.url("admin-order-email-send"),
                {"message": "Oi"},
                format="json",
            )
        self.assertEqual(fail_resp.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(OrderRecoveryEmailSend.objects.count(), 0)

    def test_pedido_inexistente_retorna_404(self):
        url = reverse(
            "orders:admin-order-email",
            args=["00000000-0000-0000-0000-000000000000"],
        )
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)

    def test_mensagem_maior_que_limite_retorna_400(self):
        response = self.client.post(
            self.url("admin-order-email-send"),
            {"message": "x" * 501},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_exige_permissao_manage_orders(self):
        self.client.force_authenticate(user=None)
        for route in ROUTES:
            url = self.url(route)
            resp = (
                self.client.get(url)
                if route == "admin-order-email"
                else self.client.post(url, {}, format="json")
            )
            self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

        for unauthorized in (self.customer, self.catalog_admin):
            self.client.force_authenticate(user=unauthorized)
            for route in ROUTES:
                url = self.url(route)
                resp = (
                    self.client.get(url)
                    if route == "admin-order-email"
                    else self.client.post(url, {}, format="json")
                )
                self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
