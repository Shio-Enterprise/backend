from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from products.models import Product

from .models import SiteEvent, SiteEventType

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
        return SiteEvent.objects.create(
            event_type=event_type,
            occurred_at=timezone.now() - timedelta(days=days_ago),
            **kwargs,
        )

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

    def test_timeline_exige_administrador(self):
        self.client.force_authenticate(user=self.cliente)

        response = self.client.get(f"/api/analytics/users/{self.cliente.pk}/events/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_timeline_de_usuario_inexistente_recebe_404(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get("/api/analytics/users/999999/events/")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
