"""Filtro por cliente na lista e na exportação de pedidos do admin (#88)."""

import csv
import io

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from orders.models import (
    CustomerOrder,
    OrderStatus,
    Refund,
    RefundReason,
    RefundStatus,
)

User = get_user_model()

LIST_URL = "/api/orders/admin/"
EXPORT_URL = "/api/orders/admin/export/"


class AdminOrderCustomerFilterTests(APITestCase):
    def setUp(self):
        admin = User.objects.create_user(
            email="admin88@x.com", name="Admin", is_staff=True
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(admin).access_token}"
        )
        self.maria = User.objects.create_user(email="maria@x.com", name="Maria")
        self.joao = User.objects.create_user(email="joao@x.com", name="João")
        self.sem_pedidos = User.objects.create_user(email="ana@x.com", name="Ana")

        self.maria_paid = self.order(self.maria, OrderStatus.PAID)
        self.maria_delivered = self.order(self.maria, OrderStatus.DELIVERED)
        self.joao_paid = self.order(self.joao, OrderStatus.PAID)

    def order(self, user, order_status):
        return CustomerOrder.objects.create(
            user=user,
            subtotal=50,
            total_amount=50,
            status=order_status,
            shipping_zip_code="70000000",
            shipping_street="Rua",
            shipping_number="1",
            shipping_neighborhood="Centro",
            shipping_city="Brasília",
            shipping_state="DF",
        )

    def list_ids(self, query):
        response = self.client.get(LIST_URL, query)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {o["id"] for o in response.json()}

    def test_lista_so_os_pedidos_do_cliente(self):
        ids = self.list_ids({"customer": self.maria.id})
        self.assertEqual(ids, {str(self.maria_paid.id), str(self.maria_delivered.id)})

    def test_combina_com_status(self):
        ids = self.list_ids({"customer": self.maria.id, "status": "PAID"})
        self.assertEqual(ids, {str(self.maria_paid.id)})

    def test_combina_com_reembolso(self):
        Refund.objects.create(
            order=self.maria_delivered,
            status=RefundStatus.REQUESTED,
            amount=50,
            reason=RefundReason.CUSTOMER_REQUEST,
        )
        Refund.objects.create(
            order=self.joao_paid,
            status=RefundStatus.REQUESTED,
            amount=50,
            reason=RefundReason.CUSTOMER_REQUEST,
        )
        ids = self.list_ids(
            {"customer": self.maria.id, "refund_status": RefundStatus.REQUESTED}
        )
        self.assertEqual(ids, {str(self.maria_delivered.id)})

    def test_cliente_sem_pedidos_devolve_lista_vazia(self):
        self.assertEqual(self.list_ids({"customer": self.sem_pedidos.id}), set())

    def test_cliente_inexistente_devolve_lista_vazia(self):
        self.assertEqual(self.list_ids({"customer": 999999}), set())

    def test_valor_invalido_devolve_400(self):
        for value in ["abc", "0", "-1", "1.5"]:
            with self.subTest(value=value):
                response = self.client.get(LIST_URL, {"customer": value})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("customer", response.json())

    def test_exportacao_respeita_o_filtro(self):
        response = self.client.get(
            EXPORT_URL, {"customer": self.maria.id, "file_format": "csv"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = (
            b"".join(response.streaming_content)
            if response.streaming
            else response.content
        )
        rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig")), delimiter=";"))
        emails = {row[3] for row in rows[1:]}
        self.assertEqual(emails, {"maria@x.com"})

    def test_exportacao_valor_invalido_devolve_400(self):
        response = self.client.get(EXPORT_URL, {"customer": "abc"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
