import threading
import time
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, ProtectedError
from django.test import (
    TestCase,
    TransactionTestCase,
    override_settings,
    skipUnlessDBFeature,
)
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from authentication.models import Address
from orders.coupons import (
    COUPON_ERROR_MESSAGES,
    count_coupon_uses,
    resolve_coupon,
    valid_order_q,
)
from orders.models import (
    Cart,
    CartItem,
    Coupon,
    CouponDiscountType,
    CustomerOrder,
    OrderStatus,
    ShippingQuote,
    normalize_coupon_code,
)
from products.models import Category, DropCampaign, Product, ProductVariation

User = get_user_model()


def create_coupon(**fields):
    data = {
        "code": "VERAO20",
        "discount_type": CouponDiscountType.PERCENTAGE,
        "discount_value": Decimal("20.00"),
    }
    data.update(fields)
    return Coupon.objects.create(**data)


def create_order(user, *, coupon=None, order_status=OrderStatus.PAID, **fields):
    return CustomerOrder.objects.create(
        user=user,
        coupon=coupon,
        status=order_status,
        **fields,
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


class CouponUsageCountTests(TestCase):
    def setUp(self):
        self.coupon = create_coupon()
        self.user = User.objects.create_user(email="cliente@shio.com", name="Cliente")
        self.other = User.objects.create_user(email="outro@shio.com", name="Outro")

    def test_cupom_sem_pedidos_tem_zero_usos(self):
        self.assertEqual(count_coupon_uses(self.coupon), 0)
        self.assertEqual(count_coupon_uses(self.coupon, user=self.user), 0)

    def test_conta_usos_totais_e_por_usuario(self):
        create_order(self.user, coupon=self.coupon)
        create_order(self.user, coupon=self.coupon, order_status=OrderStatus.DELIVERED)
        create_order(self.other, coupon=self.coupon)
        create_order(self.user)  # pedido sem cupom não conta

        self.assertEqual(count_coupon_uses(self.coupon), 3)
        self.assertEqual(count_coupon_uses(self.coupon, user=self.user), 2)
        self.assertEqual(count_coupon_uses(self.coupon, user=self.other), 1)

    def test_pedido_cancelado_devolve_o_uso(self):
        order = create_order(self.user, coupon=self.coupon)
        self.assertEqual(count_coupon_uses(self.coupon), 1)

        order.status = OrderStatus.CANCELED
        order.save(update_fields=["status"])

        self.assertEqual(count_coupon_uses(self.coupon), 0)
        self.assertEqual(count_coupon_uses(self.coupon, user=self.user), 0)

    def test_pedido_aguardando_pagamento_com_reserva_vencida_ainda_conta(self):
        create_order(
            self.user,
            coupon=self.coupon,
            order_status=OrderStatus.AWAITING_PAYMENT,
            reservation_expires_at=timezone.now() - timedelta(minutes=1),
        )

        self.assertEqual(count_coupon_uses(self.coupon, user=self.user), 1)

    def test_filtro_funciona_a_partir_do_cupom_numa_unica_consulta(self):
        other_coupon = create_coupon(code="INVERNO10")
        create_order(self.user, coupon=self.coupon)
        create_order(self.other, coupon=self.coupon, order_status=OrderStatus.CANCELED)
        create_order(self.other, coupon=other_coupon)

        with self.assertNumQueries(1):
            uses = dict(
                Coupon.objects.annotate(
                    uses=Count("orders", filter=valid_order_q("orders__"))
                ).values_list("code", "uses")
            )

        self.assertEqual(uses, {"VERAO20": 1, "INVERNO10": 1, "BEMVINDO10": 0})


class WelcomeDiscountEligibilityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="novo@shio.com", name="Novo")

    def assert_eligible(self, expected):
        category = Category.objects.get_or_create(name="Boas-vindas", slug="bv")[0]
        product = Product.objects.create(
            name="Camiseta", description="x", base_price=200, category=category
        )
        variation = ProductVariation.objects.create(
            product=product, size="M", sku=f"BV-{product.pk}", stock_quantity=1
        )
        items = [{"variation": variation, "total_price": Decimal("200.00")}]

        preview = resolve_coupon(self.user, items, Decimal("200.00"))
        with transaction.atomic():
            locked = resolve_coupon(self.user, items, Decimal("200.00"), lock=True)

        self.assertEqual(preview.coupon is not None, expected)
        self.assertEqual(locked.coupon is not None, expected)

    def test_cliente_sem_pedidos_e_elegivel(self):
        self.assert_eligible(True)

    def test_pedido_cancelado_nao_tira_o_desconto_de_primeira_compra(self):
        coupon = Coupon.objects.get(code="BEMVINDO10")
        create_order(self.user, coupon=coupon, order_status=OrderStatus.CANCELED)

        self.assert_eligible(True)

    def test_pedido_pago_tira_o_desconto_de_primeira_compra(self):
        create_order(self.user, order_status=OrderStatus.PAID)

        self.assert_eligible(False)

    def test_pedido_aguardando_pagamento_tira_o_desconto_mesmo_com_reserva_vencida(
        self,
    ):
        create_order(
            self.user,
            order_status=OrderStatus.AWAITING_PAYMENT,
            reservation_expires_at=timezone.now() - timedelta(minutes=1),
        )

        self.assert_eligible(False)


class ResolveCouponTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="compra@shio.com", name="Cliente")
        self.verao = DropCampaign.objects.create(name="Verão", slug="verao")
        self.camisetas = Category.objects.create(name="Camisetas", slug="camisetas")
        self.calcas = Category.objects.create(name="Calças", slug="calcas")
        self.camiseta_verao = self.item(
            drop=self.verao, category=self.camisetas, total="120.00"
        )
        self.calca = self.item(category=self.calcas, total="200.00")
        self.items = [self.camiseta_verao, self.calca]
        self.subtotal = Decimal("320.00")
        # Isola os testes de código digitado do cupom automático da seed.
        Coupon.objects.filter(code="BEMVINDO10").update(is_active=False)

    def item(self, *, total, drop=None, category=None):
        product = Product.objects.create(
            name=f"Produto {Product.objects.count()}",
            description="x",
            base_price=total,
            drop=drop,
            category=category,
        )
        variation = ProductVariation.objects.create(
            product=product,
            size="M",
            sku=f"SKU-{product.pk}",
            stock_quantity=10,
        )
        return {"variation": variation, "total_price": Decimal(total)}

    def resolve(self, code=None, **kwargs):
        return resolve_coupon(self.user, self.items, self.subtotal, code, **kwargs)

    def assert_error(self, result, error_code):
        self.assertEqual(result.error_code, error_code)
        self.assertIsNone(result.coupon)
        self.assertEqual(result.discount, Decimal("0.00"))
        self.assertTrue(result.error_message)


class ResolveCouponValidationTests(ResolveCouponTestCase):
    def test_codigo_inexistente(self):
        self.assert_error(self.resolve("NAOEXISTE"), "coupon_not_found")

    def test_cupom_inativo(self):
        create_coupon(is_active=False)
        self.assert_error(self.resolve("VERAO20"), "coupon_inactive")

    def test_cupom_que_ainda_nao_comecou(self):
        create_coupon(starts_at=timezone.now() + timedelta(days=1))
        self.assert_error(self.resolve("VERAO20"), "coupon_not_started")

    def test_cupom_expirado(self):
        create_coupon(expiration_date=timezone.now() - timedelta(seconds=1))
        self.assert_error(self.resolve("VERAO20"), "coupon_expired")

    def test_cupom_de_primeira_compra_para_quem_ja_comprou(self):
        create_coupon(first_purchase_only=True)
        create_order(self.user)
        self.assert_error(self.resolve("VERAO20"), "coupon_first_purchase_only")

    def test_valor_minimo_informa_quanto_falta(self):
        create_coupon(min_order_value=Decimal("357.50"))

        result = self.resolve("VERAO20")

        self.assert_error(result, "coupon_min_value")
        self.assertEqual(result.error_message, "Faltam R$ 37,50 para usar este cupom.")

    def test_valor_minimo_e_comparado_com_o_subtotal_inteiro(self):
        coupon = create_coupon(min_order_value=Decimal("320.00"))
        coupon.drops.add(self.verao)

        result = self.resolve("VERAO20")

        self.assertIsNone(result.error_code)
        self.assertEqual(result.discount, Decimal("24.00"))

    def test_nenhum_item_no_escopo(self):
        inverno = DropCampaign.objects.create(name="Inverno", slug="inverno")
        coupon = create_coupon()
        coupon.drops.add(inverno)
        self.assert_error(self.resolve("VERAO20"), "coupon_not_applicable")

    def test_limite_total_atingido(self):
        coupon = create_coupon(max_uses_total=1)
        other = User.objects.create_user(email="outro@shio.com", name="Outro")
        create_order(other, coupon=coupon)
        self.assert_error(self.resolve("VERAO20"), "coupon_limit_reached")

    def test_limite_por_cliente_atingido(self):
        coupon = create_coupon(max_uses_per_user=1)
        create_order(self.user, coupon=coupon)
        self.assert_error(self.resolve("VERAO20"), "coupon_user_limit_reached")

    def test_pedido_cancelado_nao_conta_para_os_limites(self):
        coupon = create_coupon(max_uses_total=1, max_uses_per_user=1)
        create_order(self.user, coupon=coupon, order_status=OrderStatus.CANCELED)

        self.assertIsNone(self.resolve("VERAO20").error_code)

    def test_primeira_verificacao_que_falha_define_o_erro(self):
        create_coupon(
            is_active=False, expiration_date=timezone.now() - timedelta(days=1)
        )
        self.assert_error(self.resolve("VERAO20"), "coupon_inactive")

    def test_todos_os_codigos_de_erro_tem_mensagem(self):
        self.assertEqual(len(COUPON_ERROR_MESSAGES), 9)


class ResolveCouponDiscountTests(ResolveCouponTestCase):
    def test_percentual_sobre_o_carrinho_inteiro(self):
        create_coupon(discount_value=Decimal("10.00"))

        result = self.resolve("VERAO20")

        self.assertEqual(result.coupon.code, "VERAO20")
        self.assertEqual(result.discount, Decimal("32.00"))

    def test_percentual_respeita_o_teto(self):
        create_coupon(max_discount_amount=Decimal("50.00"))
        self.assertEqual(self.resolve("VERAO20").discount, Decimal("50.00"))

    def test_escopo_misto_do_documento(self):
        """Camiseta do drop Verão (R$ 120) + calça fora dele (R$ 200), 20% só no
        drop com teto de R$ 50: desconta R$ 24."""
        coupon = create_coupon(max_discount_amount=Decimal("50.00"))
        coupon.drops.add(self.verao)
        self.assertEqual(self.resolve("VERAO20").discount, Decimal("24.00"))

    def test_escopo_por_categoria(self):
        coupon = create_coupon(discount_value=Decimal("10.00"))
        coupon.categories.add(self.calcas)
        self.assertEqual(self.resolve("VERAO20").discount, Decimal("20.00"))

    def test_escopo_por_drop_ou_categoria(self):
        coupon = create_coupon(discount_value=Decimal("10.00"))
        coupon.drops.add(self.verao)
        coupon.categories.add(self.calcas)
        self.assertEqual(self.resolve("VERAO20").discount, Decimal("32.00"))

    def test_fixo_nunca_passa_da_base_elegivel(self):
        coupon = create_coupon(
            code="CAMISA50",
            discount_type=CouponDiscountType.FIXED_VALUE,
            discount_value=Decimal("150.00"),
        )
        coupon.categories.add(self.camisetas)
        self.assertEqual(self.resolve("CAMISA50").discount, Decimal("120.00"))

    def test_fixo_menor_que_a_base(self):
        create_coupon(
            discount_type=CouponDiscountType.FIXED_VALUE,
            discount_value=Decimal("15.00"),
        )
        self.assertEqual(self.resolve("VERAO20").discount, Decimal("15.00"))

    def test_arredonda_centavos_para_cima_na_metade(self):
        self.items = [self.item(total="0.25")]
        self.subtotal = Decimal("0.25")
        create_coupon(discount_value=Decimal("10.00"))
        # 10% de R$ 0,25 = 0,025 -> 0,03 (ROUND_HALF_UP)
        self.assertEqual(self.resolve("VERAO20").discount, Decimal("0.03"))

    def test_codigo_digitado_em_minusculas_e_com_espacos(self):
        create_coupon()
        self.assertEqual(self.resolve("  verao 20 ").coupon.code, "VERAO20")


class ResolveCouponAutoApplyTests(ResolveCouponTestCase):
    def setUp(self):
        super().setUp()
        Coupon.objects.filter(code="BEMVINDO10").update(is_active=True)

    def test_sem_codigo_aplica_o_cupom_automatico(self):
        result = self.resolve()

        self.assertEqual(result.coupon.code, "BEMVINDO10")
        self.assertEqual(result.discount, Decimal("32.00"))

    def test_sem_codigo_e_sem_elegibilidade_nao_e_erro(self):
        create_order(self.user)

        result = self.resolve()

        self.assertIsNone(result.coupon)
        self.assertEqual(result.discount, Decimal("0.00"))
        self.assertIsNone(result.error_code)

    def test_codigo_vazio_e_o_mesmo_que_sem_codigo(self):
        self.assertEqual(self.resolve("   ").coupon.code, "BEMVINDO10")

    def test_codigo_digitado_substitui_o_automatico(self):
        create_coupon(discount_value=Decimal("5.00"))
        self.assertEqual(self.resolve("VERAO20").coupon.code, "VERAO20")

    def test_codigo_invalido_nao_cai_para_o_automatico(self):
        self.assert_error(self.resolve("NAOEXISTE"), "coupon_not_found")

    def test_cliente_anonimo_nao_recebe_cupom_automatico(self):
        result = resolve_coupon(AnonymousUser(), self.items, self.subtotal)
        self.assertEqual(result.coupon, None)

    def test_cliente_anonimo_nao_pode_digitar_codigo(self):
        with self.assertRaises(ValueError):
            resolve_coupon(AnonymousUser(), self.items, self.subtotal, "VERAO20")

    # No SQLite (core.settings.test) o Django omite o FOR UPDATE.
    @skipUnlessDBFeature("has_select_for_update")
    def test_lock_trava_a_linha_do_cupom(self):
        create_coupon()
        with CaptureQueriesContext(connection) as queries, transaction.atomic():
            self.resolve("VERAO20", lock=True)

        coupon_queries = [q["sql"] for q in queries if '"orders_coupon"' in q["sql"]]
        self.assertIn("FOR UPDATE", coupon_queries[0])


class ResolveCouponEmptyCartTests(ResolveCouponTestCase):
    def test_cupom_sem_restricao_com_carrinho_vazio_vale_com_desconto_zero(self):
        create_coupon()
        self.items, self.subtotal = [], Decimal("0.00")

        result = self.resolve("VERAO20")

        self.assertEqual(result.coupon.code, "VERAO20")
        self.assertEqual(result.discount, Decimal("0.00"))

    def test_cupom_restrito_com_carrinho_vazio_nao_se_aplica(self):
        coupon = create_coupon()
        coupon.drops.add(self.verao)
        self.items, self.subtotal = [], Decimal("0.00")

        self.assert_error(self.resolve("VERAO20"), "coupon_not_applicable")


QUOTE_URL = "/api/orders/checkout/calculate/"
CHECKOUT_URL = "/api/orders/checkout/"
SHIPPING_SETTINGS = {
    "CORREIOS_REMETENTE_CEP": "70000000",
    "CORREIOS_CODIGO_SERVICO": "03220",
    "CORREIOS_PESO_PADRAO_GRAMAS": "300",
}


@override_settings(**SHIPPING_SETTINGS)
class CouponCheckoutFlowTestCase(APITestCase):
    """Base dos testes do cupom pela API: carrinho de R$ 200 e frete de R$ 15."""

    def setUp(self):
        self.user = User.objects.create_user(email="api@shio.com", name="Cliente")
        self.address = Address.objects.create(
            user=self.user,
            zip_code="71000000",
            street="Rua",
            address_number="1",
            neighborhood="Centro",
            city="Brasília",
            state="DF",
        )
        category = Category.objects.create(name="Fluxo", slug="fluxo")
        product = Product.objects.create(
            name="Jaqueta", description="x", base_price=200, category=category
        )
        self.variation = ProductVariation.objects.create(
            product=product, size="M", sku="FLUXO-M", stock_quantity=10
        )
        cart = Cart.objects.create(user=self.user, status="ACTIVE")
        CartItem.objects.create(
            cart=cart, variation=self.variation, quantity=1, unit_price=200
        )
        self.client.force_authenticate(user=self.user)

    def quote(self, coupon_code=None):
        payload = {"address_id": str(self.address.pk)}
        if coupon_code is not None:
            payload["coupon_code"] = coupon_code
        with (
            patch(
                "orders.services.fetch_shipping_price_by_service_and_ceps",
                return_value={"pcFinal": "15,00"},
            ),
            patch(
                "orders.services.fetch_shipping_deadline_by_service_and_ceps",
                return_value={"prazoEntrega": 3},
            ),
        ):
            return self.client.post(QUOTE_URL, payload, format="json")


class CouponQuoteTests(CouponCheckoutFlowTestCase):
    def test_sem_codigo_aplica_o_bemvindo10(self):
        response = self.quote()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["discount_amount"], "20.00")
        self.assertEqual(response.data["coupon"]["code"], "BEMVINDO10")
        self.assertEqual(ShippingQuote.objects.get().coupon_code, "")

    def test_codigo_valido_substitui_o_automatico(self):
        create_coupon(discount_value=Decimal("25.00"))

        response = self.quote(" verao20 ")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["discount_amount"], "50.00")
        self.assertEqual(response.data["total_amount"], "165.00")
        self.assertEqual(
            response.data["coupon"],
            {"code": "VERAO20", "type": "PERCENTAGE", "value": "25.00"},
        )
        self.assertEqual(ShippingQuote.objects.get().coupon_code, "VERAO20")

    def test_sem_cupom_aplicavel_devolve_coupon_nulo(self):
        create_order(self.user)

        response = self.quote()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["coupon"])
        self.assertEqual(response.data["discount_amount"], "0.00")

    def test_codigo_vazio_e_o_mesmo_que_sem_codigo(self):
        response = self.quote("")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["coupon"]["code"], "BEMVINDO10")

    def test_codigo_invalido_recusa_a_cotacao_com_o_codigo_do_erro(self):
        response = self.quote("NAOEXISTE")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json(),
            {"coupon_code": ["Cupom não encontrado."], "code": "coupon_not_found"},
        )
        self.assertFalse(ShippingQuote.objects.exists())

    def test_cupom_expirado_informa_o_motivo(self):
        create_coupon(expiration_date=timezone.now() - timedelta(days=1))

        response = self.quote("VERAO20")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["code"], "coupon_expired")
        self.assertEqual(response.json()["coupon_code"], ["Este cupom expirou."])

    def test_checkout_recusa_coupon_code(self):
        response = self.client.post(
            CHECKOUT_URL,
            {
                "address_id": str(self.address.pk),
                "shipping_quote_id": str(uuid.uuid4()),
                "idempotency_key": str(uuid.uuid4()),
                "coupon_code": "VERAO20",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("coupon_code", response.json())
        self.assertNotIn("code", response.json())


class ConcurrentCouponLastUseTests(TransactionTestCase):
    """Duas compras disputando o último uso de um cupom: só uma pode levar.

    A primeira transação trava o cupom e só grava o pedido depois que a
    segunda já tentou validar. Com a trava, a segunda espera o commit e vê o
    cupom esgotado; sem ela, as duas veriam zero usos e passariam.
    """

    def setUp(self):
        self.coupon = create_coupon(max_uses_total=1)
        self.users = [
            User.objects.create_user(email=f"disputa{i}@shio.com", name=f"C{i}")
            for i in (1, 2)
        ]
        category = Category.objects.create(name="Disputa", slug="disputa")
        product = Product.objects.create(
            name="Camiseta", description="x", base_price=100, category=category
        )
        variation = ProductVariation.objects.create(
            product=product, size="M", sku="DISPUTA-M", stock_quantity=10
        )
        self.items = [{"variation": variation, "total_price": Decimal("100.00")}]

    # SQLite trava a tabela inteira; só faz sentido com locks de linha (Postgres).
    @skipUnlessDBFeature("has_select_for_update")
    def test_ultimo_uso_vai_para_uma_compra_so(self):
        first_locked = threading.Event()
        second_started = threading.Event()
        results = {}

        def buy(key, user, *, first):
            try:
                if not first:
                    first_locked.wait(timeout=10)
                with transaction.atomic():
                    if not first:
                        second_started.set()
                    result = resolve_coupon(
                        user, self.items, Decimal("100.00"), "VERAO20", lock=True
                    )
                    if first:
                        first_locked.set()
                        # Dá tempo de a segunda compra tentar validar antes do commit.
                        second_started.wait(timeout=10)
                        time.sleep(0.3)
                    if result.error_code is None:
                        create_order(user, coupon=result.coupon)
                results[key] = result.error_code
            except Exception as exc:  # noqa: BLE001 - o teste reporta o erro
                results[key] = repr(exc)
            finally:
                connection.close()

        threads = [
            threading.Thread(
                target=buy, args=("a", self.users[0]), kwargs={"first": True}
            ),
            threading.Thread(
                target=buy, args=("b", self.users[1]), kwargs={"first": False}
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(results, {"a": None, "b": "coupon_limit_reached"})
        self.assertEqual(count_coupon_uses(self.coupon), 1)
