from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from .models import SiteEvent, SiteEventType

User = get_user_model()

LINK_URL = "/api/analytics/link/"
OVERVIEW_URL = "/api/analytics/overview/"


class LinkAnonymousEventsTests(APITestCase):
    def setUp(self):
        self.cliente = User.objects.create_user(
            email="vinculo@shio.com", name="Vínculo", password="senha_forte_123"
        )

    def _anonimo(self, anonymous_id, path="/produtos"):
        return SiteEvent.objects.create(
            event_type=SiteEventType.PAGE_VIEW,
            anonymous_id=anonymous_id,
            path=path,
            occurred_at=timezone.now() - timedelta(hours=1),
        )

    def test_login_vincula_eventos_anteriores_do_mesmo_identificador(self):
        antes = self._anonimo("navegador-1")
        self._anonimo("navegador-1", path="/cart")
        outro = self._anonimo("navegador-2")
        self.client.force_authenticate(user=self.cliente)

        response = self.client.post(
            LINK_URL, {"anonymous_id": "navegador-1"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {"linked": 2})
        antes.refresh_from_db()
        self.assertEqual(antes.user, self.cliente)
        self.assertIsNone(SiteEvent.objects.get(pk=outro.pk).user)

    def test_eventos_ja_vinculados_nao_mudam_de_conta(self):
        outro_usuario = User.objects.create_user(
            email="outro-vinculo@shio.com", name="Outro", password="senha_forte_123"
        )
        SiteEvent.objects.create(
            event_type=SiteEventType.PAGE_VIEW,
            anonymous_id="navegador-3",
            user=outro_usuario,
            path="/",
        )
        self.client.force_authenticate(user=self.cliente)

        response = self.client.post(
            LINK_URL, {"anonymous_id": "navegador-3"}, format="json"
        )

        self.assertEqual(response.json(), {"linked": 0})
        self.assertEqual(SiteEvent.objects.get().user, outro_usuario)

    def test_vinculo_exige_login(self):
        response = self.client.post(
            LINK_URL, {"anonymous_id": "navegador-1"}, format="json"
        )

        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_vinculo_sem_identificador_recebe_400(self):
        self.client.force_authenticate(user=self.cliente)

        response = self.client.post(LINK_URL, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class AdminPagesOnlyInUserTimelineTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="admin-regra@shio.com",
            name="Admin",
            password="senha_forte_123",
            is_staff=True,
        )

    def _evento(self, path, **kwargs):
        return SiteEvent.objects.create(
            event_type=SiteEventType.PAGE_VIEW,
            path=path,
            occurred_at=timezone.now() - timedelta(days=1),
            **kwargs,
        )

    def test_paginas_do_admin_nao_entram_nas_metricas_gerais(self):
        self._evento("/admin/dashboard", user=self.admin)
        self._evento("/admin/customers", user=self.admin)
        self._evento("/", anonymous_id="visitante-1")
        self.client.force_authenticate(user=self.admin)

        data = self.client.get(OVERVIEW_URL).json()

        self.assertEqual(data["events_by_type"]["PAGE_VIEW"], 1)
        self.assertEqual(data["visitors"], {"registered": 0, "anonymous": 1})

    def test_paginas_do_admin_aparecem_na_linha_do_tempo_do_usuario(self):
        self._evento("/admin/dashboard", user=self.admin)
        self.client.force_authenticate(user=self.admin)

        results = self.client.get(
            f"/api/analytics/users/{self.admin.pk}/events/"
        ).json()["results"]

        self.assertEqual([item["path"] for item in results], ["/admin/dashboard"])

    def test_rota_que_apenas_comeca_com_admin_nao_e_excluida(self):
        self._evento("/administracao-de-estoque", anonymous_id="visitante-2")
        self.client.force_authenticate(user=self.admin)

        data = self.client.get(OVERVIEW_URL).json()

        self.assertEqual(data["events_by_type"]["PAGE_VIEW"], 1)
