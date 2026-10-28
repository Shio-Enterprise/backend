from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from products.models import Product, ProductVariation

from .models import SiteEvent, SiteEventType

User = get_user_model()


class SiteEventCreateTests(APITestCase):
    url = "/api/analytics/events/"

    def setUp(self):
        self.user = User.objects.create_user(
            email="metricas@shio.com", name="Métricas", password="senha_forte_123"
        )
        self.product = Product.objects.create(
            name="Camiseta", description="x", base_price=100
        )
        self.variation = ProductVariation.objects.create(
            product=self.product, size="M", sku="MET-M", stock_quantity=5
        )

    def test_visitante_anonimo_registra_evento_com_identificador(self):
        response = self.client.post(
            self.url,
            {
                "event_type": SiteEventType.PAGE_VIEW,
                "anonymous_id": "visitante-abc",
                "path": "/produtos",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        event = SiteEvent.objects.get()
        self.assertIsNone(event.user)
        self.assertEqual(event.anonymous_id, "visitante-abc")
        self.assertEqual(event.path, "/produtos")

    def test_usuario_logado_tem_evento_associado_a_conta(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.post(
            self.url,
            {"event_type": SiteEventType.PAGE_VIEW, "path": "/"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(SiteEvent.objects.get().user, self.user)

    def test_evento_de_produto_registra_produto_e_variacao(self):
        response = self.client.post(
            self.url,
            {
                "event_type": SiteEventType.ADD_TO_CART,
                "anonymous_id": "visitante-abc",
                "product": str(self.product.id),
                "variation": str(self.variation.id),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        event = SiteEvent.objects.get()
        self.assertEqual(event.product, self.product)
        self.assertEqual(event.variation, self.variation)

    def test_visitante_sem_identificador_anonimo_recebe_400(self):
        response = self.client.post(
            self.url,
            {"event_type": SiteEventType.PAGE_VIEW, "path": "/"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("anonymous_id", response.json())
        self.assertFalse(SiteEvent.objects.exists())

    def test_tipo_de_evento_invalido_recebe_400(self):
        response = self.client.post(
            self.url,
            {"event_type": "TIPO_INEXISTENTE", "anonymous_id": "visitante-abc"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(SiteEvent.objects.exists())

    def test_produto_inexistente_recebe_400(self):
        response = self.client.post(
            self.url,
            {
                "event_type": SiteEventType.PRODUCT_VIEW,
                "anonymous_id": "visitante-abc",
                "product": "00000000-0000-0000-0000-000000000000",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(SiteEvent.objects.exists())

    def test_endpoint_nao_expoe_leitura(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
