import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from products.models import Category, DropCampaign, Product, ProductVariation

from .metrics import METRICS_TIMEZONE
from .models import (
    CustomerOrder,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentMethod,
    PaymentStatus,
)

User = get_user_model()


class DashboardApiTests(TestCase):
    detail_url = "/api/orders/dashboard/detail/"
    orders_url = "/api/orders/dashboard/orders/"

    def setUp(self):
        self.admin = User.objects.create_user(
            email="admin-api-detail@example.com", name="Admin", is_staff=True
        )
        self.customer = User.objects.create_user(
            email="customer-api-detail@example.com", name="Cliente"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def today_params(self):
        today = timezone.now().astimezone(METRICS_TIMEZONE).date().isoformat()
        return {"start_date": today, "end_date": today}

    def create_order(
        self,
        *,
        status=OrderStatus.DELIVERED,
        payment_status=PaymentStatus.PAID,
        total="100.00",
        method=PaymentMethod.PIX,
    ):
        order = CustomerOrder.objects.create(
            user=self.customer,
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
        payment = Payment.objects.create(
            order=order,
            method=method,
            status=(
                PaymentStatus.PAID
                if payment_status == PaymentStatus.REFUNDED
                else payment_status
            ),
            total_amount=total,
        )
        if payment_status == PaymentStatus.REFUNDED:
            payment.status = PaymentStatus.REFUNDED
            payment.save(update_fields=["status", "updated_at"])
        return order

    def create_variation(self, name, drop, category):
        product = Product.objects.create(
            name=name,
            description="Produto do teste de API",
            base_price="10.00",
            drop=drop,
            category=category,
        )
        return ProductVariation.objects.create(
            product=product,
            size="M",
            sku=f"DETAIL-API-{uuid.uuid4()}",
            stock_quantity=20,
        )

    def add_item(self, order, variation, *, quantity=1, price="10.00"):
        OrderItem.objects.create(
            order=order,
            variation=variation,
            product_name=variation.product.name if variation else "Produto legado",
            quantity=quantity,
            unit_price=price,
        )

    def test_acesso_administrativo_em_ambos_endpoints(self):
        for url in (self.detail_url, self.orders_url):
            with self.subTest(url=url):
                self.client.force_authenticate(user=None)
                self.assertEqual(self.client.get(url).status_code, 401)
                self.client.force_authenticate(user=self.customer)
                self.assertEqual(self.client.get(url).status_code, 403)
                self.client.force_authenticate(user=self.admin)
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_detalhe_retorna_contrato_e_decimais_como_strings(self):
        self.create_order(total="25.00")
        result = self.client.get(self.detail_url, self.today_params())

        self.assertEqual(result.status_code, 200)
        payload = result.json()
        self.assertEqual(payload["financial"]["gross_revenue"], "25.00")
        self.assertEqual(payload["financial"]["valid_sales"], 1)
        self.assertEqual(payload["period"]["timezone"], "America/Sao_Paulo")
        self.assertEqual(payload["sales_series"][0]["net_revenue"], "25.00")
        self.assertEqual(payload["orders_by_status"]["date_basis"], "order_created_at")
        self.assertIn("product_rankings", payload)
        self.assertIn("item_revenue", payload)
        self.assertIn("customers", payload)
        self.assertIn("stock", payload)

    def test_filtros_e_seletores_invalidos_retornam_400(self):
        for params in (
            {"period": "quarterly"},
            {"start_date": "06-10-2026"},
            {"drop": "invalido"},
        ):
            with self.subTest(params=params):
                response = self.client.get(self.detail_url, params)
                self.assertEqual(response.status_code, 400)

        for params in (
            {"metric": "desconhecida"},
            {"metric": "valid_sales", "customer": "invalido"},
            {"metric": "status"},
            {"metric": "status", "status": "INEXISTENTE"},
            {"metric": "payment_method"},
            {"metric": "product_units", "product_id": "invalido"},
            {"metric": "drop_item_revenue"},
            {"metric": "category_item_revenue"},
            {"metric": "product_units", "unclassified": "talvez"},
        ):
            with self.subTest(params=params):
                response = self.client.get(self.orders_url, params)
                self.assertEqual(response.status_code, 400)

    def test_seletores_preservam_busca_e_cliente_do_resumo(self):
        selected = self.create_order(total="25.00")
        other = self.create_order(total="40.00")
        other.user = User.objects.create_user(
            email="other-api-detail@example.com", name="Outra Pessoa"
        )
        other.save(update_fields=["user", "updated_at"])

        for extra in (
            {"search": "Cliente"},
            {"customer": str(self.customer.id)},
        ):
            with self.subTest(extra=extra):
                params = {**self.today_params(), **extra}
                summary = self.client.get(
                    "/api/orders/dashboard/summary/", params
                ).json()
                detail = self.client.get(
                    self.orders_url, {**params, "metric": "valid_sales"}
                ).json()
                self.assertEqual(summary["sales_summary"]["total_orders"], 1)
                self.assertEqual(detail["count"], 1)
                self.assertEqual(detail["results"][0]["id"], str(selected.id))

    def test_drill_down_corresponde_aos_agregados(self):
        drop_a = DropCampaign.objects.create(name="Drop A", slug="detail-api-a")
        drop_b = DropCampaign.objects.create(name="Drop B", slug="detail-api-b")
        category_a = Category.objects.create(name="A", slug="detail-api-cat-a")
        category_b = Category.objects.create(name="B", slug="detail-api-cat-b")
        variation_a = self.create_variation("A", drop_a, category_a)
        variation_b = self.create_variation("B", drop_b, category_b)
        selected_variation = self.create_variation("Selecionado", drop_a, category_b)

        mixed = self.create_order(total="110.00")
        self.add_item(mixed, variation_a)
        self.add_item(mixed, variation_b)
        selected = self.create_order(total="50.00")
        self.add_item(selected, selected_variation, quantity=2, price="10.00")
        self.add_item(selected, None, price="5.00")
        refunded = self.create_order(
            status=OrderStatus.CANCELED,
            payment_status=PaymentStatus.REFUNDED,
            total="30.00",
        )
        self.add_item(refunded, variation_a)
        pending = self.create_order(
            status=OrderStatus.AWAITING_PAYMENT,
            payment_status=PaymentStatus.PENDING,
            total="20.00",
        )
        self.add_item(pending, selected_variation)
        Payment.objects.filter(order=pending).delete()

        filters = {
            **self.today_params(),
            "drop": str(drop_a.id),
            "category": str(category_b.id),
        }
        detail = self.client.get(self.detail_url, filters).json()
        self.assertEqual(detail["financial"]["valid_sales"], 1)
        self.assertEqual(detail["financial"]["gross_revenue"], "50.00")

        sales = self.client.get(
            self.orders_url, {**filters, "metric": "valid_sales"}
        ).json()
        self.assertEqual(sales["count"], detail["financial"]["valid_sales"])
        self.assertEqual([row["id"] for row in sales["results"]], [str(selected.id)])
        self.assertEqual(
            sales["results"][0]["admin_path"], f"/admin/orders/{selected.id}"
        )

        status_rows = self.client.get(
            self.orders_url,
            {**filters, "metric": "status", "status": OrderStatus.AWAITING_PAYMENT},
        ).json()
        self.assertEqual(status_rows["date_basis"], "order_created_at")
        self.assertEqual(status_rows["count"], 1)
        self.assertEqual(status_rows["results"][0]["id"], str(pending.id))
        self.assertIsNone(status_rows["results"][0]["paid_at"])
        self.assertIsNone(status_rows["results"][0]["payment_status"])
        pending_total = next(
            row["orders"]
            for row in detail["orders_by_status"]["rows"]
            if row["status"] == OrderStatus.AWAITING_PAYMENT
        )
        self.assertEqual(status_rows["count"], pending_total)

        product_rows = self.client.get(
            self.orders_url,
            {
                **filters,
                "metric": "product_units",
                "product_id": str(selected_variation.product_id),
            },
        ).json()
        self.assertEqual(product_rows["count"], 1)
        self.assertEqual(product_rows["results"][0]["metric_units"], 2)
        self.assertEqual(product_rows["results"][0]["metric_item_revenue"], "20.00")
        self.assertEqual(
            product_rows["results"][0]["metric_units"],
            detail["product_rankings"]["by_units"][0]["units"],
        )
        self.assertEqual(
            Decimal(product_rows["results"][0]["metric_item_revenue"]),
            Decimal(detail["item_revenue"]["by_drop"][0]["revenue"]),
        )
        for metric in ("drop_item_revenue", "category_item_revenue"):
            with self.subTest(metric=metric):
                group_rows = self.client.get(
                    self.orders_url, {**filters, "metric": metric}
                ).json()
                self.assertEqual(group_rows["count"], 1)
                self.assertEqual(
                    group_rows["results"][0]["metric_item_revenue"], "20.00"
                )

        method_rows = self.client.get(
            self.orders_url,
            {
                **filters,
                "metric": "payment_method",
                "payment_method": PaymentMethod.PIX,
            },
        ).json()
        self.assertEqual(method_rows["count"], 1)
        self.assertEqual(
            method_rows["count"], detail["sales_by_payment_method"][0]["valid_sales"]
        )
        self.assertEqual(
            self.client.get(self.orders_url, {"metric": "refunds"}).json()["count"],
            1,
        )
        net_rows = self.client.get(
            self.orders_url, {**self.today_params(), "metric": "net_revenue"}
        ).json()
        unfiltered_detail = self.client.get(self.detail_url, self.today_params()).json()
        self.assertEqual(net_rows["count"], 3)
        self.assertEqual(
            sum(Decimal(row["revenue_value"]) for row in net_rows["results"]),
            Decimal(unfiltered_detail["financial"]["net_revenue"]),
        )

        legacy = self.client.get(self.orders_url, self.today_params()).json()
        self.assertEqual(legacy["metric"], "all")
        self.assertEqual(legacy["count"], 3)
        self.assertIn("revenue_value", legacy["results"][0])

    def test_grupo_sem_classificacao_retorna_somente_itens_sem_vinculo(self):
        order = self.create_order(total="15.00")
        self.add_item(order, None, price="5.00")

        detail = self.client.get(self.detail_url, self.today_params()).json()
        result = self.client.get(
            self.orders_url,
            {
                **self.today_params(),
                "metric": "product_revenue",
                "unclassified": "true",
            },
        ).json()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["metric_item_revenue"], "5.00")
        self.assertEqual(
            result["results"][0]["metric_item_revenue"],
            detail["product_rankings"]["unclassified"]["revenue"],
        )

    def test_schema_documenta_rotas_parametros_e_erros(self):
        response = self.client.get("/api/schema/?format=json")

        self.assertEqual(response.status_code, 200)
        paths = response.json()["paths"]
        detail = paths[self.detail_url]["get"]
        drill_down = paths[self.orders_url]["get"]
        self.assertEqual(
            {parameter["name"] for parameter in detail["parameters"]},
            {"period", "start_date", "end_date", "drop", "category"},
        )
        self.assertIn("metric", {p["name"] for p in drill_down["parameters"]})
        for operation in (detail, drill_down):
            for code in ("200", "400", "401", "403"):
                self.assertIn(code, operation["responses"])
