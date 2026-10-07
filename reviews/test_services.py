from decimal import Decimal

from django.test import TestCase

from orders.models import OrderItem, OrderStatus, PaymentStatus

from .factories import make_order_item, make_product, make_review, make_user
from .models import ProductReview, RemovalReason, ReviewFit, ReviewStatus
from .services import (
    AlreadyReviewed,
    InvalidTransition,
    MissingRemovalNote,
    NotEligible,
    clear_reply,
    create_review,
    delete_review,
    get_eligible_order_item,
    rating_summary,
    recompute_product_rating,
    remove_review,
    restore_review,
    set_reply,
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


class ModerationTests(TestCase):
    def setUp(self):
        self.product, (self.variation,) = make_product()
        self.admin = make_user(admin=True)
        self.user = make_user()
        make_order_item(self.user, self.variation)
        self.review = create_review(self.user, self.product, rating=1)
        other = make_user()
        make_order_item(other, self.variation)
        create_review(other, self.product, rating=5)

    def test_remover_tira_da_media_e_grava_motivo(self):
        remove_review(self.review, admin=self.admin, reason=RemovalReason.SPAM)

        self.review.refresh_from_db()
        self.assertEqual(self.review.status, ReviewStatus.REMOVED)
        self.assertEqual(self.review.removal_reason, RemovalReason.SPAM)
        self.assertEqual(self.review.removed_by, self.admin)
        self.assertIsNotNone(self.review.removed_at)
        self.product.refresh_from_db()
        self.assertEqual(self.product.rating_avg, Decimal("5.00"))
        self.assertEqual(self.product.rating_count, 1)

    def test_outro_exige_texto(self):
        for note in ("", "   "):
            with self.subTest(note=note):
                with self.assertRaises(MissingRemovalNote):
                    remove_review(
                        self.review,
                        admin=self.admin,
                        reason=RemovalReason.OTHER,
                        note=note,
                    )
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, ReviewStatus.PUBLISHED)

    def test_outro_com_texto_grava_nota(self):
        remove_review(
            self.review, admin=self.admin, reason=RemovalReason.OTHER, note=" Golpe "
        )
        self.review.refresh_from_db()
        self.assertEqual(self.review.removal_note, "Golpe")

    def test_remover_duas_vezes_e_erro(self):
        remove_review(self.review, admin=self.admin, reason=RemovalReason.SPAM)
        with self.assertRaises(InvalidTransition):
            remove_review(self.review, admin=self.admin, reason=RemovalReason.SPAM)

    def test_restaurar_volta_para_media(self):
        remove_review(self.review, admin=self.admin, reason=RemovalReason.SPAM)

        restore_review(self.review)

        self.review.refresh_from_db()
        self.assertEqual(self.review.status, ReviewStatus.PUBLISHED)
        self.assertEqual(self.review.removal_reason, "")
        self.assertIsNone(self.review.removed_by)
        self.product.refresh_from_db()
        self.assertEqual(self.product.rating_avg, Decimal("3.00"))
        self.assertEqual(self.product.rating_count, 2)

    def test_restaurar_publicada_e_erro(self):
        with self.assertRaises(InvalidTransition):
            restore_review(self.review)


class ReplyTests(TestCase):
    def setUp(self):
        self.product, _ = make_product()
        self.review = make_review(make_user(), self.product, rating=4)

    def test_responder_grava_texto_e_data(self):
        set_reply(self.review, "  Obrigado!  ")
        self.review.refresh_from_db()
        self.assertEqual(self.review.admin_reply, "Obrigado!")
        self.assertIsNotNone(self.review.admin_reply_at)

    def test_responder_nao_mexe_na_media(self):
        set_reply(self.review, "Obrigado!")
        self.product.refresh_from_db()
        self.assertEqual(self.product.rating_count, 0)  # make_review não recalcula

    def test_nao_responde_removida(self):
        self.review.status = ReviewStatus.REMOVED
        self.review.save(update_fields=["status"])
        with self.assertRaises(InvalidTransition):
            set_reply(self.review, "Obrigado!")

    def test_apagar_resposta(self):
        set_reply(self.review, "Obrigado!")
        clear_reply(self.review)
        self.review.refresh_from_db()
        self.assertEqual(self.review.admin_reply, "")
        self.assertIsNone(self.review.admin_reply_at)


class SummaryTests(TestCase):
    def test_resumo_conta_so_publicadas(self):
        product, _ = make_product()
        make_review(make_user(), product, rating=5, fit=ReviewFit.TRUE_TO_SIZE)
        make_review(make_user(), product, rating=4, fit=ReviewFit.SMALL)
        make_review(make_user(), product, rating=4)
        make_review(
            make_user(),
            product,
            rating=1,
            fit=ReviewFit.LARGE,
            status=ReviewStatus.REMOVED,
        )
        recompute_product_rating(product)

        summary = rating_summary(product)

        self.assertEqual(summary["rating_avg"], Decimal("4.33"))
        self.assertEqual(summary["rating_count"], 3)
        self.assertEqual(
            summary["distribution"], {"1": 0, "2": 0, "3": 0, "4": 2, "5": 1}
        )
        self.assertEqual(summary["fit"], {"SMALL": 1, "TRUE_TO_SIZE": 1, "LARGE": 0})

    def test_resumo_vazio(self):
        product, _ = make_product()
        summary = rating_summary(product)
        self.assertEqual(summary["rating_count"], 0)
        self.assertEqual(summary["distribution"], {str(n): 0 for n in range(1, 6)})
        self.assertEqual(summary["fit"], {"SMALL": 0, "TRUE_TO_SIZE": 0, "LARGE": 0})


class LostUpdateTests(TestCase):
    def setUp(self):
        self.product, (variation,) = make_product()
        self.user = make_user()
        make_order_item(self.user, variation)
        self.review = create_review(self.user, self.product, rating=5)
        self.admin = make_user(admin=True)

    def test_edicao_do_autor_nao_apaga_resposta_do_admin(self):
        a = ProductReview.objects.get(pk=self.review.pk)
        b = ProductReview.objects.get(pk=self.review.pk)
        set_reply(a, "Obrigado")

        update_review(b, comment="Novo")

        self.review.refresh_from_db()
        self.assertEqual(self.review.admin_reply, "Obrigado")
        self.assertEqual(self.review.comment, "Novo")

    def test_remocao_do_admin_nao_reverte_texto_do_autor(self):
        a = ProductReview.objects.get(pk=self.review.pk)
        b = ProductReview.objects.get(pk=self.review.pk)
        update_review(a, comment="Novo")

        remove_review(b, admin=self.admin, reason=RemovalReason.SPAM)

        self.review.refresh_from_db()
        self.assertEqual(self.review.comment, "Novo")
        self.assertEqual(self.review.status, ReviewStatus.REMOVED)
