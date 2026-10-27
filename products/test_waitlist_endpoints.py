"""Testes dos endpoints de lista de espera dos drops."""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from products.models import DropCampaign, DropWaitlist
from products.tests import auth_header, make_user


def make_drop(slug="drop-teste", launch_date=None, **kwargs):
    """Cria um drop com launch_date no futuro por padrão (antes de abrir)."""
    defaults = {
        "name": "Drop Teste",
        "is_public": True,
        "is_active": True,
        "launch_date": launch_date if launch_date is not None else (timezone.now() + timedelta(days=7)),
    }
    defaults.update(kwargs)
    return DropCampaign.objects.create(slug=slug, **defaults)


def make_open_drop(slug="drop-aberto", **kwargs):
    """Drop já aberto para venda."""
    defaults = {
        "launch_date": timezone.now() - timedelta(hours=1),
    }
    defaults.update(kwargs)
    return make_drop(slug=slug, **defaults)


class DropWaitlistJoinTests(APITestCase):
    """POST /api/catalog/drops/<id>/waitlist/"""

    def setUp(self):
        self.user = make_user("u@x.com")
        self.other = make_user("o@x.com")
        self.drop = make_drop()
        self.url = reverse("products:drop-waitlist-join", args=[self.drop.pk])

    def test_401_sem_autenticacao(self):
        resp = self.client.post(self.url)
        self.assertEqual(resp.status_code, 401)

    def test_entrar_na_lista_cria_entrada(self):
        resp = self.client.post(self.url, **auth_header(self.user))
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(
            DropWaitlist.objects.filter(user=self.user, drop=self.drop).exists()
        )

    def test_entrar_duas_vezes_nao_duplica(self):
        self.client.post(self.url, **auth_header(self.user))
        resp = self.client.post(self.url, **auth_header(self.user))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            DropWaitlist.objects.filter(user=self.user, drop=self.drop).count(), 1
        )

    def test_recusa_drop_ja_aberto(self):
        drop = make_open_drop(slug="aberto-1")
        url = reverse("products:drop-waitlist-join", args=[drop.pk])
        resp = self.client.post(url, **auth_header(self.user))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("já abriu", resp.data["detail"])

    def test_recusa_drop_encerrado(self):
        drop = make_drop(
            slug="encerrado-1",
            launch_date=timezone.now() - timedelta(days=2),
            end_date=timezone.now() - timedelta(hours=1),
        )
        url = reverse("products:drop-waitlist-join", args=[drop.pk])
        resp = self.client.post(url, **auth_header(self.user))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("encerrado", resp.data["detail"])

    def test_recusa_drop_privado(self):
        drop = make_drop(slug="privado-1", is_public=False)
        url = reverse("products:drop-waitlist-join", args=[drop.pk])
        resp = self.client.post(url, **auth_header(self.user))
        self.assertEqual(resp.status_code, 400)

    def test_recusa_drop_inativo(self):
        drop = make_drop(slug="inativo-1", is_active=False)
        url = reverse("products:drop-waitlist-join", args=[drop.pk])
        resp = self.client.post(url, **auth_header(self.user))
        self.assertEqual(resp.status_code, 400)

    def test_drop_inexistente_retorna_404(self):
        import uuid

        url = reverse("products:drop-waitlist-join", args=[uuid.uuid4()])
        resp = self.client.post(url, **auth_header(self.user))
        self.assertEqual(resp.status_code, 404)

    def test_usuarios_diferentes_podem_entrar_no_mesmo_drop(self):
        self.client.post(self.url, **auth_header(self.user))
        self.client.post(self.url, **auth_header(self.other))
        self.assertEqual(DropWaitlist.objects.filter(drop=self.drop).count(), 2)


class DropWaitlistLeaveTests(APITestCase):
    """DELETE /api/catalog/drops/<id>/waitlist/delete/"""

    def setUp(self):
        self.user = make_user("u2@x.com")
        self.other = make_user("o2@x.com")
        self.drop = make_drop(slug="drop-sair")
        self.entry = DropWaitlist.objects.create(user=self.user, drop=self.drop)
        self.url = reverse("products:drop-waitlist-leave", args=[self.drop.pk])

    def test_401_sem_autenticacao(self):
        resp = self.client.delete(self.url)
        self.assertEqual(resp.status_code, 401)

    def test_sair_da_lista(self):
        resp = self.client.delete(self.url, **auth_header(self.user))
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(DropWaitlist.objects.filter(pk=self.entry.pk).exists())

    def test_usuario_nao_mexe_na_entrada_de_outro(self):
        resp = self.client.delete(self.url, **auth_header(self.other))
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(DropWaitlist.objects.filter(pk=self.entry.pk).exists())

    def test_404_se_nao_estiver_na_lista(self):
        self.entry.delete()
        resp = self.client.delete(self.url, **auth_header(self.user))
        self.assertEqual(resp.status_code, 404)


class DropWaitlistMyEntriesTests(APITestCase):
    """GET /api/catalog/drop-waitlist/"""

    def setUp(self):
        self.user = make_user("u3@x.com")
        self.other = make_user("o3@x.com")
        self.drop1 = make_drop(slug="drop-lista-1")
        self.drop2 = make_drop(slug="drop-lista-2")
        DropWaitlist.objects.create(user=self.user, drop=self.drop1)
        DropWaitlist.objects.create(user=self.other, drop=self.drop2)
        self.url = reverse("products:drop-waitlist-list")

    def test_401_sem_autenticacao(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 401)

    def test_lista_apenas_entradas_do_proprio_usuario(self):
        resp = self.client.get(self.url, **auth_header(self.user))
        self.assertEqual(resp.status_code, 200)
        ids = [e["drop"]["id"] for e in resp.data["results"]]
        self.assertIn(str(self.drop1.pk), ids)
        self.assertNotIn(str(self.drop2.pk), ids)


class DropWaitlistUnseenTests(APITestCase):
    """GET /api/catalog/drop-waitlist/unseen/"""

    def setUp(self):
        self.user = make_user("u4@x.com")
        self.open_drop = make_open_drop(slug="aberto-unseen")
        self.future_drop = make_drop(slug="futuro-unseen")
        self.url = reverse("products:drop-waitlist-unseen")

    def test_401_sem_autenticacao(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 401)

    def test_modal_lista_apenas_drops_abertos_e_nao_vistos(self):
        # Drop aberto, não visto — deve aparecer
        DropWaitlist.objects.create(user=self.user, drop=self.open_drop)
        # Drop ainda fechado — não deve aparecer
        DropWaitlist.objects.create(user=self.user, drop=self.future_drop)

        resp = self.client.get(self.url, **auth_header(self.user))
        self.assertEqual(resp.status_code, 200)
        ids = [e["drop"]["id"] for e in resp.data["results"]]
        self.assertIn(str(self.open_drop.pk), ids)
        self.assertNotIn(str(self.future_drop.pk), ids)

    def test_drop_ja_visto_nao_aparece_no_modal(self):
        DropWaitlist.objects.create(
            user=self.user,
            drop=self.open_drop,
            seen_at=timezone.now(),
        )
        resp = self.client.get(self.url, **auth_header(self.user))
        ids = [e["drop"]["id"] for e in resp.data["results"]]
        self.assertNotIn(str(self.open_drop.pk), ids)

    def test_drop_encerrado_nao_aparece_no_modal(self):
        drop = make_drop(
            slug="encerrado-unseen",
            launch_date=timezone.now() - timedelta(days=2),
            end_date=timezone.now() - timedelta(hours=1),
        )
        DropWaitlist.objects.create(user=self.user, drop=drop)
        resp = self.client.get(self.url, **auth_header(self.user))
        ids = [e["drop"]["id"] for e in resp.data["results"]]
        self.assertNotIn(str(drop.pk), ids)

    def test_usuario_nao_ve_entradas_de_outro(self):
        other = make_user("outro-unseen@x.com")
        DropWaitlist.objects.create(user=other, drop=self.open_drop)
        resp = self.client.get(self.url, **auth_header(self.user))
        self.assertEqual(resp.data["results"], [])


class DropWaitlistMarkSeenTests(APITestCase):
    """POST /api/catalog/drop-waitlist/<id>/seen/"""

    def setUp(self):
        self.user = make_user("u5@x.com")
        self.other = make_user("o5@x.com")
        self.open_drop = make_open_drop(slug="aberto-seen")
        self.entry = DropWaitlist.objects.create(user=self.user, drop=self.open_drop)

    def url(self, pk):
        return reverse("products:drop-waitlist-mark-seen", args=[pk])

    def test_401_sem_autenticacao(self):
        resp = self.client.post(self.url(self.entry.pk))
        self.assertEqual(resp.status_code, 401)

    def test_marcar_como_visto_preenche_seen_at(self):
        resp = self.client.post(self.url(self.entry.pk), **auth_header(self.user))
        self.assertEqual(resp.status_code, 200)
        self.entry.refresh_from_db()
        self.assertIsNotNone(self.entry.seen_at)

    def test_marcar_como_visto_tira_do_unseen(self):
        unseen_url = reverse("products:drop-waitlist-unseen")
        # Antes: aparece
        resp = self.client.get(unseen_url, **auth_header(self.user))
        self.assertEqual(len(resp.data["results"]), 1)
        # Marca como visto
        self.client.post(self.url(self.entry.pk), **auth_header(self.user))
        # Depois: não aparece
        resp = self.client.get(unseen_url, **auth_header(self.user))
        self.assertEqual(len(resp.data["results"]), 0)

    def test_usuario_nao_marca_entrada_de_outro(self):
        resp = self.client.post(self.url(self.entry.pk), **auth_header(self.other))
        self.assertEqual(resp.status_code, 404)

    def test_marcar_ja_visto_e_idempotente(self):
        self.client.post(self.url(self.entry.pk), **auth_header(self.user))
        self.entry.refresh_from_db()
        first_seen = self.entry.seen_at
        self.client.post(self.url(self.entry.pk), **auth_header(self.user))
        self.entry.refresh_from_db()
        # seen_at não muda na segunda chamada
        self.assertEqual(self.entry.seen_at, first_seen)
