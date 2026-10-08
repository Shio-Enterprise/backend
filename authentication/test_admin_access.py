"""Regressões de autorização para os recursos do painel administrativo."""

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from authentication.models import UserProfile, UserRole

User = get_user_model()


class AdminEndpointAccessTests(APITestCase):
    """Garante que dados administrativos não vazem para clientes ou anônimos."""

    admin_read_endpoints = (
        "/api/catalog/inventory/",
        "/api/orders/dashboard/summary/",
        "/api/orders/dashboard/orders/",
        "/api/orders/dashboard/drop-revenue/",
        "/api/orders/admin/",
        "/api/auth/crm/customers/",
        "/api/reviews/admin/",
    )

    def setUp(self):
        self.admin = User.objects.create_user(
            email="admin-access@example.com",
            name="Admin Access",
        )
        UserProfile.objects.create(user=self.admin, role=UserRole.ADMIN)

        self.customer = User.objects.create_user(
            email="customer-access@example.com",
            name="Customer Access",
        )
        UserProfile.objects.create(user=self.customer, role=UserRole.CUSTOMER)

    def test_rotas_administrativas_exigem_autenticacao(self):
        for url in self.admin_read_endpoints:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_cliente_nao_acessa_rotas_administrativas(self):
        self.client.force_authenticate(user=self.customer)

        for url in self.admin_read_endpoints:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_acessa_rotas_administrativas(self):
        self.client.force_authenticate(user=self.admin)

        for url in self.admin_read_endpoints:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_escrita_de_catalogo_exige_permissao_administrativa(self):
        url = "/api/catalog/categories/"

        response = self.client.post(url, {"name": "Protegida"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        self.client.force_authenticate(user=self.customer)
        response = self.client.post(url, {"name": "Protegida"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(user=self.admin)
        response = self.client.post(url, {"name": "Protegida"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
