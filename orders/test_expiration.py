from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from orders.models import (
    CustomerOrder,
    OrderItem,
    OrderStatus,
    OrderStatusLog,
    Payment,
    PaymentStatus,
)
from orders.services import release_if_expired, update_status
from products.models import Category, Product, ProductVariation, StockMovement
from products.services import move_stock

User = get_user_model()


class ReservationTestCase(APITestCase):
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

    def create_reserved_order(self, *, expires_in=timedelta(minutes=-1)):
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
            variation=self.variation,
            quantity=2,
            unit_price=Decimal("100.00"),
            product_name="Jaqueta - M",
            sku_snapshot=self.variation.sku,
        )
        move_stock(
            variation=self.variation,
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
