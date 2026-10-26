from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITestCase

from authentication.models import NewsletterSubscriber
from notifications.marketing import is_receiving
from notifications.test_marketing import make_order
from notifications.unsubscribe import make_unsubscribe_token

User = get_user_model()


def unsub_url(token):
    return reverse("authentication:marketing-unsubscribe", args=[token])


class UnsubscribeApiTests(APITestCase):
    def test_get_unsubscribe_mostra_email_mascarado(self):
        token = make_unsubscribe_token("arthur@gmail.com")
        resp = self.client.get(unsub_url(token))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"email": "ar***@gmail.com"})
        self.assertEqual(NewsletterSubscriber.objects.count(), 0)

    def test_post_unsubscribe_bloqueia_comprador_nao_inscrito(self):
        user = User.objects.create_user(
            email="ana@x.com", password="senha-forte-123", name="Ana"
        )
        make_order(user)
        self.assertTrue(is_receiving("ana@x.com"))
        resp = self.client.post(unsub_url(make_unsubscribe_token("ana@x.com")))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            {"message": "Pronto, você não vai mais receber promoções."},
        )
        sub = NewsletterSubscriber.objects.get(email="ana@x.com")
        self.assertIsNotNone(sub.unsubscribed_at)
        self.assertFalse(is_receiving("ana@x.com"))

    def test_token_adulterado_retorna_400_sem_alterar(self):
        token = make_unsubscribe_token("ana@x.com")
        ruim = token[:-1] + ("A" if token[-1] != "A" else "B")
        antes = NewsletterSubscriber.objects.count()
        for t in [ruim, token[:10], "lixo"]:
            for method in (self.client.get, self.client.post):
                resp = method(unsub_url(t))
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.json(), {"error": "Link inválido."})
        self.assertEqual(NewsletterSubscriber.objects.count(), antes)


class RegisterMarketingTests(APITestCase):
    def _register(self, email, **extra):
        return self.client.post(
            reverse("authentication:register"),
            {"email": email, "name": "Ana", "password": "senha-forte-123", **extra},
            format="json",
        )

    def test_register_com_aceite_inscreve(self):
        resp = self._register("Ana@X.com", marketing_opt_in=True)
        self.assertEqual(resp.status_code, 201, resp.content)
        sub = NewsletterSubscriber.objects.get(email="ana@x.com")
        self.assertTrue(sub.consent_lgpd)
        self.assertIsNone(sub.unsubscribed_at)

    def test_register_sem_aceite_nao_inscreve(self):
        resp = self._register("ana@x.com")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(NewsletterSubscriber.objects.count(), 0)


class MeMarketingTests(APITestCase):
    url = "/api/auth/me/marketing/"

    def setUp(self):
        self.user = User.objects.create_user(
            email="ana@x.com", password="senha-forte-123", name="Ana"
        )

    def test_me_marketing_reflete_receberia_hoje(self):
        make_order(self.user)
        self.client.force_authenticate(self.user)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"receives": True})

    def test_me_marketing_put_false_bloqueia_e_true_reativa(self):
        self.client.force_authenticate(self.user)
        resp = self.client.put(self.url, {"receives": False}, format="json")
        self.assertEqual(resp.json(), {"receives": False})
        self.assertFalse(is_receiving("ana@x.com"))
        resp = self.client.put(self.url, {"receives": True}, format="json")
        self.assertEqual(resp.json(), {"receives": True})
        self.assertTrue(is_receiving("ana@x.com"))
        self.assertIsNone(
            NewsletterSubscriber.objects.get(email="ana@x.com").unsubscribed_at
        )

    def test_me_marketing_exige_login(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.assertEqual(
            self.client.put(self.url, {"receives": True}, format="json").status_code,
            401,
        )
