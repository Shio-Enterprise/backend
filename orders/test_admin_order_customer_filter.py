"""Filtro por cliente na lista e na exportação de pedidos do admin (#88)."""

import csv
import io

import openpyxl
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from authentication.admin_permissions import ADMIN_PERMISSION_CODES
from authentication.models import UserProfile, UserRole
from orders.models import (
    CustomerOrder,
    ExchangeReason,
    ExchangeRequest,
    ExchangeStatus,
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
        self.assertEqual(len(rows) - 1, 2)

    def test_exportacao_valor_invalido_devolve_400(self):
        response = self.client.get(EXPORT_URL, {"customer": "abc"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def export_rows(self, query):
        response = self.client.get(EXPORT_URL, query)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        if query.get("file_format") == "xlsx":
            sheet = openpyxl.load_workbook(io.BytesIO(response.content)).active
            return [list(r) for r in sheet.iter_rows(min_row=2, values_only=True)]
        content = (
            b"".join(response.streaming_content)
            if response.streaming
            else response.content
        )
        return list(
            csv.reader(io.StringIO(content.decode("utf-8-sig")), delimiter=";")
        )[1:]

    def test_combina_com_troca(self):
        for order in (self.maria_delivered, self.joao_paid):
            ExchangeRequest.objects.create(
                order=order,
                status=ExchangeStatus.REQUESTED,
                reason=ExchangeReason.SIZE_OR_COLOR,
            )
        ids = self.list_ids(
            {"customer": self.maria.id, "exchange_status": ExchangeStatus.REQUESTED}
        )
        self.assertEqual(ids, {str(self.maria_delivered.id)})

    def test_vazio_nao_filtra(self):
        ids = self.list_ids({"customer": ""})
        self.assertEqual(len(ids), 3)

    def test_exportacao_combina_cliente_e_busca(self):
        code = str(self.maria_paid.id)[:8].upper()
        rows = self.export_rows(
            {"customer": self.maria.id, "search": f"#{code}", "file_format": "csv"}
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], "maria@x.com")
        # A busca pelo código de um pedido de outro cliente não traz nada.
        other = str(self.joao_paid.id)[:8]
        self.assertEqual(
            self.export_rows(
                {"customer": self.maria.id, "search": other, "file_format": "csv"}
            ),
            [],
        )

    def test_exportacao_xlsx_respeita_o_filtro(self):
        rows = self.export_rows({"customer": self.joao.id, "file_format": "xlsx"})
        self.assertEqual([r[3] for r in rows], ["joao@x.com"])

    def test_sem_permissao_de_pedidos_recebe_403(self):
        token = RefreshToken.for_user(self.maria).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        for url in (LIST_URL, EXPORT_URL):
            with self.subTest(url=url):
                response = self.client.get(url, {"customer": self.maria.id})
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def admin_with(self, email, codenames):
        admin = User.objects.create_user(email=email, name="Admin", is_staff=True)
        UserProfile.objects.create(user=admin, role=UserRole.ADMIN)
        admin.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label="authentication",
                codename__in=codenames,
            )
        )
        self.assertTrue(set(codenames) <= set(ADMIN_PERMISSION_CODES))
        token = RefreshToken.for_user(admin).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_admin_so_de_clientes_nao_ve_pedidos_do_cliente(self):
        self.admin_with("crm88@x.com", ["manage_customers"])
        for url in (LIST_URL, EXPORT_URL):
            with self.subTest(url=url):
                response = self.client.get(url, {"customer": self.maria.id})
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_so_de_pedidos_usa_o_filtro(self):
        self.admin_with("pedidos88@x.com", ["manage_orders"])
        self.assertEqual(
            self.list_ids({"customer": self.joao.id}), {str(self.joao_paid.id)}
        )

    def test_exportacao_respeita_reembolso_e_troca(self):
        Refund.objects.create(
            order=self.maria_delivered,
            status=RefundStatus.REQUESTED,
            amount=50,
            reason=RefundReason.CUSTOMER_REQUEST,
        )
        ExchangeRequest.objects.create(
            order=self.joao_paid,
            status=ExchangeStatus.REQUESTED,
            reason=ExchangeReason.SIZE_OR_COLOR,
        )
        cases = [
            ({"refund_status": RefundStatus.REQUESTED}, {str(self.maria_delivered.id)}),
            ({"exchange_status": ExchangeStatus.REQUESTED}, {str(self.joao_paid.id)}),
            (
                {
                    "customer": self.maria.id,
                    "exchange_status": ExchangeStatus.REQUESTED,
                },
                set(),
            ),
        ]
        for query, expected in cases:
            with self.subTest(query=query):
                rows = self.export_rows({**query, "file_format": "csv"})
                self.assertEqual({r[0] for r in rows}, expected)

    def test_exportacao_status_invalido_devolve_400(self):
        for query in ({"refund_status": "XYZ"}, {"exchange_status": "XYZ"}):
            with self.subTest(query=query):
                response = self.client.get(EXPORT_URL, query)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
