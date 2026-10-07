from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase
from django.utils import timezone

from orders.models import (
    Coupon,
    CouponDiscountType,
    CustomerOrder,
    OrderStatus,
    normalize_coupon_code,
)

User = get_user_model()


def create_coupon(**fields):
    data = {
        "code": "VERAO20",
        "discount_type": CouponDiscountType.PERCENTAGE,
        "discount_value": Decimal("20.00"),
    }
    data.update(fields)
    return Coupon.objects.create(**data)


def create_order(user, *, coupon=None, order_status=OrderStatus.PAID):
    return CustomerOrder.objects.create(
        user=user,
        coupon=coupon,
        status=order_status,
        subtotal=Decimal("100.00"),
        total_amount=Decimal("100.00"),
        shipping_zip_code="70000000",
        shipping_street="Rua",
        shipping_number="1",
        shipping_neighborhood="Centro",
        shipping_city="Brasília",
        shipping_state="DF",
    )


class CouponCodeNormalizationTests(TestCase):
    def test_normaliza_para_maiusculas_sem_espacos(self):
        self.assertEqual(normalize_coupon_code(" verao 20\n"), "VERAO20")

    def test_save_grava_o_codigo_normalizado(self):
        coupon = create_coupon(code=" verao20 ")

        coupon.refresh_from_db()
        self.assertEqual(coupon.code, "VERAO20")
        self.assertEqual(str(coupon), "VERAO20")

    def test_codigo_em_caixa_diferente_e_o_mesmo_cupom(self):
        create_coupon(code="VERAO20")

        with self.assertRaises(IntegrityError), transaction.atomic():
            create_coupon(code="verao20")

    def test_banco_recusa_codigo_fora_do_padrao_mesmo_sem_save(self):
        coupon = create_coupon()

        # update() não chama save(); quem barra é a constraint do banco.
        with self.assertRaises(IntegrityError), transaction.atomic():
            Coupon.objects.filter(pk=coupon.pk).update(code="verao20")


class CouponConstraintTests(TestCase):
    def assert_rejected(self, **fields):
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_coupon(**fields)

    def test_valor_do_desconto_precisa_ser_positivo(self):
        self.assert_rejected(discount_value=Decimal("0.00"))
        self.assert_rejected(
            discount_type=CouponDiscountType.FIXED_VALUE,
            discount_value=Decimal("-5.00"),
        )

    def test_percentual_no_maximo_100(self):
        self.assert_rejected(discount_value=Decimal("100.01"))
        create_coupon(code="TUDO", discount_value=Decimal("100.00"))

    def test_valor_fixo_pode_passar_de_100(self):
        create_coupon(
            discount_type=CouponDiscountType.FIXED_VALUE,
            discount_value=Decimal("150.00"),
        )

    def test_teto_do_desconto_so_em_cupom_percentual(self):
        self.assert_rejected(
            discount_type=CouponDiscountType.FIXED_VALUE,
            discount_value=Decimal("10.00"),
            max_discount_amount=Decimal("50.00"),
        )
        self.assert_rejected(max_discount_amount=Decimal("0.00"))
        create_coupon(max_discount_amount=Decimal("50.00"))

    def test_valor_minimo_precisa_ser_positivo(self):
        self.assert_rejected(min_order_value=Decimal("0.00"))
        create_coupon(min_order_value=Decimal("200.00"))

    def test_inicio_precisa_ser_antes_da_expiracao(self):
        now = timezone.now()
        self.assert_rejected(starts_at=now, expiration_date=now)
        self.assert_rejected(starts_at=now, expiration_date=now - timedelta(days=1))
        create_coupon(starts_at=now, expiration_date=now + timedelta(days=1))


class CouponDeletionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="cupom@shio.com", name="Cliente")

    def test_cupom_usado_em_pedido_nao_pode_ser_apagado(self):
        coupon = create_coupon()
        create_order(self.user, coupon=coupon)

        with self.assertRaises(ProtectedError):
            coupon.delete()

    def test_cupom_usado_so_em_pedido_cancelado_tambem_e_protegido(self):
        coupon = create_coupon()
        create_order(self.user, coupon=coupon, order_status=OrderStatus.CANCELED)

        with self.assertRaises(ProtectedError):
            coupon.delete()

    def test_cupom_nunca_usado_pode_ser_apagado(self):
        coupon = create_coupon()

        coupon.delete()

        self.assertFalse(Coupon.objects.filter(code="VERAO20").exists())


class WelcomeCouponMigrationTests(TestCase):
    def test_bemvindo10_vira_cupom_automatico_de_primeira_compra(self):
        coupon = Coupon.objects.get(code="BEMVINDO10")

        self.assertTrue(coupon.first_purchase_only)
        self.assertTrue(coupon.auto_apply)
        self.assertEqual(coupon.max_uses_per_user, 1)
        self.assertIsNone(coupon.max_uses_total)
        self.assertFalse(coupon.drops.exists())
        self.assertFalse(coupon.categories.exists())
