from calendar import monthrange
from datetime import datetime, time, timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from orders.models import CustomerOrder, OrderItem, Payment, PaymentStatus
from products.models import Product, ProductVariation

from .models import SiteEvent, SiteEventType
from .periods import BUSINESS_TIMEZONE

User = get_user_model()


class SiteEventMetricsTests(APITestCase):
    overview_url = "/api/analytics/overview/"

    def setUp(self):
        self.admin = User.objects.create_user(
            email="admin-metricas@shio.com",
            name="Admin",
            password="senha_forte_123",
            is_staff=True,
        )
        self.cliente = User.objects.create_user(
            email="cliente-metricas@shio.com",
            name="Cliente",
            password="senha_forte_123",
        )
        self.product = Product.objects.create(
            name="Boné", description="x", base_price=50
        )

    def _event(self, days_ago, event_type=SiteEventType.PAGE_VIEW, **kwargs):
        occurred_at = kwargs.pop("occurred_at", timezone.now() - timedelta(days=days_ago))
        return SiteEvent.objects.create(
            event_type=event_type,
            occurred_at=occurred_at,
            **kwargs,
        )

    def _paid_item(self, user, product, *, paid_at=None, status=PaymentStatus.PAID):
        variation = ProductVariation.objects.create(
            product=product,
            size=uuid4().hex[:12],
            color="",
            sku=f"sku-{uuid4().hex}",
        )
        order = CustomerOrder.objects.create(
            user=user,
            subtotal=50,
            total_amount=50,
            status="PAID",
            shipping_zip_code="70000-000",
            shipping_street="Rua",
            shipping_number="1",
            shipping_neighborhood="Asa",
            shipping_city="Brasilia",
            shipping_state="DF",
        )
        OrderItem.objects.create(
            order=order,
            variation=variation,
            quantity=1,
            unit_price=50,
            product_name=product.name,
        )
        Payment.objects.create(
            order=order,
            status=status,
            total_amount=50,
            paid_at=paid_at or timezone.now() - timedelta(days=1),
        )
        return variation

    def test_overview_exige_administrador(self):
        self.client.force_authenticate(user=self.cliente)

        response = self.client.get(self.overview_url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_overview_exige_autenticacao(self):
        response = self.client.get(self.overview_url)

        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_periodo_invalido_recebe_400(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(self.overview_url, {"period": "semanal"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_mensal_agrupa_por_dia_em_30_pontos_e_ignora_eventos_antigos(self):
        self._event(days_ago=2)
        self._event(
            days_ago=2, event_type=SiteEventType.PRODUCT_VIEW, anonymous_id="v1"
        )
        self._event(days_ago=40)
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(self.overview_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["group_by"], "day")
        self.assertEqual(len(data["series"]), 30)
        self.assertEqual(sum(point["total"] for point in data["series"]), 2)
        self.assertEqual(data["events_by_type"][SiteEventType.PAGE_VIEW], 1)
        self.assertEqual(data["events_by_type"][SiteEventType.PRODUCT_VIEW], 1)

    def test_anual_agrupa_por_mes_e_cobre_365_dias(self):
        self._event(days_ago=200)
        self._event(days_ago=400)
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(self.overview_url, {"period": "anual"})

        data = response.json()
        self.assertEqual(data["group_by"], "month")
        self.assertEqual(sum(point["total"] for point in data["series"]), 1)
        self.assertTrue(all(point["date"].endswith("-01") for point in data["series"]))

    def test_visitantes_contam_cadastrados_e_anonimos_sem_duplicar(self):
        self._event(days_ago=1, user=self.cliente)
        self._event(days_ago=1, user=self.cliente)
        self._event(days_ago=1, anonymous_id="visitante-1")
        self._event(days_ago=1, anonymous_id="visitante-1")
        self._event(days_ago=1, anonymous_id="visitante-2")
        self.client.force_authenticate(user=self.admin)

        data = self.client.get(self.overview_url).json()

        self.assertEqual(data["visitors"], {"registered": 1, "anonymous": 2})

    def test_produtos_mais_visualizados_ordenados_por_visualizacoes(self):
        outro = Product.objects.create(name="Camiseta", description="x", base_price=80)
        self._event(
            days_ago=1,
            event_type=SiteEventType.PRODUCT_VIEW,
            product=outro,
            anonymous_id="a",
        )
        self._event(
            days_ago=1,
            event_type=SiteEventType.PRODUCT_VIEW,
            product=self.product,
            anonymous_id="a",
        )
        self._event(
            days_ago=1,
            event_type=SiteEventType.PRODUCT_VIEW,
            product=self.product,
            anonymous_id="b",
        )
        self.client.force_authenticate(user=self.admin)

        data = self.client.get(self.overview_url).json()

        self.assertEqual(data["top_products"][0]["name"], "Boné")
        self.assertEqual(data["top_products"][0]["views"], 2)
        self.assertEqual(data["top_products"][1]["name"], "Camiseta")

    def test_interesse_em_comum_agrupa_contas_pelo_sinal_mais_forte(self):
        outro = User.objects.create_user(
            email="outra-conta@shio.com",
            name="Outra Conta",
            password="senha_forte_123",
        )
        self._event(
            days_ago=1,
            event_type=SiteEventType.PRODUCT_VIEW,
            product=self.product,
            user=self.cliente,
        )
        self._paid_item(self.cliente, self.product)
        self._event(
            days_ago=1,
            event_type=SiteEventType.ADD_TO_CART,
            product=self.product,
            user=outro,
        )
        self._event(
            days_ago=1,
            event_type=SiteEventType.PRODUCT_VIEW,
            product=self.product,
            anonymous_id="sem-conta",
        )
        sozinho = Product.objects.create(name="Camiseta", description="x", base_price=80)
        self._event(
            days_ago=1,
            event_type=SiteEventType.PRODUCT_VIEW,
            product=sozinho,
            user=self.cliente,
        )
        self.client.force_authenticate(user=self.admin)

        response = self.client.get("/api/analytics/products/interest/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["name"] for item in response.json()["products"]],
            ["Boné"],
        )
        product = response.json()["products"][0]
        self.assertEqual(product["name"], "Boné")
        self.assertEqual(product["accounts"], 2)
        self.assertEqual(product["anonymous"], 1)
        by_email = {person["email"]: person["interest"] for person in product["users"]}
        self.assertEqual(by_email["cliente-metricas@shio.com"], "PURCHASE")
        self.assertEqual(by_email["outra-conta@shio.com"], "ADD_TO_CART")
        self.assertNotIn("name", product["users"][0])

    def test_busca_de_produto_filtra_pelo_nome(self):
        Product.objects.create(name="Camiseta Preta", description="x", base_price=80)
        self.client.force_authenticate(user=self.admin)

        response = self.client.get("/api/analytics/products/search/", {"q": "preta"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["name"] for item in response.json()],
            ["Camiseta Preta"],
        )

    def test_acessos_do_produto_trazem_acao_horario_conta_e_variacao(self):
        variation = ProductVariation.objects.create(
            product=self.product, size="M", color="Preto", sku="bone-m-preto"
        )
        self._event(
            days_ago=1,
            event_type=SiteEventType.PRODUCT_VIEW,
            product=self.product,
            variation=variation,
            user=self.cliente,
            path="/produto/bone",
        )
        self._event(
            days_ago=1,
            event_type=SiteEventType.ADD_TO_CART,
            product=self.product,
            anonymous_id="sem-conta",
            path="/produto/bone",
        )
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(f"/api/analytics/products/{self.product.pk}/accesses/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["name"], "Boné")
        self.assertEqual(data["totals"]["PRODUCT_VIEW"], 1)
        self.assertEqual(data["totals"]["ADD_TO_CART"], 1)
        self.assertEqual(data["count"], 2)
        by_type = {item["event_type"]: item for item in data["accesses"]}
        self.assertEqual(by_type["PRODUCT_VIEW"]["email"], "cliente-metricas@shio.com")
        self.assertEqual(by_type["PRODUCT_VIEW"]["variation"], "M / Preto")
        self.assertEqual(by_type["PRODUCT_VIEW"]["path"], "/produto/bone")
        self.assertIsNone(by_type["ADD_TO_CART"]["email"])
        self.assertNotIn("name", by_type["PRODUCT_VIEW"])

    def test_produtos_da_conta_contam_cada_acao(self):
        outro = Product.objects.create(name="Camiseta", description="x", base_price=80)
        self._event(
            days_ago=1,
            event_type=SiteEventType.PRODUCT_VIEW,
            product=self.product,
            user=self.cliente,
        )
        self._paid_item(self.cliente, self.product)
        self._event(
            days_ago=2,
            event_type=SiteEventType.PRODUCT_VIEW,
            product=outro,
            user=self.admin,
        )
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(f"/api/analytics/users/{self.cliente.pk}/products/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        products = response.json()["products"]
        self.assertEqual([item["name"] for item in products], ["Boné"])
        self.assertEqual(products[0]["views"], 1)
        self.assertEqual(products[0]["purchases"], 1)
        self.assertEqual(products[0]["added_to_cart"], 0)

    def test_timeline_do_usuario_mostra_apenas_eventos_dele_no_periodo(self):
        self._event(days_ago=1, user=self.cliente)
        self._event(days_ago=3, user=self.cliente)
        self._event(days_ago=40, user=self.cliente)
        self._event(days_ago=1, anonymous_id="outro")
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(f"/api/analytics/users/{self.cliente.pk}/events/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json()["results"]
        self.assertEqual(len(results), 2)
        self.assertGreater(results[0]["occurred_at"], results[1]["occurred_at"])

    def test_timeline_informa_nome_do_produto_quando_houver(self):
        self._event(
            days_ago=1,
            event_type=SiteEventType.PRODUCT_VIEW,
            product=self.product,
            user=self.cliente,
        )
        self._event(days_ago=1, event_type=SiteEventType.PAGE_VIEW, user=self.cliente)
        self.client.force_authenticate(user=self.admin)

        results = self.client.get(
            f"/api/analytics/users/{self.cliente.pk}/events/"
        ).json()["results"]

        por_tipo = {item["event_type"]: item for item in results}
        self.assertEqual(por_tipo["PRODUCT_VIEW"]["product_name"], "Boné")
        self.assertIsNone(por_tipo["PAGE_VIEW"]["product_name"])

    def test_timeline_exige_administrador(self):
        self.client.force_authenticate(user=self.cliente)

        response = self.client.get(f"/api/analytics/users/{self.cliente.pk}/events/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_timeline_de_usuario_inexistente_recebe_404(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get("/api/analytics/users/999999/events/")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_mes_escolhido_cobre_o_calendario_e_ignora_o_mes_seguinte(self):
        today = timezone.now().astimezone(BUSINESS_TIMEZONE).date()
        previous = today.replace(day=1) - timedelta(days=1)
        inside = datetime.combine(
            previous.replace(day=1), time(15, 0), tzinfo=BUSINESS_TIMEZONE
        )
        outside = datetime.combine(
            today.replace(day=1), time(15, 0), tzinfo=BUSINESS_TIMEZONE
        )
        self._event(0, occurred_at=inside)
        self._event(0, occurred_at=outside)
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(
            self.overview_url,
            {"period": "mensal", "year": previous.year, "month": previous.month},
        )

        data = response.json()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(data["group_by"], "day")
        self.assertEqual(len(data["series"]), monthrange(previous.year, previous.month)[1])
        self.assertEqual(data["series"][0]["date"], previous.replace(day=1).isoformat())
        self.assertEqual(sum(point["total"] for point in data["series"]), 1)

    def test_ano_escolhido_cobre_os_doze_meses(self):
        today = timezone.now().astimezone(BUSINESS_TIMEZONE).date()
        year = today.year - 1
        inside = datetime(year, 3, 15, 12, 0, tzinfo=BUSINESS_TIMEZONE)
        outside = datetime(today.year, 1, 10, 12, 0, tzinfo=BUSINESS_TIMEZONE)
        self._event(0, occurred_at=inside)
        self._event(0, occurred_at=outside)
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(self.overview_url, {"period": "anual", "year": year})

        data = response.json()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(data["group_by"], "month")
        self.assertEqual(len(data["series"]), 12)
        self.assertEqual(data["series"][0]["date"], f"{year}-01-01")
        self.assertEqual(sum(point["total"] for point in data["series"]), 1)

    def test_mes_futuro_recebe_400(self):
        today = timezone.now().astimezone(BUSINESS_TIMEZONE).date()
        if today.month == 12:
            params = {"period": "mensal", "year": today.year + 1, "month": 1}
        else:
            params = {"period": "mensal", "year": today.year, "month": today.month + 1}
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(self.overview_url, params)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_compra_confirmada_entra_na_navegacao_sem_evento(self):
        self._paid_item(self.cliente, self.product)
        self._paid_item(self.cliente, self.product, status=PaymentStatus.PENDING)
        self._event(
            days_ago=1,
            event_type=SiteEventType.PURCHASE,
            product=self.product,
            user=self.cliente,
        )
        self.client.force_authenticate(user=self.admin)

        overview = self.client.get(self.overview_url).json()
        self.assertEqual(overview["events_by_type"]["PURCHASE"], 1)
        self.assertEqual(sum(point["total"] for point in overview["series"]), 1)

        timeline = self.client.get(
            f"/api/analytics/users/{self.cliente.pk}/events/"
        ).json()
        purchases = [
            item for item in timeline["results"] if item["event_type"] == "PURCHASE"
        ]
        self.assertEqual(len(purchases), 1)
        self.assertEqual(purchases[0]["product_name"], "Boné")

        products = self.client.get(
            f"/api/analytics/users/{self.cliente.pk}/products/"
        ).json()["products"]
        self.assertEqual(products[0]["purchases"], 1)
        self.assertEqual(products[0]["views"], 0)
