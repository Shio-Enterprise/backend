from decimal import Decimal

from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APITestCase

from orders.models import OrderStatus, PaymentStatus
from products.models import Product

from .factories import make_order_item, make_product, make_user
from .models import ProductReview, RemovalReason, ReviewStatus
from .services import create_review, remove_review


def product_url(product, suffix=""):
    return f"/api/reviews/products/{product.id}/{suffix}"


def review_url(review):
    return f"/api/reviews/{review.id}/"


class PublicReviewApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.product, (self.variation,) = make_product()
        self.maria = make_user(name="Maria Silva")
        make_order_item(self.maria, self.variation)
        self.review = create_review(
            self.maria, self.product, rating=4, comment="Bom", fit="SMALL"
        )

    def test_lista_publica_sem_login(self):
        response = self.client.get(product_url(self.product))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        item = response.data["results"][0]
        self.assertEqual(item["author_name"], "Maria Silva")
        self.assertEqual(item["purchased_size"], "M")
        self.assertTrue(item["verified_purchase"])
        self.assertEqual(item["rating"], 4)
        self.assertEqual(item["fit"], "SMALL")
        for hidden in ("status", "removal_note", "removed_by", "removal_reason"):
            self.assertNotIn(hidden, item)

    def test_lista_publica_esconde_removidas(self):
        remove_review(
            self.review, admin=make_user(admin=True), reason=RemovalReason.SPAM
        )
        response = self.client.get(product_url(self.product))
        self.assertEqual(response.data["count"], 0)

    def test_filtro_por_nota(self):
        joao = make_user(name="João")
        make_order_item(joao, self.variation)
        create_review(joao, self.product, rating=2)

        response = self.client.get(product_url(self.product), {"rating": 2})

        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["rating"], 2)

    def test_filtro_por_nota_invalida(self):
        response = self.client.get(product_url(self.product), {"rating": 9})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_produto_inexistente_ou_inativo_404(self):
        inactive, _ = make_product("Antigo", is_active=False)
        for url in (
            "/api/reviews/products/00000000-0000-0000-0000-000000000000/",
            product_url(inactive),
            product_url(inactive, "summary/"),
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    self.client.get(url).status_code, status.HTTP_404_NOT_FOUND
                )

    def test_resumo(self):
        response = self.client.get(product_url(self.product, "summary/"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["rating_avg"], "4.00")
        self.assertEqual(response.data["rating_count"], 1)
        self.assertEqual(
            response.data["distribution"], {"1": 0, "2": 0, "3": 0, "4": 1, "5": 0}
        )
        self.assertEqual(
            response.data["fit"], {"SMALL": 1, "TRUE_TO_SIZE": 0, "LARGE": 0}
        )


class CreateReviewApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.product, (self.variation,) = make_product()
        self.user = make_user()
        self.url = product_url(self.product)

    def test_sem_login_401(self):
        response = self.client.post(self.url, {"rating": 5}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_sem_pedido_entregue_403(self):
        make_order_item(self.user, self.variation, order_status=OrderStatus.SHIPPED)
        self.client.force_authenticate(self.user)

        response = self.client.post(self.url, {"rating": 5}, format="json")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("message", response.data)
        self.assertFalse(ProductReview.objects.exists())

    def test_pedido_entregue_cria_201(self):
        make_order_item(self.user, self.variation)
        self.client.force_authenticate(self.user)

        response = self.client.post(
            self.url,
            {"rating": 5, "comment": "Perfeita", "fit": "TRUE_TO_SIZE"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["rating"], 5)
        self.assertEqual(response.data["status"], ReviewStatus.PUBLISHED)
        self.product.refresh_from_db()
        self.assertEqual(self.product.rating_count, 1)
        self.assertEqual(self.product.rating_avg, Decimal("5.00"))

    def test_pedido_entregue_e_reembolsado_cria_201(self):
        make_order_item(
            self.user, self.variation, payment_status=PaymentStatus.REFUNDED
        )
        self.client.force_authenticate(self.user)
        response = self.client.post(self.url, {"rating": 2}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_duplicada_409(self):
        make_order_item(self.user, self.variation)
        self.client.force_authenticate(self.user)
        self.client.post(self.url, {"rating": 5}, format="json")

        response = self.client.post(self.url, {"rating": 1}, format="json")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(ProductReview.objects.count(), 1)

    def test_nota_invalida_400(self):
        make_order_item(self.user, self.variation)
        self.client.force_authenticate(self.user)
        for rating in (0, 6, 4.5, "abc", None):
            with self.subTest(rating=rating):
                response = self.client.post(self.url, {"rating": rating}, format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(ProductReview.objects.exists())

    def test_nota_obrigatoria_400(self):
        make_order_item(self.user, self.variation)
        self.client.force_authenticate(self.user)
        response = self.client.post(self.url, {"comment": "Sem nota"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_comentario_longo_400(self):
        make_order_item(self.user, self.variation)
        self.client.force_authenticate(self.user)
        response = self.client.post(
            self.url, {"rating": 5, "comment": "a" * 1001}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_campos_proibidos_400(self):
        make_order_item(self.user, self.variation)
        other = make_user()
        self.client.force_authenticate(self.user)
        for extra in (
            {"status": "REMOVED"},
            {"user": str(other.id)},
            {"product": str(self.product.id)},
            {"admin_reply": "Oficial"},
            {"purchased_size": "GG"},
        ):
            with self.subTest(extra=extra):
                response = self.client.post(
                    self.url, {"rating": 5, **extra}, format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(ProductReview.objects.exists())

    def test_corpo_que_nao_e_objeto_400(self):
        self.client.force_authenticate(self.user)
        make_order_item(self.user, self.variation)
        for body in ([{"rating": 5}], 5):
            with self.subTest(body=body):
                response = self.client.post(self.url, body, format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(ProductReview.objects.exists())

    def test_limite_de_20_por_hora(self):
        self.client.force_authenticate(self.user)
        for _ in range(20):
            self.client.post(self.url, {"rating": 5}, format="json")
        response = self.client.post(self.url, {"rating": 5}, format="json")
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


class AuthorReviewApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.product, (self.variation,) = make_product()
        self.user = make_user()
        self.stranger = make_user()
        make_order_item(self.user, self.variation)
        self.review = create_review(self.user, self.product, rating=5)

    def test_editar_propria(self):
        self.client.force_authenticate(self.user)

        response = self.client.patch(
            review_url(self.review), {"rating": 2}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["rating"], 2)
        self.product.refresh_from_db()
        self.assertEqual(self.product.rating_avg, Decimal("2.00"))

    def test_editar_removida_republica(self):
        remove_review(
            self.review, admin=make_user(admin=True), reason=RemovalReason.OFFENSIVE
        )
        self.client.force_authenticate(self.user)

        response = self.client.patch(
            review_url(self.review), {"comment": "Corrigido"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], ReviewStatus.PUBLISHED)
        self.assertIsNone(response.data["removal_reason"])

    def test_patch_com_campo_proibido_400(self):
        self.client.force_authenticate(self.user)
        forbidden = [
            {"status": "REMOVED"},
            {"user": str(self.stranger.id)},
            {"product": str(self.product.id)},
            {"admin_reply": "Oficial"},
            {"purchased_size": "GG"},
        ]
        for extra in forbidden:
            with self.subTest(extra=extra):
                response = self.client.patch(
                    review_url(self.review), {"rating": 1, **extra}, format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.review.refresh_from_db()
        self.assertEqual(self.review.rating, 5)
        self.assertEqual(self.review.status, ReviewStatus.PUBLISHED)
        self.assertEqual(self.review.admin_reply, "")

    def test_editar_ou_excluir_de_outra_pessoa_404(self):
        self.client.force_authenticate(self.stranger)

        patch = self.client.patch(review_url(self.review), {"rating": 1}, format="json")
        delete = self.client.delete(review_url(self.review))

        self.assertEqual(patch.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(delete.status_code, status.HTTP_404_NOT_FOUND)
        self.review.refresh_from_db()
        self.assertEqual(self.review.rating, 5)

    def test_sem_login_401(self):
        response = self.client.delete(review_url(self.review))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_excluir_propria(self):
        self.client.force_authenticate(self.user)

        response = self.client.delete(review_url(self.review))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ProductReview.objects.exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.rating_count, 0)

    def test_minhas_avaliacoes_mostra_motivo_e_so_as_minhas(self):
        make_order_item(self.stranger, self.variation)
        create_review(self.stranger, self.product, rating=1)
        remove_review(
            self.review,
            admin=make_user(admin=True),
            reason=RemovalReason.OTHER,
            note="Contém link",
        )
        self.client.force_authenticate(self.user)

        response = self.client.get("/api/reviews/mine/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        item = response.data["results"][0]
        self.assertEqual(item["id"], str(self.review.id))
        self.assertEqual(item["status"], ReviewStatus.REMOVED)
        self.assertEqual(item["removal_reason"], RemovalReason.OTHER)
        self.assertEqual(item["removal_reason_label"], "Outro")
        self.assertEqual(item["removal_note"], "Contém link")
        self.assertEqual(item["product_id"], str(self.product.id))
        self.assertEqual(item["product_name"], self.product.name)

    def test_produto_desativado_continua_em_minhas_avaliacoes(self):
        Product.objects.filter(pk=self.product.pk).update(is_active=False)
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/reviews/mine/")
        self.assertEqual(response.data["count"], 1)

    def test_minhas_avaliacoes_sem_login_401(self):
        response = self.client.get("/api/reviews/mine/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class EligibilityApiTests(APITestCase):
    def setUp(self):
        self.product, (self.variation,) = make_product()
        self.user = make_user()
        self.url = product_url(self.product, "eligibility/")

    def test_sem_login_401(self):
        self.assertEqual(
            self.client.get(self.url).status_code, status.HTTP_401_UNAUTHORIZED
        )

    def test_sem_compra(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.data, {"can_review": False, "review_id": None})

    def test_com_compra_entregue(self):
        make_order_item(self.user, self.variation)
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.data, {"can_review": True, "review_id": None})

    def test_ja_avaliou_retorna_id(self):
        make_order_item(self.user, self.variation)
        review = create_review(self.user, self.product, rating=4)
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(
            response.data, {"can_review": True, "review_id": str(review.id)}
        )
