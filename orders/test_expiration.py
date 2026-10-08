import threading
import time
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from authentication.models import Address
from orders import expiration, services
from orders.expiration import release_expired_reservations
from orders.models import (
    Cart,
    CartItem,
    CustomerOrder,
    OrderItem,
    OrderStatus,
    OrderStatusLog,
    Payment,
    PaymentStatus,
)
from orders.services import (
    confirm_infinitepay_payment,
    release_if_expired,
    update_status,
)
from products.models import Category, Product, ProductVariation, StockMovement
from products.services import move_stock

User = get_user_model()

# Resposta verificada da InfinitePay para um pedido de R$ 200,00 pago no PIX.
VERIFIED_PAYMENT = {
    "success": True,
    "paid": True,
    "amount": 20000,
    "paid_amount": 20000,
    "installments": 1,
    "capture_method": "pix",
}


class ReservationMixin:
    """Pedido aguardando pagamento que reservou 2 de 10 unidades."""

    def setUp(self):
        self.user = User.objects.create_user(email="reserva@shio.com", name="Cliente")
        category = Category.objects.create(name="Reserva", slug="reserva")
        product = Product.objects.create(
            name="Jaqueta", description="x", base_price=100, category=category
        )
        self.variation = ProductVariation.objects.create(
            product=product, size="M", sku="RESERVA-M", stock_quantity=10
        )

    def create_reserved_order(
        self, *, expires_in=timedelta(minutes=-10), variation=None
    ):
        variation = variation or self.variation
        order = CustomerOrder.objects.create(
            user=self.user,
            status=OrderStatus.AWAITING_PAYMENT,
            subtotal=Decimal("200.00"),
            total_amount=Decimal("200.00"),
            shipping_zip_code="70000000",
            shipping_street="Rua",
            shipping_number="1",
            shipping_neighborhood="Centro",
            shipping_city="Brasília",
            shipping_state="DF",
            reservation_expires_at=timezone.now() + expires_in,
        )
        item = OrderItem.objects.create(
            order=order,
            variation=variation,
            quantity=2,
            unit_price=Decimal("100.00"),
            product_name="Jaqueta - M",
            sku_snapshot=variation.sku,
        )
        move_stock(
            variation=variation,
            kind="SAIDA",
            reason="VENDA",
            quantity=2,
            origin_type="ORDER",
            origin_id=order.pk,
            order_item=item,
            idempotency_key=f"sale:{item.pk}",
            created_by=self.user,
        )
        Payment.objects.create(order=order, total_amount=order.total_amount)
        return order

    def assert_stock(self, quantity):
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, quantity)

    def returns(self):
        return StockMovement.objects.filter(
            variation=self.variation, reason="DEVOLUCAO"
        ).count()


class ReservationTestCase(ReservationMixin, APITestCase):
    pass


class ReleaseIfExpiredTests(ReservationTestCase):
    def test_libera_reserva_vencida_uma_vez_so(self):
        order = self.create_reserved_order()
        self.assert_stock(8)

        self.assertTrue(release_if_expired(order))
        self.assertFalse(release_if_expired(order))

        self.assertEqual(order.status, OrderStatus.CANCELED)
        self.assert_stock(10)
        self.assertEqual(self.returns(), 1)
        self.assertEqual(
            OrderStatusLog.objects.filter(
                order=order, new_status=OrderStatus.CANCELED
            ).count(),
            1,
        )

    def test_duas_requisicoes_com_o_mesmo_pedido_nao_dao_erro(self):
        """Cliente e admin abrem o pedido vencido ao mesmo tempo: as duas
        requisições carregaram o pedido antes de qualquer uma liberar."""
        order = self.create_reserved_order()
        first = CustomerOrder.objects.get(pk=order.pk)
        second = CustomerOrder.objects.get(pk=order.pk)

        self.assertTrue(release_if_expired(first))
        self.assertFalse(release_if_expired(second))

        self.assertEqual(second.status, OrderStatus.CANCELED)
        self.assertEqual(self.returns(), 1)
        self.assert_stock(10)

    def test_instancia_desatualizada_de_pedido_pago_nao_e_cancelada(self):
        order = self.create_reserved_order()
        stale = CustomerOrder.objects.get(pk=order.pk)
        update_status(order, OrderStatus.PAID)

        self.assertFalse(release_if_expired(stale))

        self.assertEqual(stale.status, OrderStatus.PAID)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAID)
        self.assert_stock(8)

    def test_pagamento_confirmado_impede_o_cancelamento(self):
        order = self.create_reserved_order()
        Payment.objects.filter(order=order).update(status=PaymentStatus.PAID)

        self.assertFalse(release_if_expired(order))

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.AWAITING_PAYMENT)
        self.assert_stock(8)
        self.assertEqual(self.returns(), 0)

    def test_pedido_ja_cancelado_nao_gera_nova_devolucao(self):
        order = self.create_reserved_order()
        update_status(order, OrderStatus.CANCELED)
        self.assertEqual(self.returns(), 1)

        self.assertFalse(release_if_expired(order))

        self.assertEqual(self.returns(), 1)
        self.assert_stock(10)

    def test_reserva_dentro_do_prazo_nao_e_liberada(self):
        order = self.create_reserved_order(expires_in=timedelta(minutes=10))

        self.assertFalse(release_if_expired(order))

        self.assertEqual(order.status, OrderStatus.AWAITING_PAYMENT)
        self.assert_stock(8)

    def test_cancelamento_marca_o_pagamento_como_falho(self):
        order = self.create_reserved_order()

        release_if_expired(order)

        # A instância recebida é atualizada, inclusive o pagamento em cache.
        self.assertEqual(order.payment.status, PaymentStatus.FAILED)


@override_settings(STOCK_RESERVATION_GRACE_SECONDS=120)
class ReleaseExpiredReservationsTests(ReservationTestCase):
    def test_libera_so_o_que_venceu_alem_da_tolerancia(self):
        expired = self.create_reserved_order(expires_in=timedelta(minutes=-3))
        in_grace = self.create_reserved_order(expires_in=timedelta(seconds=-60))
        on_time = self.create_reserved_order(expires_in=timedelta(minutes=10))

        self.assertEqual(release_expired_reservations(), 1)

        statuses = dict(
            CustomerOrder.objects.filter(
                pk__in=[expired.pk, in_grace.pk, on_time.pk]
            ).values_list("pk", "status")
        )
        self.assertEqual(statuses[expired.pk], OrderStatus.CANCELED)
        self.assertEqual(statuses[in_grace.pk], OrderStatus.AWAITING_PAYMENT)
        self.assertEqual(statuses[on_time.pk], OrderStatus.AWAITING_PAYMENT)

    def test_modo_direcionado_libera_so_as_variacoes_pedidas(self):
        other = ProductVariation.objects.create(
            product=self.variation.product, size="G", sku="RESERVA-G", stock_quantity=10
        )
        mine = self.create_reserved_order()
        theirs = self.create_reserved_order(variation=other)

        released = release_expired_reservations(variation_ids=[self.variation.pk])

        self.assertEqual(released, 1)
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual(mine.status, OrderStatus.CANCELED)
        self.assertEqual(theirs.status, OrderStatus.AWAITING_PAYMENT)

    def test_lote_comeca_pelas_reservas_mais_antigas(self):
        newer = self.create_reserved_order(expires_in=timedelta(minutes=-5))
        older = self.create_reserved_order(expires_in=timedelta(minutes=-50))

        self.assertEqual(release_expired_reservations(batch_size=1), 1)

        newer.refresh_from_db()
        older.refresh_from_db()
        self.assertEqual(older.status, OrderStatus.CANCELED)
        self.assertEqual(newer.status, OrderStatus.AWAITING_PAYMENT)

    def test_falha_em_um_pedido_nao_impede_os_outros(self):
        broken = self.create_reserved_order(expires_in=timedelta(minutes=-50))
        ok = self.create_reserved_order(expires_in=timedelta(minutes=-5))

        def release(order):
            if order.pk == broken.pk:
                raise RuntimeError("falha simulada")
            return release_if_expired(order)

        with (
            patch("orders.expiration.release_if_expired", side_effect=release),
            self.assertLogs("orders.expiration", level="ERROR") as logs,
        ):
            released = release_expired_reservations()

        self.assertEqual(released, 1)
        ok.refresh_from_db()
        self.assertEqual(ok.status, OrderStatus.CANCELED)
        self.assertIn(str(broken.pk), logs.output[0])

    @patch("orders.services.check_payment_status")
    def test_pagamento_depois_da_liberacao_mantem_o_pedido_cancelado(self, check):
        order = self.create_reserved_order()
        release_expired_reservations()
        check.return_value = VERIFIED_PAYMENT

        confirm_infinitepay_payment(str(order.pk), "TRANS-TARDIO", "FATURA-TARDIA")

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELED)
        self.assertEqual(order.payment.status, PaymentStatus.PAID)
        self.assert_stock(10)


class ConcurrentReleaseAndWebhookTests(ReservationMixin, TransactionTestCase):
    """O webhook confirma o pagamento enquanto a liberação tenta cancelar.

    O webhook trava o pedido e só confirma depois que a liberação já
    começou. Com a trava, a liberação espera o commit, vê o pedido pago e não
    cancela; sem ela, cancelaria um pedido pago e devolveria o estoque.
    """

    # SQLite trava a tabela inteira; só faz sentido com locks de linha (Postgres).
    @skipUnlessDBFeature("has_select_for_update")
    @override_settings(STOCK_RESERVATION_GRACE_SECONDS=0)
    @patch("orders.services.check_payment_status", return_value=None)
    def test_liberacao_espera_o_webhook_e_nao_cancela_pedido_pago(self, check):
        check.return_value = VERIFIED_PAYMENT
        order = self.create_reserved_order()
        webhook_locked = threading.Event()
        release_started = threading.Event()
        real_update_status = services.update_status
        results = {}

        def update_status(order, new_status, *args, **kwargs):
            if new_status == OrderStatus.PAID:
                # O webhook já trava o pedido aqui; espera a liberação tentar.
                webhook_locked.set()
                release_started.wait(timeout=10)
                time.sleep(0.3)
            return real_update_status(order, new_status, *args, **kwargs)

        def webhook():
            try:
                confirm_infinitepay_payment(str(order.pk), "TRANS-1", "FATURA-1")
                results["webhook"] = "ok"
            except Exception as exc:  # noqa: BLE001 - o teste reporta o erro
                results["webhook"] = repr(exc)
            finally:
                connection.close()

        def release():
            try:
                webhook_locked.wait(timeout=10)
                release_started.set()
                results["released"] = release_expired_reservations()
            except Exception as exc:  # noqa: BLE001 - o teste reporta o erro
                results["released"] = repr(exc)
            finally:
                connection.close()

        # Sem a trava, o resultado final seria o mesmo, mas porque o
        # update_status recusa PAID -> CANCELED: a liberação lançaria erro.
        with (
            patch("orders.services.update_status", side_effect=update_status),
            self.assertNoLogs("orders.expiration", level="ERROR"),
        ):
            threads = [
                threading.Thread(target=webhook),
                threading.Thread(target=release),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        self.assertEqual(results, {"webhook": "ok", "released": 0})
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAID)
        self.assert_stock(8)
        self.assertEqual(self.returns(), 0)


CART_ITEMS_URL = "/api/orders/cart/items/"
CATALOG_URL = "/api/catalog/products/"
SHIPPING_SETTINGS = {
    "CORREIOS_REMETENTE_CEP": "70000000",
    "CORREIOS_CODIGO_SERVICO": "03220",
    "CORREIOS_PESO_PADRAO_GRAMAS": "300",
}


def correios_mocks():
    return (
        patch(
            "orders.services.fetch_shipping_price_by_service_and_ceps",
            return_value={"pcFinal": "15,00"},
        ),
        patch(
            "orders.services.fetch_shipping_deadline_by_service_and_ceps",
            return_value={"prazoEntrega": 3},
        ),
    )


@override_settings(**SHIPPING_SETTINGS)
class AutomaticReleaseTests(ReservationTestCase):
    """Uma reserva abandonada segura as 2 únicas unidades da variação."""

    def setUp(self):
        super().setUp()
        expiration._last_sweep = None
        ProductVariation.objects.filter(pk=self.variation.pk).update(stock_quantity=2)
        self.buyer = User.objects.create_user(email="comprador@shio.com", name="B")
        self.client.force_authenticate(user=self.buyer)

    def buyer_cart(self):
        cart = Cart.objects.create(user=self.buyer, status="ACTIVE")
        CartItem.objects.create(
            cart=cart, variation=self.variation, quantity=1, unit_price=100
        )
        return Address.objects.create(
            user=self.buyer,
            zip_code="71000000",
            street="Rua",
            address_number="1",
            neighborhood="Centro",
            city="Brasília",
            state="DF",
        )

    def quote(self, address):
        price, deadline = correios_mocks()
        with price, deadline:
            return self.client.post(
                "/api/orders/checkout/calculate/",
                {"address_id": str(address.pk)},
                format="json",
            )

    def test_reserva_abandonada_nao_bloqueia_quem_adiciona_ao_carrinho(self):
        abandoned = self.create_reserved_order()
        self.assert_stock(0)

        response = self.client.post(
            CART_ITEMS_URL,
            {"variation_id": str(self.variation.pk), "quantity": 1},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        abandoned.refresh_from_db()
        self.assertEqual(abandoned.status, OrderStatus.CANCELED)

    def test_reserva_dentro_do_prazo_continua_segurando_o_estoque(self):
        self.create_reserved_order(expires_in=timedelta(minutes=10))

        response = self.client.post(
            CART_ITEMS_URL,
            {"variation_id": str(self.variation.pk), "quantity": 1},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assert_stock(0)

    def test_produto_volta_ao_catalogo_sem_ninguem_abrir_o_pedido(self):
        self.create_reserved_order()
        product_id = str(self.variation.product_id)

        with patch("products.views.sweep_expired_reservations"):
            hidden = self.client.get(CATALOG_URL)
        visible = self.client.get(CATALOG_URL)

        ids = lambda response: [p["id"] for p in response.json()["results"]]  # noqa: E731
        self.assertNotIn(product_id, ids(hidden))
        self.assertIn(product_id, ids(visible))

    def test_cotacao_libera_antes_de_validar_o_estoque(self):
        address = self.buyer_cart()
        self.create_reserved_order()

        response = self.quote(address)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assert_stock(2)

    @patch(
        "orders.services.create_infinitepay_checkout",
        return_value="https://pay.example.com/mock",
    )
    def test_checkout_libera_antes_de_validar_o_estoque(self, _):
        address = self.buyer_cart()
        quote = self.quote(address)
        self.create_reserved_order()  # abandonada depois da cotação

        response = self.client.post(
            "/api/orders/checkout/",
            {
                "address_id": str(address.pk),
                "shipping_quote_id": quote.data["shipping_quote_id"],
                "idempotency_key": str(uuid.uuid4()),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = CustomerOrder.objects.get(user=self.buyer)
        self.assertIsNotNone(response.data["reservation_expires_at"])
        self.assertEqual(
            response.data["reservation_expires_at"], order.reservation_expires_at
        )

    def test_varredura_respeita_o_intervalo(self):
        self.assertEqual(expiration.sweep_expired_reservations(), 0)
        order = self.create_reserved_order()

        self.assertEqual(expiration.sweep_expired_reservations(), 0)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.AWAITING_PAYMENT)
        expiration._last_sweep = None
        self.assertEqual(expiration.sweep_expired_reservations(), 1)

    def test_falha_na_liberacao_nao_quebra_catalogo_nem_carrinho(self):
        with (
            patch(
                "orders.expiration.release_expired_reservations",
                side_effect=RuntimeError("banco fora"),
            ),
            self.assertLogs("orders.expiration", level="ERROR"),
        ):
            catalog = self.client.get(CATALOG_URL)
            cart = self.client.get("/api/orders/cart/")

        self.assertEqual(catalog.status_code, status.HTTP_200_OK)
        self.assertEqual(cart.status_code, status.HTTP_200_OK)

    def test_detalhe_do_pedido_mostra_o_prazo_da_reserva(self):
        order = self.create_reserved_order(expires_in=timedelta(minutes=10))
        self.client.force_authenticate(user=self.user)

        response = self.client.get(f"/api/orders/my-orders/{order.pk}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.json()["reservation_expires_at"])
