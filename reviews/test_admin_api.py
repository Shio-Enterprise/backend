from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from .factories import make_order_item, make_product, make_user
from .models import RemovalReason, ReviewStatus
from .services import create_review, remove_review

LIST_URL = "/api/reviews/admin/"


def action_url(review, action):
    return f"/api/reviews/admin/{review.id}/{action}/"


class AdminReviewApiTests(APITestCase):
    def setUp(self):
        self.admin = make_user(name="Admin Shio", admin=True)
        self.customer = make_user(name="Maria Silva")
        self.product, (self.variation,) = make_product()
        make_order_item(self.customer, self.variation)
        self.review = create_review(self.customer, self.product, rating=1)
        self.other_product, (other_variation,) = make_product("Calça")
        joao = make_user(name="João")
        make_order_item(joao, other_variation)
        self.other_review = create_review(joao, self.other_product, rating=5)
        self.client.force_authenticate(self.admin)

    def test_cliente_nao_acessa_rotas_admin(self):
        self.client.force_authenticate(self.customer)
        calls = [
            ("get", LIST_URL, None),
            ("post", action_url(self.review, "remove"), {"reason": "SPAM"}),
            ("post", action_url(self.review, "restore"), None),
            ("put", action_url(self.review, "reply"), {"text": "Oi"}),
            ("delete", action_url(self.review, "reply"), None),
        ]
        for method, url, body in calls:
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(url, body, format="json")
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, ReviewStatus.PUBLISHED)

    def test_sem_login_401(self):
        self.client.force_authenticate(None)
        self.assertEqual(
            self.client.get(LIST_URL).status_code, status.HTTP_401_UNAUTHORIZED
        )

    def test_lista_tudo_com_dados_de_moderacao(self):
        remove_review(self.review, admin=self.admin, reason=RemovalReason.SPAM)

        response = self.client.get(LIST_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        removed = next(
            r for r in response.data["results"] if r["id"] == str(self.review.id)
        )
        self.assertEqual(removed["status"], ReviewStatus.REMOVED)
        self.assertEqual(removed["removed_by_name"], "Admin Shio")
        self.assertEqual(removed["author_email"], self.customer.email)

    def test_filtros(self):
        remove_review(self.review, admin=self.admin, reason=RemovalReason.SPAM)
        cases = [
            ({"status": "REMOVED"}, {str(self.review.id)}),
            ({"status": "PUBLISHED"}, {str(self.other_review.id)}),
            ({"rating": 5}, {str(self.other_review.id)}),
            ({"product": str(self.product.id)}, {str(self.review.id)}),
        ]
        for params, expected in cases:
            with self.subTest(params=params):
                response = self.client.get(LIST_URL, params)
                self.assertEqual({r["id"] for r in response.data["results"]}, expected)

    def test_filtro_invalido_400(self):
        response = self.client.get(LIST_URL, {"status": "PENDING"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_remover(self):
        response = self.client.post(
            action_url(self.review, "remove"), {"reason": "OFFENSIVE"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], ReviewStatus.REMOVED)
        self.assertEqual(response.data["removal_reason_label"], "Linguagem ofensiva")
        self.product.refresh_from_db()
        self.assertEqual(self.product.rating_count, 0)
        self.assertEqual(self.product.rating_avg, Decimal("0.00"))

    def test_remover_com_corpo_que_nao_e_objeto_400(self):
        response = self.client.post(
            action_url(self.review, "remove"), ["SPAM"], format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, ReviewStatus.PUBLISHED)

    def test_remover_outro_sem_texto_400(self):
        for body in ({"reason": "OTHER"}, {"reason": "OTHER", "note": "   "}):
            with self.subTest(body=body):
                response = self.client.post(
                    action_url(self.review, "remove"), body, format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_remover_motivo_invalido_400(self):
        response = self.client.post(
            action_url(self.review, "remove"), {"reason": "FEIO"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_remover_duas_vezes_400(self):
        url = action_url(self.review, "remove")
        self.client.post(url, {"reason": "SPAM"}, format="json")
        response = self.client.post(url, {"reason": "SPAM"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("message", response.data)

    def test_restaurar(self):
        remove_review(self.review, admin=self.admin, reason=RemovalReason.SPAM)

        response = self.client.post(action_url(self.review, "restore"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], ReviewStatus.PUBLISHED)
        self.product.refresh_from_db()
        self.assertEqual(self.product.rating_count, 1)

    def test_restaurar_publicada_400(self):
        response = self.client.post(action_url(self.review, "restore"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_responder_aparece_na_lista_publica(self):
        response = self.client.put(
            action_url(self.review, "reply"),
            {"text": "Sentimos muito, Maria!"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        public = self.client.get(f"/api/reviews/products/{self.product.id}/")
        self.assertEqual(
            public.data["results"][0]["admin_reply"], "Sentimos muito, Maria!"
        )

    def test_responder_vazio_ou_longo_400(self):
        for text in ("", "a" * 1001):
            with self.subTest(size=len(text)):
                response = self.client.put(
                    action_url(self.review, "reply"), {"text": text}, format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_responder_removida_400(self):
        remove_review(self.review, admin=self.admin, reason=RemovalReason.SPAM)
        response = self.client.put(
            action_url(self.review, "reply"), {"text": "Oi"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_apagar_resposta(self):
        url = action_url(self.review, "reply")
        self.client.put(url, {"text": "Oi"}, format="json")

        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["admin_reply"], "")

    def test_avaliacao_inexistente_404(self):
        url = "/api/reviews/admin/00000000-0000-0000-0000-000000000000/restore/"
        self.assertEqual(self.client.post(url).status_code, status.HTTP_404_NOT_FOUND)
