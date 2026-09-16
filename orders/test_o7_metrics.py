import datetime
import importlib
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.apps import apps
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from authentication.models import UserProfile, UserRole
from products.models import Category, DropCampaign, Product, ProductVariation

from .models import (
    CustomerOrder,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentMethod,
    PaymentStatus,
)

User = get_user_model()
SAO_PAULO = ZoneInfo("America/Sao_Paulo")
UTC = datetime.UTC


class O7MetricsTestCase(APITestCase):
    dashboard_url = "/api/orders/dashboard/summary/"
    drill_down_url = "/api/orders/dashboard/orders/"
    drop_revenue_url = "/api/orders/dashboard/drop-revenue/"
    crm_url = "/api/auth/crm/customers/"

    def setUp(self):
        self.admin = self.create_user("admin-o7@example.com", "Admin O7", UserRole.ADMIN)
        self.admin.is_staff = True
        self.admin.save(update_fields=["is_staff"])
        self.customer = self.create_user(
            "cliente-o7@example.com", "Cliente Principal", UserRole.CUSTOMER
        )
        self.client.force_authenticate(self.admin)

    def create_user(self, email, name, role):
        user = User.objects.create_user(email=email, name=name)
        UserProfile.objects.create(user=user, role=role)
        return user

    def create_order(
        self,
        *,
        user=None,
        order_status=OrderStatus.DELIVERED,
        payment_status=PaymentStatus.PAID,
        total="100.00",
        subtotal=None,
        shipping="0.00",
        discount="0.00",
        paid_at=None,
    ):
        user = user or self.customer
        order = CustomerOrder.objects.create(
            user=user,
            status=order_status,
            subtotal=subtotal or total,
            shipping_cost=shipping,
            discount_amount=discount,
            total_amount=total,
            shipping_zip_code="70000-000",
            shipping_street="Rua Teste",
            shipping_number="1",
            shipping_neighborhood="Centro",
            shipping_city="Brasília",
            shipping_state="DF",
        )
        initial_status = (
            PaymentStatus.PAID
            if payment_status == PaymentStatus.REFUNDED
            else payment_status
        )
        payment = Payment.objects.create(
            order=order,
            method=PaymentMethod.PIX,
            status=initial_status,
            total_amount=total,
        )
        if paid_at is not None:
            Payment.objects.filter(pk=payment.pk).update(paid_at=paid_at)
            payment.refresh_from_db()
        if payment_status == PaymentStatus.REFUNDED:
            payment.status = PaymentStatus.REFUNDED
            payment.save(update_fields=["status", "updated_at"])
        return order

    def create_product(self, *, name, drop, category, price="10.00"):
        product = Product.objects.create(
            name=name,
            description="Produto para teste da O7",
            base_price=price,
            drop=drop,
            category=category,
        )
        return ProductVariation.objects.create(
            product=product,
            size="M",
            sku=f"O7-{uuid.uuid4()}",
            stock_quantity=20,
        )

    def add_item(self, order, variation, quantity=1, unit_price="10.00"):
        return OrderItem.objects.create(
            order=order,
            variation=variation,
            quantity=quantity,
            unit_price=unit_price,
            product_name=variation.product.name,
        )

    def test_paid_at_e_definido_uma_vez_e_preservado_no_reembolso(self):
        order = self.create_order(payment_status=PaymentStatus.PENDING)
        payment = order.payment
        self.assertIsNone(payment.paid_at)

        payment.status = PaymentStatus.PAID
        payment.save(update_fields=["status", "updated_at"])
        first_paid_at = payment.paid_at
        self.assertIsNotNone(first_paid_at)

        payment.status = PaymentStatus.REFUNDED
        payment.save(update_fields=["status", "updated_at"])
        payment.refresh_from_db()
        self.assertEqual(payment.paid_at, first_paid_at)

    def test_backfill_usa_updated_at_para_pagamentos_antigos(self):
        order = self.create_order()
        Payment.objects.filter(pk=order.payment.pk).update(paid_at=None)
        payment = Payment.objects.get(pk=order.payment.pk)
        self.assertIsNone(payment.paid_at)

        migration = importlib.import_module("orders.migrations.0003_payment_paid_at")
        migration.backfill_paid_at(apps, None)

        payment.refresh_from_db()
        self.assertEqual(payment.paid_at, payment.updated_at)

    def test_periodo_anual_agrega_por_mes(self):
        today = timezone.now().astimezone(SAO_PAULO).date()
        current = datetime.datetime.combine(today, datetime.time(12), tzinfo=SAO_PAULO)
        previous = current - datetime.timedelta(days=40)
        self.create_order(total="80.00", paid_at=current)
        self.create_order(total="20.00", paid_at=previous)

        response = self.client.get(self.dashboard_url, {"period": "annual"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        self.assertEqual(payload["period"]["granularity"], "month")
        points = {point["period"]: Decimal(point["total_revenue"]) for point in payload["series"]}
        self.assertEqual(points[current.date().replace(day=1).isoformat()], Decimal("80.00"))
        self.assertEqual(points[previous.date().replace(day=1).isoformat()], Decimal("20.00"))

    def test_fronteira_diaria_usa_timezone_de_sao_paulo(self):
        inside_local_day = datetime.datetime(2026, 1, 2, 2, 30, tzinfo=UTC)
        next_local_day = datetime.datetime(2026, 1, 2, 3, 30, tzinfo=UTC)
        self.create_order(total="45.00", paid_at=inside_local_day)
        self.create_order(total="90.00", paid_at=next_local_day)

        response = self.client.get(
            self.dashboard_url,
            {"start_date": "2026-01-01", "end_date": "2026-01-01"},
        )

        payload = response.json()
        self.assertEqual(payload["sales_summary"]["total_orders"], 1)
        self.assertEqual(Decimal(payload["sales_summary"]["total_revenue"]), Decimal("45.00"))
        self.assertEqual(payload["series"][0]["period"], "2026-01-01")

    def test_drill_down_e_paginado_e_ordenado_por_paid_at(self):
        base = timezone.now().astimezone(SAO_PAULO) - datetime.timedelta(days=1)
        for index in range(21):
            self.create_order(total="10.00", paid_at=base + datetime.timedelta(minutes=index))

        first_page = self.client.get(self.drill_down_url)

        self.assertEqual(first_page.status_code, status.HTTP_200_OK)
        payload = first_page.json()
        self.assertEqual(payload["count"], 21)
        self.assertEqual(len(payload["results"]), 20)
        self.assertIsNotNone(payload["next"])
        dates = [item["paid_at"] for item in payload["results"]]
        self.assertEqual(dates, sorted(dates, reverse=True))

        second_page = self.client.get(self.drill_down_url, {"page": 2})
        self.assertEqual(len(second_page.json()["results"]), 1)

    def test_busca_unica_encontra_cliente_email_produto_drop_e_categoria(self):
        drop = DropCampaign.objects.create(name="Drop Esperança", slug="drop-esperanca")
        category = Category.objects.create(name="Moletons", slug="moletons-o7")
        variation = self.create_product(
            name="Moletom Caminho", drop=drop, category=category
        )
        order = self.create_order(total="10.00")
        self.add_item(order, variation)

        for term in [
            "Cliente Principal",
            "cliente-o7@example.com",
            "Moletom Caminho",
            "Drop Esperança",
            "Moletons",
        ]:
            with self.subTest(term=term):
                response = self.client.get(self.dashboard_url, {"search": term})
                self.assertEqual(response.json()["sales_summary"]["total_orders"], 1)

        response = self.client.get(self.dashboard_url, {"search": "Inexistente"})
        self.assertEqual(response.json()["sales_summary"]["total_orders"], 0)

    def test_recorrencia_exige_drops_consecutivos(self):
        base_date = timezone.now() - datetime.timedelta(days=30)
        drops = [
            DropCampaign.objects.create(
                name=f"Drop {index}",
                slug=f"drop-recorrencia-{index}",
                launch_date=base_date + datetime.timedelta(days=index),
            )
            for index in range(3)
        ]
        category = Category.objects.create(name="Recorrência", slug="recorrencia-o7")
        variations = [
            self.create_product(name=f"Produto {index}", drop=drop, category=category)
            for index, drop in enumerate(drops)
        ]
        consecutive = self.create_user(
            "consecutivo@example.com", "Cliente Consecutivo", UserRole.CUSTOMER
        )
        gap = self.create_user("intervalo@example.com", "Cliente Intervalo", UserRole.CUSTOMER)

        for user, indexes in [(consecutive, [0, 1]), (gap, [0, 2])]:
            for index in indexes:
                order = self.create_order(user=user)
                self.add_item(order, variations[index])

        response = self.client.get(self.crm_url)
        customers = response.json().get("results", response.json())
        by_email = {customer["email"]: customer for customer in customers}
        self.assertTrue(by_email["consecutivo@example.com"]["is_recurring"])
        self.assertFalse(by_email["intervalo@example.com"]["is_recurring"])

    def test_historico_exibe_todos_os_pedidos_e_status_comercial(self):
        self.create_order(order_status=OrderStatus.AWAITING_PAYMENT, payment_status=PaymentStatus.PENDING)
        self.create_order(order_status=OrderStatus.SHIPPED, payment_status=PaymentStatus.PAID)
        self.create_order(order_status=OrderStatus.DELIVERED, payment_status=PaymentStatus.PAID)
        self.create_order(order_status=OrderStatus.CANCELED, payment_status=PaymentStatus.REFUNDED)

        response = self.client.get(f"{self.crm_url}{self.customer.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        history = response.json()["order_history"]
        self.assertEqual(len(history), 4)
        self.assertEqual(
            {entry["status"] for entry in history},
            {"AWAITING_PAYMENT", "SHIPPED", "DELIVERED", "CANCELED"},
        )
        self.assertEqual(
            {entry["commercial_status"] for entry in history},
            {"NOT_REVENUE", "VALID_SALE", "REFUNDED"},
        )

    def test_pedido_com_dois_drops_nao_rateia_frete_e_desconto(self):
        category = Category.objects.create(name="Drops mistos", slug="drops-mistos-o7")
        drop_a = DropCampaign.objects.create(name="Drop A", slug="drop-a-o7")
        drop_b = DropCampaign.objects.create(name="Drop B", slug="drop-b-o7")
        variation_a = self.create_product(name="Produto A", drop=drop_a, category=category)
        variation_b = self.create_product(name="Produto B", drop=drop_b, category=category)
        order = self.create_order(
            subtotal="100.00", shipping="20.00", discount="10.00", total="110.00"
        )
        self.add_item(order, variation_a, quantity=2, unit_price="30.00")
        self.add_item(order, variation_b, quantity=1, unit_price="40.00")

        response = self.client.get(self.drop_revenue_url)

        revenue = {item["drop_id"]: Decimal(item["revenue"]) for item in response.json()}
        self.assertEqual(revenue[str(drop_a.id)], Decimal("60.00"))
        self.assertEqual(revenue[str(drop_b.id)], Decimal("40.00"))
        summary = self.client.get(self.dashboard_url).json()["sales_summary"]
        self.assertEqual(Decimal(summary["total_revenue"]), Decimal("110.00"))

    def test_reembolso_afeta_serie_temporal_e_total_gasto_do_crm(self):
        paid_at = timezone.now().astimezone(SAO_PAULO) - datetime.timedelta(hours=1)
        self.create_order(total="100.00", paid_at=paid_at)
        self.create_order(total="30.00", payment_status=PaymentStatus.REFUNDED, paid_at=paid_at)

        dashboard = self.client.get(self.dashboard_url).json()
        self.assertEqual(Decimal(dashboard["sales_summary"]["total_revenue"]), Decimal("70.00"))
        self.assertEqual(Decimal(dashboard["series"][0]["total_revenue"]), Decimal("70.00"))

        crm = self.client.get(self.crm_url, {"search": self.customer.email}).json()
        customers = crm.get("results", crm)
        self.assertEqual(customers[0]["total_orders"], 1)
        self.assertEqual(Decimal(customers[0]["total_spent"]), Decimal("70.00"))
