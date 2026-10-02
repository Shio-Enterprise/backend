from decimal import Decimal

from django.test import TestCase

from orders.models import OrderItem, OrderStatus, PaymentStatus

from .factories import make_order_item, make_product, make_user
from .models import ProductReview, RemovalReason, ReviewStatus
from .services import (
    AlreadyReviewed,
    NotEligible,
    create_review,
    delete_review,
    get_eligible_order_item,
    update_review,
)


class EligibilityTests(TestCase):
    def setUp(self):
        self.product, (self.variation_m, self.variation_g) = make_product(
            sizes=("M", "G")
        )
        self.user = make_user()

    def test_sem_pedido_nao_e_elegivel(self):
        self.assertIsNone(get_eligible_order_item(self.user, self.product))

    def test_pedido_nao_entregue_nao_e_elegivel(self):
        for order_status in (
            OrderStatus.AWAITING_PAYMENT,
            OrderStatus.PAID,
            OrderStatus.PREPARING,
            OrderStatus.SHIPPED,
            OrderStatus.CANCELED,
        ):
            with self.subTest(order_status=order_status):
                make_order_item(self.user, self.variation_m, order_status=order_status)
                self.assertIsNone(get_eligible_order_item(self.user, self.product))

    def test_pedido_entregue_e_elegivel(self):
        item = make_order_item(self.user, self.variation_m)
        self.assertEqual(get_eligible_order_item(self.user, self.product), item)

    def test_entregue_e_reembolsado_continua_elegivel(self):
        item = make_order_item(
            self.user, self.variation_m, payment_status=PaymentStatus.REFUNDED
        )
        self.assertEqual(get_eligible_order_item(self.user, self.product), item)

    def test_pedido_de_outra_pessoa_nao_conta(self):
        make_order_item(make_user(), self.variation_m)
        self.assertIsNone(get_eligible_order_item(self.user, self.product))

    def test_usa_o_pedido_entregue_mais_recente(self):
        make_order_item(self.user, self.variation_m, days_ago=3)
        latest = make_order_item(self.user, self.variation_g)
        self.assertEqual(get_eligible_order_item(self.user, self.product), latest)


class CreateReviewTests(TestCase):
    def setUp(self):
        self.product, (self.variation,) = make_product(sizes=("M",))
        self.user = make_user(name="Maria Silva")

    def test_cria_publicada_com_tamanho_e_atualiza_media(self):
        item = make_order_item(self.user, self.variation)

        review = create_review(
            self.user, self.product, rating=4, comment="  Bom  ", fit="SMALL"
        )

        self.assertEqual(review.status, ReviewStatus.PUBLISHED)
        self.assertEqual(review.order_item, item)
        self.assertEqual(review.purchased_size, "M")
        self.assertEqual(review.comment, "Bom")
        self.assertEqual(review.fit, "SMALL")
        self.product.refresh_from_db()
        self.assertEqual(self.product.rating_avg, Decimal("4.00"))
        self.assertEqual(self.product.rating_count, 1)

    def test_comentario_so_com_espacos_vira_vazio(self):
        make_order_item(self.user, self.variation)
        review = create_review(self.user, self.product, rating=5, comment="   ")
        self.assertEqual(review.comment, "")

    def test_nao_elegivel_levanta_erro_e_nao_grava(self):
        with self.assertRaises(NotEligible):
            create_review(self.user, self.product, rating=5)
        self.assertFalse(ProductReview.objects.exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.rating_count, 0)

    def test_segunda_avaliacao_levanta_erro(self):
        make_order_item(self.user, self.variation)
        create_review(self.user, self.product, rating=5)
        with self.assertRaises(AlreadyReviewed):
            create_review(self.user, self.product, rating=1)
        self.assertEqual(ProductReview.objects.count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.rating_avg, Decimal("5.00"))


class UpdateReviewTests(TestCase):
    def setUp(self):
        self.product, (self.variation_m, self.variation_g) = make_product(
            sizes=("M", "G")
        )
        self.user = make_user()
        make_order_item(self.user, self.variation_m, days_ago=3)
        self.review = create_review(self.user, self.product, rating=5)

    def test_editar_nota_recalcula_media(self):
        update_review(self.review, rating=2, comment="Mudei de ideia")

        self.review.refresh_from_db()
        self.assertEqual(self.review.rating, 2)
        self.assertEqual(self.review.comment, "Mudei de ideia")
        self.product.refresh_from_db()
        self.assertEqual(self.product.rating_avg, Decimal("2.00"))

    def test_editar_atualiza_para_o_tamanho_entregue_mais_recente(self):
        latest = make_order_item(self.user, self.variation_g)

        update_review(self.review, rating=4)

        self.review.refresh_from_db()
        self.assertEqual(self.review.order_item, latest)
        self.assertEqual(self.review.purchased_size, "G")

    def test_editar_removida_republica_e_limpa_remocao(self):
        admin = make_user(admin=True)
        ProductReview.objects.filter(pk=self.review.pk).update(
            status=ReviewStatus.REMOVED,
            removal_reason=RemovalReason.OTHER,
            removal_note="Texto",
            removed_by=admin,
        )
        self.review.refresh_from_db()

        update_review(self.review, comment="Agora sem ofensa")

        self.review.refresh_from_db()
        self.assertEqual(self.review.status, ReviewStatus.PUBLISHED)
        self.assertEqual(self.review.removal_reason, "")
        self.assertEqual(self.review.removal_note, "")
        self.assertIsNone(self.review.removed_by)
        self.assertIsNone(self.review.removed_at)
        self.product.refresh_from_db()
        self.assertEqual(self.product.rating_count, 1)

    def test_campo_nao_editavel_levanta_erro(self):
        with self.assertRaises(ValueError):
            update_review(self.review, status=ReviewStatus.REMOVED)

    def test_item_do_pedido_apagado_mantem_tamanho_comprado(self):
        OrderItem.objects.filter(pk=self.review.order_item_id).delete()

        update_review(self.review, rating=3)

        self.review.refresh_from_db()
        self.assertIsNone(self.review.order_item)
        self.assertEqual(self.review.purchased_size, "M")


class DeleteReviewTests(TestCase):
    def test_excluir_recalcula_media(self):
        product, (variation,) = make_product()
        user = make_user()
        make_order_item(user, variation)
        review = create_review(user, product, rating=3)

        delete_review(review)

        self.assertFalse(ProductReview.objects.exists())
        product.refresh_from_db()
        self.assertEqual(product.rating_avg, Decimal("0.00"))
        self.assertEqual(product.rating_count, 0)
