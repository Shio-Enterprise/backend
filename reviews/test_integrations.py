from django.contrib import admin as django_admin
from django.test import RequestFactory
from rest_framework import status
from rest_framework.test import APITestCase

from orders.models import OrderStatus

from .factories import make_order_item, make_product, make_review, make_user
from .models import ProductReview
from .services import create_review, recompute_product_rating


class CatalogRatingTests(APITestCase):
    def setUp(self):
        self.top, _ = make_product("Top")
        self.mid_many, _ = make_product("Mid muitos")
        self.mid_few, _ = make_product("Mid poucos")
        self.none, _ = make_product("Sem avaliação")
        make_review(make_user(), self.top, rating=5)
        for _ in range(3):
            make_review(make_user(), self.mid_many, rating=4)
        make_review(make_user(), self.mid_few, rating=4)
        for product in (self.top, self.mid_many, self.mid_few):
            recompute_product_rating(product)

    def test_listagem_e_detalhe_expoem_media(self):
        listing = self.client.get("/api/catalog/products/")
        by_id = {p["id"]: p for p in listing.data["results"]}
        self.assertEqual(by_id[str(self.mid_many.id)]["rating_avg"], "4.00")
        self.assertEqual(by_id[str(self.mid_many.id)]["rating_count"], 3)
        self.assertEqual(by_id[str(self.none.id)]["rating_count"], 0)

        detail = self.client.get(f"/api/catalog/products/{self.top.id}/")
        self.assertEqual(detail.data["rating_avg"], "5.00")
        self.assertEqual(detail.data["rating_count"], 1)

    def test_ordenar_por_melhor_avaliacao_desempata_por_quantidade(self):
        response = self.client.get(
            "/api/catalog/products/", {"ordering": "-rating_avg"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [p["id"] for p in response.data["results"]]
        self.assertEqual(
            ids,
            [
                str(self.top.id),
                str(self.mid_many.id),
                str(self.mid_few.id),
                str(self.none.id),
            ],
        )


class OrderItemReviewFieldsTests(APITestCase):
    def setUp(self):
        self.user = make_user()
        self.product, (self.variation,) = make_product()
        self.client.force_authenticate(self.user)

    def get_item(self, order_item):
        response = self.client.get(f"/api/orders/my-orders/{order_item.order_id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["items"][0]

    def test_item_entregue_sem_avaliacao(self):
        item = make_order_item(self.user, self.variation)
        data = self.get_item(item)
        self.assertEqual(data["product_id"], str(self.product.id))
        self.assertTrue(data["can_review"])
        self.assertIsNone(data["review_id"])

    def test_item_entregue_ja_avaliado(self):
        item = make_order_item(self.user, self.variation)
        review = create_review(self.user, self.product, rating=5)
        data = self.get_item(item)
        self.assertTrue(data["can_review"])
        self.assertEqual(data["review_id"], str(review.id))

    def test_item_nao_entregue(self):
        item = make_order_item(
            self.user, self.variation, order_status=OrderStatus.SHIPPED
        )
        data = self.get_item(item)
        self.assertFalse(data["can_review"])
        self.assertIsNone(data["review_id"])

    def test_produto_inativo_nao_pode_avaliar(self):
        item = make_order_item(self.user, self.variation)
        type(self.product).objects.filter(pk=self.product.pk).update(is_active=False)
        data = self.get_item(item)
        self.assertFalse(data["can_review"])

    def test_avaliacao_de_outro_usuario_nao_vaza(self):
        item = make_order_item(self.user, self.variation)
        other = make_user()
        make_order_item(other, self.variation)
        create_review(other, self.product, rating=4)
        data = self.get_item(item)
        self.assertIsNone(data["review_id"])
        self.assertTrue(data["can_review"])

    def test_item_sem_variacao(self):
        item = make_order_item(self.user, self.variation)
        type(item).objects.filter(pk=item.pk).update(variation=None)
        data = self.get_item(item)
        self.assertIsNone(data["product_id"])
        self.assertFalse(data["can_review"])
        self.assertIsNone(data["review_id"])


class DjangoAdminReadOnlyTests(APITestCase):
    def test_admin_registrado_e_somente_leitura(self):
        self.assertIn(ProductReview, django_admin.site._registry)
        model_admin = django_admin.site._registry[ProductReview]
        request = RequestFactory().get("/admin/")
        request.user = make_user(admin=True)
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))
