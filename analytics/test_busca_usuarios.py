from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

User = get_user_model()

SEARCH_URL = "/api/analytics/users/search/"


class UserSearchTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="admin-busca@shio.com",
            name="Admin",
            password="senha_forte_123",
            is_staff=True,
        )
        self.maria = User.objects.create_user(
            email="maria.silva@gmail.com",
            name="Maria Silva",
            password="senha_forte_123",
        )
        self.joao = User.objects.create_user(
            email="joao@hotmail.com", name="João Souza", password="senha_forte_123"
        )
        self.client.force_authenticate(user=self.admin)

    def test_busca_por_nome_parcial_sem_diferenciar_maiusculas(self):
        response = self.client.get(SEARCH_URL, {"q": "silva"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.json(),
            [
                {
                    "id": self.maria.pk,
                    "name": "Maria Silva",
                    "email": "maria.silva@gmail.com",
                }
            ],
        )

    def test_busca_por_email_parcial(self):
        response = self.client.get(SEARCH_URL, {"q": "hotmail"})

        self.assertEqual([item["id"] for item in response.json()], [self.joao.pk])

    def test_texto_sem_correspondencia_retorna_lista_vazia(self):
        response = self.client.get(SEARCH_URL, {"q": "ninguem"})

        self.assertEqual(response.json(), [])

    def test_texto_curto_recebe_400(self):
        response = self.client.get(SEARCH_URL, {"q": "a"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_sem_texto_recebe_400(self):
        response = self.client.get(SEARCH_URL)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_resultados_limitados_a_dez(self):
        for i in range(12):
            User.objects.create_user(
                email=f"cliente{i}@shio.com",
                name=f"Cliente {i}",
                password="senha_forte_123",
            )

        response = self.client.get(SEARCH_URL, {"q": "cliente"})

        self.assertEqual(len(response.json()), 10)

    def test_nao_admin_nao_pode_buscar(self):
        self.client.force_authenticate(user=self.maria)

        response = self.client.get(SEARCH_URL, {"q": "maria"})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
