from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase

from products.models import Product

from .factories import make_product, make_review, make_user
from .models import ReviewStatus
from .services import recompute_product_rating


class ProductReviewModelTests(TestCase):
    def setUp(self):
        self.product, _ = make_product()
        self.user = make_user()

    def test_produto_novo_comeca_sem_avaliacoes(self):
        self.assertEqual(self.product.rating_avg, Decimal("0.00"))
        self.assertEqual(self.product.rating_count, 0)

    def test_uma_avaliacao_por_cliente_por_produto(self):
        make_review(self.user, self.product)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_review(self.user, self.product)

    def test_banco_rejeita_nota_fora_de_1_a_5(self):
        for rating in (0, 6):
            with self.subTest(rating=rating):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    make_review(make_user(), self.product, rating=rating)


class RecomputeProductRatingTests(TestCase):
    def setUp(self):
        self.product, _ = make_product()

    def test_media_com_duas_casas_arredondando_para_cima(self):
        for rating in (5, 4, 4):
            make_review(make_user(), self.product, rating=rating)

        result = recompute_product_rating(self.product)

        self.product.refresh_from_db()
        self.assertEqual(self.product.rating_avg, Decimal("4.33"))
        self.assertEqual(self.product.rating_count, 3)
        self.assertEqual(result.rating_avg, Decimal("4.33"))

    def test_meio_arredonda_para_cima(self):
        for rating in (5, 4):
            make_review(make_user(), self.product, rating=rating)
        recompute_product_rating(self.product)
        self.assertEqual(self.product.rating_avg, Decimal("4.50"))

        make_review(make_user(), self.product, rating=4)
        make_review(make_user(), self.product, rating=4)
        make_review(make_user(), self.product, rating=4)
        make_review(make_user(), self.product, rating=4)
        make_review(make_user(), self.product, rating=4)
        make_review(make_user(), self.product, rating=4)
        recompute_product_rating(self.product)
        # (5 + 4*7) / 8 = 4.125 -> 4.13
        self.assertEqual(self.product.rating_avg, Decimal("4.13"))

    def test_removidas_nao_contam(self):
        make_review(make_user(), self.product, rating=5)
        make_review(make_user(), self.product, rating=1, status=ReviewStatus.REMOVED)

        recompute_product_rating(self.product)

        self.assertEqual(self.product.rating_avg, Decimal("5.00"))
        self.assertEqual(self.product.rating_count, 1)

    def test_sem_publicadas_volta_para_zero(self):
        Product.objects.filter(pk=self.product.pk).update(
            rating_avg=Decimal("3.00"), rating_count=7
        )
        make_review(make_user(), self.product, status=ReviewStatus.REMOVED)

        recompute_product_rating(self.product)

        self.product.refresh_from_db()
        self.assertEqual(self.product.rating_avg, Decimal("0.00"))
        self.assertEqual(self.product.rating_count, 0)

    def test_nao_mexe_em_outros_produtos(self):
        other, _ = make_product("Calça")
        make_review(make_user(), other, rating=2)
        make_review(make_user(), self.product, rating=5)

        recompute_product_rating(self.product)

        other.refresh_from_db()
        self.assertEqual(other.rating_count, 0)


class RecomputeRatingsCommandTests(TestCase):
    def test_comando_corrige_produtos_dessincronizados(self):
        product, _ = make_product()
        empty, _ = make_product("Boné")
        make_review(make_user(), product, rating=3)
        make_review(make_user(), product, rating=4)
        Product.objects.filter(pk=empty.pk).update(
            rating_avg=Decimal("5.00"), rating_count=9
        )
        out = StringIO()

        call_command("recompute_ratings", stdout=out)

        product.refresh_from_db()
        empty.refresh_from_db()
        self.assertEqual(product.rating_avg, Decimal("3.50"))
        self.assertEqual(product.rating_count, 2)
        self.assertEqual(empty.rating_avg, Decimal("0.00"))
        self.assertEqual(empty.rating_count, 0)
        self.assertIn("2 produtos recalculados", out.getvalue())
