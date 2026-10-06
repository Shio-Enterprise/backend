import datetime
import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from products.models import Category, DropCampaign, Product, ProductVariation

from .dashboard_aggregates import dashboard_detail_aggregates
from .metrics import METRICS_TIMEZONE, is_recurring_customer
from .models import (
    CustomerOrder,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentMethod,
    PaymentStatus,
)

User = get_user_model()


class DashboardAggregatesTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="admin-detail@example.com", name="Admin", is_staff=True
        )
        self.customer = User.objects.create_user(
            email="customer-detail@example.com", name="Customer"
        )

    def today_params(self):
        today = timezone.now().astimezone(METRICS_TIMEZONE).date().isoformat()
        return {"start_date": today, "end_date": today}

    def create_order(
        self,
        *,
        user=None,
        status=OrderStatus.DELIVERED,
        payment_status=PaymentStatus.PAID,
        total="100.00",
        method=PaymentMethod.PIX,
        paid_at=None,
    ):
        order = CustomerOrder.objects.create(
            user=user or self.customer,
            status=status,
            subtotal=total,
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
            method=method,
            status=initial_status,
            total_amount=total,
        )
        if paid_at is not None:
            Payment.objects.filter(pk=payment.pk).update(paid_at=paid_at)
        if payment_status == PaymentStatus.REFUNDED:
            Payment.objects.filter(pk=payment.pk).update(status=PaymentStatus.REFUNDED)
        return order

    def create_variation(self, name, drop, category, *, stock=20):
        product = Product.objects.create(
            name=name,
            description="Produto do dashboard",
            base_price="10.00",
            drop=drop,
            category=category,
        )
        return ProductVariation.objects.create(
            product=product,
            size="M",
            sku=f"DETAIL-{uuid.uuid4()}",
            stock_quantity=stock,
        )

    def add_item(self, order, variation, *, quantity=1, price="10.00"):
        return OrderItem.objects.create(
            order=order,
            variation=variation,
            quantity=quantity,
            unit_price=price,
            product_name=variation.product.name if variation else "Produto legado",
        )

    def test_financeiro_serie_diaria_e_metodo_respeitam_pagamento_local(self):
        jan_first_local = datetime.datetime(
            2026, 1, 1, 23, 30, tzinfo=METRICS_TIMEZONE
        )
        jan_third_local = datetime.datetime(
            2026, 1, 3, 12, 0, tzinfo=METRICS_TIMEZONE
        )
        self.create_order(
            total="100.00", paid_at=jan_first_local.astimezone(datetime.UTC)
        )
        self.create_order(
            status=OrderStatus.CANCELED,
            payment_status=PaymentStatus.REFUNDED,
            total="30.00",
            paid_at=jan_third_local,
            method=PaymentMethod.CREDIT_CARD,
        )
        self.create_order(
            status=OrderStatus.SHIPPED, total="50.00", paid_at=jan_first_local
        )

        result = dashboard_detail_aggregates(
            {"start_date": "2026-01-01", "end_date": "2026-01-03"}
        )

        self.assertEqual(
            result["financial"],
            {
                "gross_revenue": Decimal("100.00"),
                "refunds": Decimal("30.00"),
                "net_revenue": Decimal("70.00"),
                "average_ticket": Decimal("70.00"),
                "valid_sales": 1,
            },
        )
        self.assertEqual(
            [row["period"] for row in result["sales_series"]],
            ["2026-01-01", "2026-01-02", "2026-01-03"],
        )
        self.assertEqual(result["sales_series"][0]["gross_revenue"], 100)
        self.assertEqual(result["sales_series"][1]["net_revenue"], 0)
        self.assertEqual(result["sales_series"][2]["net_revenue"], -30)
        self.assertEqual(
            result["sales_by_payment_method"],
            [{"method": PaymentMethod.PIX, "valid_sales": 1, "gross_revenue": 100}],
        )

    def test_serie_mensal_preenche_mes_sem_vendas(self):
        self.create_order(
            paid_at=datetime.datetime(2026, 1, 31, 12, tzinfo=METRICS_TIMEZONE)
        )
        self.create_order(
            paid_at=datetime.datetime(2026, 3, 1, 12, tzinfo=METRICS_TIMEZONE)
        )

        result = dashboard_detail_aggregates(
            {
                "period": "annual",
                "start_date": "2026-01-31",
                "end_date": "2026-03-01",
            }
        )
        self.assertEqual(result["period"]["granularity"], "month")
        self.assertEqual(
            [row["period"] for row in result["sales_series"]],
            ["2026-01-01", "2026-02-01", "2026-03-01"],
        )
        self.assertEqual(result["sales_series"][1]["valid_sales"], 0)

    def test_itens_status_e_filtros_combinados_nao_multiplicam_pedidos(self):
        drop_a = DropCampaign.objects.create(name="A", slug="detail-a")
        drop_b = DropCampaign.objects.create(name="B", slug="detail-b")
        category_a = Category.objects.create(name="A", slug="detail-category-a")
        category_b = Category.objects.create(name="B", slug="detail-category-b")
        variation_a = self.create_variation("Produto A", drop_a, category_a)
        variation_b = self.create_variation("Produto B", drop_b, category_b)
        matching = self.create_variation("Correspondente", drop_a, category_b)

        mixed = self.create_order(total="110.00")
        self.add_item(mixed, variation_a, quantity=3, price="10.00")
        self.add_item(mixed, variation_b, price="40.00")
        selected = self.create_order(total="50.00")
        self.add_item(selected, matching, price="20.00")
        self.add_item(selected, None, price="5.00")
        refunded = self.create_order(
            status=OrderStatus.CANCELED,
            payment_status=PaymentStatus.REFUNDED,
            total="40.00",
        )
        self.add_item(refunded, variation_a, price="30.00")
        pending = self.create_order(
            status=OrderStatus.AWAITING_PAYMENT,
            payment_status=PaymentStatus.PENDING,
            total="25.00",
        )
        self.add_item(pending, variation_b, price="25.00")

        result = dashboard_detail_aggregates(self.today_params())
        self.assertEqual(result["financial"]["gross_revenue"], 160)
        self.assertEqual(result["financial"]["refunds"], 40)
        self.assertEqual(result["financial"]["net_revenue"], 120)
        self.assertEqual(result["financial"]["valid_sales"], 2)
        self.assertEqual(
            result["product_rankings"]["by_units"][0]["product_id"],
            variation_a.product_id,
        )
        self.assertEqual(
            result["product_rankings"]["by_revenue"][0]["product_id"],
            variation_b.product_id,
        )
        self.assertEqual(
            result["product_rankings"]["unclassified"],
            {"units": 1, "revenue": Decimal("5.00")},
        )
        drops = {
            row["drop_id"]: row["revenue"]
            for row in result["item_revenue"]["by_drop"]
        }
        self.assertEqual(drops[drop_a.id], 50)
        self.assertEqual(drops[drop_b.id], 40)
        self.assertEqual(drops[None], 5)
        categories = {
            row["category_id"]: row["revenue"]
            for row in result["item_revenue"]["by_category"]
        }
        self.assertEqual(categories[category_a.id], 30)
        self.assertEqual(categories[category_b.id], 60)
        self.assertEqual(categories[None], 5)
        statuses = {
            row["status"]: row["orders"]
            for row in result["orders_by_status"]["rows"]
        }
        self.assertEqual(result["orders_by_status"]["date_basis"], "order_created_at")
        self.assertEqual(statuses[OrderStatus.DELIVERED], 2)
        self.assertEqual(statuses[OrderStatus.AWAITING_PAYMENT], 1)
        self.assertEqual(statuses[OrderStatus.CANCELED], 1)

        params = {
            **self.today_params(),
            "drop": str(drop_a.id),
            "category": str(category_b.id),
        }
        filtered = dashboard_detail_aggregates(params)
        self.assertEqual(filtered["financial"]["gross_revenue"], 50)
        self.assertEqual(filtered["financial"]["valid_sales"], 1)
        self.assertEqual(filtered["item_revenue"]["by_drop"][0]["revenue"], 20)
        self.assertEqual(filtered["product_rankings"]["unclassified"]["units"], 0)
        self.assertEqual(
            filtered["orders_by_status"]["rows"],
            [{"status": OrderStatus.DELIVERED, "orders": 1}],
        )

    def test_clientes_recorrentes_e_estoque_usam_bases_proprias(self):
        other = User.objects.create_user(
            email="other-detail@example.com", name="Other"
        )
        User.objects.filter(pk=other.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=40)
        )
        drops = [
            DropCampaign.objects.create(name=f"Drop {i}", slug=f"detail-drop-{i}")
            for i in range(3)
        ]
        category = Category.objects.create(name="Estoque", slug="detail-stock")
        variations = [
            self.create_variation(f"Produto {i}", drop, category)
            for i, drop in enumerate(drops)
        ]
        for user, indexes in ((self.customer, (0, 1)), (other, (0, 2))):
            for index in indexes:
                order = self.create_order(user=user)
                self.add_item(order, variations[index])

        stock_product = Product.objects.create(
            name="Muitas variações",
            description="Produto do dashboard",
            base_price="10.00",
            drop=drops[0],
            category=category,
        )
        for index in range(55):
            ProductVariation.objects.create(
                product=stock_product,
                size=f"S{index}",
                sku=f"DETAIL-STOCK-{index}",
                stock_quantity=0 if index < 10 else 5,
            )

        result = dashboard_detail_aggregates(self.today_params())
        self.assertEqual(result["customers"]["total_registered"], 2)
        self.assertEqual(result["customers"]["new_in_period"], 1)
        self.assertEqual(result["customers"]["recurring_customers"], 1)
        self.assertTrue(is_recurring_customer(self.customer, self.today_params()))
        self.assertFalse(is_recurring_customer(other, self.today_params()))
        self.assertEqual(result["stock"]["out_count"], 10)
        self.assertEqual(result["stock"]["low_count"], 45)
        self.assertEqual(len(result["stock"]["attention"]), 50)
        self.assertTrue(
            all(
                row["admin_path"].startswith("/admin/stock/")
                for row in result["stock"]["attention"]
            )
        )

        filtered = dashboard_detail_aggregates(
            {**self.today_params(), "drop": str(drops[0].id)}
        )
        self.assertEqual(filtered["customers"]["total_registered"], 2)
        self.assertEqual(filtered["customers"]["new_in_period"], 1)
        self.assertEqual(filtered["customers"]["recurring_customers"], 0)
        self.assertEqual(filtered["stock"]["out_count"], 10)

        past_day = (
            timezone.now().astimezone(METRICS_TIMEZONE).date()
            - datetime.timedelta(days=180)
        ).isoformat()
        old_period = dashboard_detail_aggregates(
            {
                "start_date": past_day,
                "end_date": past_day,
                "drop": str(drops[0].id),
            }
        )
        self.assertEqual(old_period["stock"]["out_count"], 10)
        self.assertEqual(old_period["customers"]["total_registered"], 2)
        self.assertEqual(old_period["customers"]["new_in_period"], 0)

    def test_reembolso_sem_venda_preserva_receita_negativa(self):
        self.create_order(
            status=OrderStatus.CANCELED,
            payment_status=PaymentStatus.REFUNDED,
            total="30.00",
        )

        result = dashboard_detail_aggregates(self.today_params())
        self.assertEqual(result["financial"]["gross_revenue"], 0)
        self.assertEqual(result["financial"]["refunds"], 30)
        self.assertEqual(result["financial"]["net_revenue"], -30)
        self.assertEqual(result["financial"]["average_ticket"], 0)
        self.assertEqual(result["sales_series"][0]["net_revenue"], -30)

    def test_rankings_limitam_linhas_sem_limitar_totais(self):
        drop = DropCampaign.objects.create(name="Ranking", slug="detail-ranking")
        category = Category.objects.create(
            name="Ranking", slug="detail-ranking-category"
        )
        order = self.create_order(total="66.00")
        variations = [
            self.create_variation(f"Produto {index}", drop, category)
            for index in range(11)
        ]
        for index, variation in enumerate(variations):
            self.add_item(order, variation, quantity=index + 1, price="1.00")

        result = dashboard_detail_aggregates(self.today_params())
        self.assertEqual(len(result["product_rankings"]["by_units"]), 10)
        self.assertEqual(len(result["product_rankings"]["by_revenue"]), 10)
        self.assertEqual(
            result["product_rankings"]["by_units"][0]["product_id"],
            variations[-1].product_id,
        )
        self.assertEqual(result["item_revenue"]["by_drop"][0]["units"], 66)
        self.assertEqual(result["financial"]["gross_revenue"], 66)

    def test_periodo_sem_vendas_retorna_zeros_e_listas_vazias(self):
        result = dashboard_detail_aggregates(self.today_params())
        self.assertEqual(result["financial"]["net_revenue"], 0)
        self.assertEqual(result["financial"]["average_ticket"], 0)
        self.assertEqual(result["financial"]["valid_sales"], 0)
        self.assertEqual(len(result["sales_series"]), 1)
        self.assertEqual(result["product_rankings"]["by_units"], [])
        self.assertEqual(result["item_revenue"]["by_drop"], [])
        self.assertEqual(result["orders_by_status"]["rows"], [])
        self.assertEqual(result["sales_by_payment_method"], [])
        self.assertEqual(result["stock"]["attention"], [])
