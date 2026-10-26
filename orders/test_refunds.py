import datetime
import threading
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import connection
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from authentication.models import UserProfile, UserRole
from orders.dashboard_aggregates import dashboard_detail_aggregates
from orders.metrics import METRICS_TIMEZONE
from orders.models import (
    CustomerOrder,
    OrderItem,
    OrderStatus,
    OrderStatusLog,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Refund,
    RefundReason,
    RefundStatus,
)
from orders.refunds import (
    RefundNotAllowed,
    complete_refund,
    refund_eligibility,
    request_refund,
)
from orders.services import confirm_infinitepay_payment, update_status
from products.availability import get_drop_sold_quantity
from products.models import (
    Category,
    DropCampaign,
    Product,
    ProductVariation,
    StockMovement,
)
from products.services import move_stock

User = get_user_model()

VERIFIED_PAYMENT = {
    "success": True,
    "paid": True,
    "amount": 20000,
    "paid_amount": 20000,
    "installments": 1,
    "capture_method": "pix",
}


class RefundFixtures:
    """Pedidos de R$ 200,00 que vendem 2 de 100 unidades cada."""

    def setUp(self):
        self.customer = User.objects.create_user(email="cliente@shio.com", name="Ana")
        UserProfile.objects.create(user=self.customer, role=UserRole.CUSTOMER)
        self.admin = User.objects.create_user(
            email="admin@shio.com", name="Admin", is_staff=True
        )
        UserProfile.objects.create(user=self.admin, role=UserRole.ADMIN)
        self.drop = DropCampaign.objects.create(
            name="Drop", slug="drop-reembolso", is_active=True, max_quantity=2
        )
        category = Category.objects.create(name="Reembolso", slug="reembolso")
        product = Product.objects.create(
            name="Jaqueta",
            description="x",
            base_price=100,
            category=category,
            drop=self.drop,
        )
        self.variation = ProductVariation.objects.create(
            product=product, size="M", sku="REEMBOLSO-M", stock_quantity=100
        )

    def create_order(
        self,
        order_status=OrderStatus.PAID,
        payment_status=PaymentStatus.PAID,
        *,
        user=None,
        delivered_at=None,
    ):
        user = user or self.customer
        order = CustomerOrder.objects.create(
            user=user,
            status=order_status,
            subtotal=Decimal("200.00"),
            total_amount=Decimal("200.00"),
            shipping_zip_code="70000000",
            shipping_street="Rua",
            shipping_number="1",
            shipping_neighborhood="Centro",
            shipping_city="Brasília",
            shipping_state="DF",
            tracking_code=(
                "TRACK"
                if order_status in (OrderStatus.SHIPPED, OrderStatus.DELIVERED)
                else None
            ),
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
            created_by=user,
        )
        Payment.objects.create(
            order=order,
            total_amount=order.total_amount,
            status=payment_status,
            method=PaymentMethod.PIX,
            gateway_transaction_id=f"TRANS-{order.pk}",
            gateway_invoice_slug="FATURA-1",
        )
        if order_status in (OrderStatus.SHIPPED, OrderStatus.DELIVERED):
            OrderStatusLog.objects.create(
                order=order,
                previous_status=OrderStatus.PREPARING,
                new_status=OrderStatus.SHIPPED,
            )
        if order_status == OrderStatus.DELIVERED:
            log = OrderStatusLog.objects.create(
                order=order,
                previous_status=OrderStatus.SHIPPED,
                new_status=OrderStatus.DELIVERED,
            )
            OrderStatusLog.objects.filter(pk=log.pk).update(
                created_at=delivered_at or timezone.now()
            )
        return order

    def assert_stock(self, quantity):
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, quantity)

    def request_url(self, order):
        return f"/api/orders/my-orders/{order.pk}/refund-request/"

    def admin_url(self, order, action=""):
        return f"/api/orders/admin/{order.pk}/refund/{action}"

    def completion(self, **overrides):
        return {
            "gateway_reference": "Estorno 123 no app",
            "refunded_at": timezone.now().isoformat(),
            **overrides,
        }

    def as_customer(self):
        self.client.force_authenticate(self.customer)

    def as_admin(self):
        self.client.force_authenticate(User.objects.get(pk=self.admin.pk))

    def ask_refund(self, order, reason=RefundReason.CUSTOMER_REQUEST, comment=""):
        return request_refund(order, self.customer, reason, comment)


class RefundRequestTests(RefundFixtures, APITestCase):
    def test_cliente_solicita_reembolso_de_pedido_pago(self):
        order = self.create_order()
        self.as_customer()

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.request_url(order),
                {"reason": RefundReason.CUSTOMER_REQUEST, "comment": " Desisti "},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        refund = Refund.objects.get(order=order)
        self.assertEqual(refund.status, RefundStatus.REQUESTED)
        self.assertEqual(refund.amount, Decimal("200.00"))
        self.assertEqual(refund.customer_comment, "Desisti")
        self.assertEqual(refund.requested_by, self.customer)
        self.assertEqual(response.data["refund"]["status"], RefundStatus.REQUESTED)
        self.assertEqual(
            response.data["refund_eligibility"]["code"], "refund_in_progress"
        )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAID)
        self.assert_stock(98)
        self.assertEqual(
            sorted(message.to[0] for message in mail.outbox),
            ["admin@shio.com", "cliente@shio.com"],
        )

    def test_elegibilidade_por_status_do_pedido(self):
        cases = [
            (OrderStatus.AWAITING_PAYMENT, PaymentStatus.PENDING, False, None),
            (OrderStatus.PAID, PaymentStatus.PAID, True, None),
            (OrderStatus.PREPARING, PaymentStatus.PAID, True, None),
            (OrderStatus.CANCELED, PaymentStatus.PAID, True, None),
            (OrderStatus.CANCELED, PaymentStatus.FAILED, False, None),
            (OrderStatus.SHIPPED, PaymentStatus.PAID, False, "order_in_transit"),
            (OrderStatus.DELIVERED, PaymentStatus.PAID, True, None),
            (OrderStatus.DELIVERED, PaymentStatus.REFUNDED, False, None),
        ]
        for order_status, payment_status, allowed, code in cases:
            with self.subTest(order_status=order_status, payment=payment_status):
                order = self.create_order(order_status, payment_status)
                eligibility = refund_eligibility(order)
                self.assertEqual(eligibility.can_request, allowed)
                if code:
                    self.assertEqual(eligibility.code, code)

        self.assertEqual(
            refund_eligibility(
                self.create_order(OrderStatus.AWAITING_PAYMENT, PaymentStatus.PENDING)
            ).code,
            "payment_not_confirmed",
        )
        self.assertEqual(
            refund_eligibility(
                self.create_order(OrderStatus.DELIVERED, PaymentStatus.REFUNDED)
            ).code,
            "refund_in_progress",
        )

    def test_prazo_termina_no_fim_do_setimo_dia_local(self):
        delivered = datetime.datetime(2026, 10, 10, 15, 0, tzinfo=METRICS_TIMEZONE)
        order = self.create_order(OrderStatus.DELIVERED, delivered_at=delivered)

        last_second = datetime.datetime(
            2026, 10, 17, 23, 59, 59, tzinfo=METRICS_TIMEZONE
        )
        inside = refund_eligibility(order, now=last_second)
        self.assertTrue(inside.can_request)
        self.assertEqual(inside.deadline, last_second)

        outside = refund_eligibility(
            order,
            now=datetime.datetime(2026, 10, 18, 0, 0, tzinfo=METRICS_TIMEZONE),
        )
        self.assertFalse(outside.can_request)
        self.assertEqual(outside.code, "deadline_passed")

    def test_prazo_vencido_e_entrega_sem_data_sao_recusados_pela_api(self):
        old = self.create_order(
            OrderStatus.DELIVERED,
            delivered_at=timezone.now() - datetime.timedelta(days=9),
        )
        unknown = self.create_order(OrderStatus.DELIVERED)
        OrderStatusLog.objects.filter(order=unknown).delete()
        self.as_customer()

        for order, code in (
            (old, "deadline_passed"),
            (unknown, "delivery_date_unknown"),
        ):
            with self.subTest(code=code):
                response = self.client.post(
                    self.request_url(order),
                    {"reason": RefundReason.CUSTOMER_REQUEST},
                    format="json",
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.data["code"], code)
        self.assertFalse(Refund.objects.exists())

    def test_validacao_do_motivo_e_do_comentario(self):
        order = self.create_order()
        self.as_customer()
        cases = [
            {"reason": RefundReason.OTHER, "comment": "  "},
            {"reason": RefundReason.CUSTOMER_REQUEST, "comment": "x" * 1001},
            {"reason": RefundReason.DELIVERY_ISSUE},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                response = self.client.post(
                    self.request_url(order), payload, format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Refund.objects.exists())

    def test_pedido_cancelado_pago_grava_pagamento_apos_cancelamento(self):
        order = self.create_order(OrderStatus.CANCELED)
        refund = self.ask_refund(order, RefundReason.PRODUCT_ISSUE)
        self.assertEqual(refund.reason, RefundReason.PAID_AFTER_CANCELLATION)

    def test_pedido_de_outro_cliente_e_anonimo(self):
        other = User.objects.create_user(email="outro@shio.com", name="Outro")
        order = self.create_order(user=other)
        payload = {"reason": RefundReason.CUSTOMER_REQUEST}

        anonymous = self.client.post(self.request_url(order), payload, format="json")
        self.assertEqual(anonymous.status_code, status.HTTP_401_UNAUTHORIZED)

        self.as_customer()
        response = self.client.post(self.request_url(order), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_segunda_solicitacao_e_solicitacao_apos_recusa(self):
        order = self.create_order()
        self.as_customer()
        payload = {"reason": RefundReason.CUSTOMER_REQUEST}
        self.client.post(self.request_url(order), payload, format="json")

        again = self.client.post(self.request_url(order), payload, format="json")
        self.assertEqual(again.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(again.data["code"], "refund_in_progress")

        self.as_admin()
        self.client.post(
            self.admin_url(order, "reject/"), {"message": "Fora da política"}
        )
        self.as_customer()
        after_rejection = self.client.post(
            self.request_url(order), payload, format="json"
        )
        self.assertEqual(after_rejection.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(after_rejection.data["code"], "refund_rejected")

    def test_detalhe_do_cliente_nao_expoe_dados_internos(self):
        order = self.create_order(OrderStatus.DELIVERED)
        self.ask_refund(order)
        complete_refund(
            order,
            self.admin,
            gateway_reference="REF-SECRETA",
            refunded_at=timezone.now(),
            internal_note="Nota interna",
        )
        self.as_customer()

        response = self.client.get(f"/api/orders/my-orders/{order.pk}/")

        refund = response.data["refund"]
        self.assertEqual(refund["status"], RefundStatus.COMPLETED)
        self.assertEqual(refund["amount"], "200.00")
        for field in (
            "internal_note",
            "gateway_reference",
            "requested_by",
            "completed_by",
        ):
            self.assertNotIn(field, refund)
        self.assertNotIn("REF-SECRETA", str(response.content))

    def test_lista_do_cliente_traz_status_do_reembolso(self):
        order = self.create_order()
        self.create_order()
        self.ask_refund(order)
        self.as_customer()

        response = self.client.get("/api/orders/my-orders/")

        statuses = {row["id"]: row["refund_status"] for row in response.data}
        self.assertEqual(statuses[str(order.pk)], RefundStatus.REQUESTED)
        self.assertEqual(list(statuses.values()).count(None), 1)


class RefundReviewTests(RefundFixtures, APITestCase):
    def test_aprovar_pedido_nao_enviado_cancela_e_devolve_estoque(self):
        order = self.create_order(OrderStatus.PREPARING)
        self.ask_refund(order)
        self.as_admin()

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.admin_url(order, "approve/"), {"message": "Tudo certo"}
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELED)
        self.assertEqual(order.payment.status, PaymentStatus.PAID)
        self.assert_stock(100)
        refund = order.refunds.get()
        self.assertEqual(refund.status, RefundStatus.APPROVED)
        self.assertEqual(refund.review_message, "Tudo certo")
        self.assertEqual(refund.reviewed_by, self.admin)
        log = order.status_logs.get(new_status=OrderStatus.CANCELED)
        self.assertTrue(log.comment.startswith("Reembolso"))
        self.assertEqual(response.data["refund"]["status"], RefundStatus.APPROVED)
        self.assertEqual(
            [message.to for message in mail.outbox], [["cliente@shio.com"]]
        )

    def test_aprovar_pedido_entregue_nao_altera_pedido(self):
        order = self.create_order(OrderStatus.DELIVERED)
        self.ask_refund(order)
        before = dashboard_detail_aggregates({})["financial"]
        self.as_admin()

        self.client.post(self.admin_url(order, "approve/"))

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.DELIVERED)
        self.assert_stock(98)
        self.assertEqual(dashboard_detail_aggregates({})["financial"], before)

    def test_recusar_exige_mensagem_e_libera_o_envio(self):
        order = self.create_order(OrderStatus.PREPARING)
        self.ask_refund(order)
        self.as_admin()

        blank = self.client.post(self.admin_url(order, "reject/"), {"message": ""})
        self.assertEqual(blank.status_code, status.HTTP_400_BAD_REQUEST)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.admin_url(order, "reject/"), {"message": "Produto usado"}
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        refund = order.refunds.get()
        self.assertEqual(refund.status, RefundStatus.REJECTED)
        self.assertEqual(refund.review_message, "Produto usado")
        self.assertEqual(len(mail.outbox), 1)
        update_status(order, OrderStatus.SHIPPED, tracking_code="TRACK")
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.SHIPPED)

    def test_aprovar_ou_recusar_sem_pedido_em_analise(self):
        order = self.create_order()
        self.as_admin()
        for action, payload in (("approve/", {}), ("reject/", {"message": "x"})):
            with self.subTest(action=action):
                response = self.client.post(self.admin_url(order, action), payload)
                self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
                self.assertEqual(response.data["code"], "no_pending_request")

    @patch("orders.views.dispatch_order_and_get_tracking_code")
    def test_pedido_solicitado_nao_pode_ser_enviado(self, dispatch):
        order = self.create_order(OrderStatus.PREPARING)
        self.ask_refund(order)
        self.as_admin()

        dispatched = self.client.post(f"/api/orders/correios/{order.pk}/despachar/")
        self.assertEqual(dispatched.status_code, status.HTTP_400_BAD_REQUEST)
        dispatch.assert_not_called()

        patched = self.client.patch(
            f"/api/orders/admin/{order.pk}/",
            {"status": OrderStatus.SHIPPED, "tracking_code": "TRACK"},
            format="json",
        )
        self.assertEqual(patched.status_code, status.HTTP_400_BAD_REQUEST)

        tracked = self.client.patch(
            f"/api/orders/correios/{order.pk}/tracking/",
            {"tracking_code": "TRACK"},
            format="json",
        )
        self.assertEqual(tracked.status_code, status.HTTP_400_BAD_REQUEST)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PREPARING)

    def test_lista_admin_filtra_por_status_do_reembolso(self):
        requested = self.create_order()
        self.create_order()
        self.ask_refund(requested)
        self.as_admin()

        response = self.client.get(
            "/api/orders/admin/", {"refund_status": RefundStatus.REQUESTED}
        )
        self.assertEqual([row["id"] for row in response.data], [str(requested.pk)])

        invalid = self.client.get("/api/orders/admin/", {"refund_status": "X"})
        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)


class RefundCompletionTests(RefundFixtures, APITestCase):
    def test_registro_de_pedido_solicitado_antes_do_envio(self):
        order = self.create_order(OrderStatus.PREPARING)
        self.ask_refund(order)
        self.as_admin()

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.admin_url(order), self.completion(), format="json"
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELED)
        self.assertEqual(order.payment.status, PaymentStatus.REFUNDED)
        self.assert_stock(100)
        refund = order.refunds.get()
        self.assertEqual(refund.status, RefundStatus.COMPLETED)
        self.assertEqual(refund.reviewed_by, self.admin)
        self.assertEqual(refund.completed_by, self.admin)
        self.assertEqual(refund.gateway_reference, "Estorno 123 no app")
        self.assertTrue(response.data["refund"]["stock_returned"])
        self.assertEqual(len(mail.outbox), 1)

    def test_registro_de_pedido_entregue_com_e_sem_retorno(self):
        for returned in (False, True):
            with self.subTest(items_returned=returned):
                order = self.create_order(OrderStatus.DELIVERED)
                self.ask_refund(order)
                stock_before = ProductVariation.objects.get(
                    pk=self.variation.pk
                ).stock_quantity
                self.as_admin()
                self.client.post(self.admin_url(order, "approve/"))

                response = self.client.post(
                    self.admin_url(order),
                    self.completion(items_returned=returned),
                    format="json",
                )

                self.assertEqual(response.status_code, status.HTTP_200_OK)
                order.refresh_from_db()
                self.assertEqual(order.status, OrderStatus.DELIVERED)
                self.assertEqual(order.payment.status, PaymentStatus.REFUNDED)
                self.assert_stock(stock_before + (2 if returned else 0))
                self.assertEqual(response.data["refund"]["stock_returned"], returned)

    def test_registro_direto_de_pedido_enviado_exige_retorno(self):
        order = self.create_order(OrderStatus.SHIPPED)
        self.as_admin()
        payload = self.completion(reason=RefundReason.DELIVERY_ISSUE)

        refused = self.client.post(self.admin_url(order), payload, format="json")
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("items_returned", refused.data)
        self.assertFalse(Refund.objects.exists())

        response = self.client.post(
            self.admin_url(order), {**payload, "items_returned": True}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELED)
        self.assertEqual(order.payment.status, PaymentStatus.REFUNDED)
        self.assert_stock(100)
        self.assertTrue(
            StockMovement.objects.filter(
                reason="DEVOLUCAO", note="Retorno físico confirmado"
            ).exists()
        )

    def test_registro_direto_de_pedido_cancelado_pago(self):
        order = self.create_order(OrderStatus.CANCELED)
        self.as_admin()

        response = self.client.post(
            self.admin_url(order),
            self.completion(reason=RefundReason.PAID_AFTER_CANCELLATION),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        refund = order.refunds.get()
        self.assertEqual(refund.requested_by, self.admin)
        self.assertEqual(refund.status, RefundStatus.COMPLETED)

    def test_validacoes_do_registro(self):
        order = self.create_order(OrderStatus.DELIVERED)
        self.as_admin()
        paid_at = order.payment.paid_at
        cases = {
            "sem motivo": self.completion(),
            "outro sem nota": self.completion(reason=RefundReason.OTHER),
            "motivo de cancelado": self.completion(
                reason=RefundReason.PAID_AFTER_CANCELLATION
            ),
            "data futura": self.completion(
                reason=RefundReason.CUSTOMER_REQUEST,
                refunded_at=(timezone.now() + datetime.timedelta(hours=1)).isoformat(),
            ),
            "antes do pagamento": self.completion(
                reason=RefundReason.CUSTOMER_REQUEST,
                refunded_at=(paid_at - datetime.timedelta(minutes=1)).isoformat(),
            ),
            "sem referência": self.completion(
                reason=RefundReason.CUSTOMER_REQUEST, gateway_reference=""
            ),
        }
        for name, payload in cases.items():
            with self.subTest(name):
                response = self.client.post(
                    self.admin_url(order), payload, format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        order.refresh_from_db()
        self.assertEqual(order.payment.status, PaymentStatus.PAID)
        self.assertFalse(Refund.objects.exists())

    def test_pagamento_nao_confirmado_e_segundo_registro(self):
        pending = self.create_order(OrderStatus.AWAITING_PAYMENT, PaymentStatus.PENDING)
        order = self.create_order(OrderStatus.DELIVERED)
        self.as_admin()
        payload = self.completion(reason=RefundReason.CUSTOMER_REQUEST)

        refused = self.client.post(self.admin_url(pending), payload, format="json")
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(refused.data["code"], "payment_not_confirmed")

        first = self.client.post(self.admin_url(order), payload, format="json")
        second = self.client.post(self.admin_url(order), payload, format="json")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(order.refunds.count(), 1)

    def test_falha_em_um_passo_desfaz_tudo(self):
        order = self.create_order(OrderStatus.DELIVERED)
        self.ask_refund(order)

        with (
            patch("orders.refunds.restore_order_stock", side_effect=RuntimeError("x")),
            self.captureOnCommitCallbacks(execute=True),
            self.assertRaises(RuntimeError),
        ):
            complete_refund(
                order,
                self.admin,
                gateway_reference="REF",
                refunded_at=timezone.now(),
                items_returned=True,
            )

        order.refresh_from_db()
        self.assertEqual(order.payment.status, PaymentStatus.PAID)
        self.assertEqual(order.refunds.get().status, RefundStatus.REQUESTED)
        self.assertEqual(mail.outbox, [])

    @patch("orders.services.check_payment_status")
    def test_webhook_repetido_mantem_reembolso(self, check):
        order = self.create_order(OrderStatus.DELIVERED)
        complete_refund(
            order,
            self.admin,
            gateway_reference="REF",
            refunded_at=timezone.now(),
            reason=RefundReason.CUSTOMER_REQUEST,
        )
        check.return_value = {
            **VERIFIED_PAYMENT,
            "order_nsu": str(order.pk),
        }

        confirm_infinitepay_payment(str(order.pk), f"TRANS-{order.pk}", "FATURA-1")

        order.refresh_from_db()
        self.assertEqual(order.payment.status, PaymentStatus.REFUNDED)

    def test_reembolso_libera_a_cota_do_drop(self):
        order = self.create_order(OrderStatus.DELIVERED)
        self.assertEqual(get_drop_sold_quantity(self.drop), 2)

        complete_refund(
            order,
            self.admin,
            gateway_reference="REF",
            refunded_at=timezone.now(),
            items_returned=True,
            reason=RefundReason.CUSTOMER_REQUEST,
        )

        self.assertEqual(get_drop_sold_quantity(self.drop), 0)

    def test_falha_no_envio_do_email_nao_desfaz_o_reembolso(self):
        order = self.create_order()
        with (
            patch(
                "django.core.mail.EmailMultiAlternatives.send",
                side_effect=OSError("smtp"),
            ),
            self.assertLogs("notifications.email", level="ERROR"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.ask_refund(order)
        self.assertEqual(order.refunds.get().status, RefundStatus.REQUESTED)


class RefundPermissionTests(RefundFixtures, APITestCase):
    def test_rotas_administrativas_exigem_manage_orders(self):
        order = self.create_order()
        no_permission = User.objects.create_user(
            email="semperm@shio.com", name="Sem", is_staff=True
        )
        no_permission.user_permissions.clear()
        urls = [
            self.admin_url(order),
            self.admin_url(order, "approve/"),
            self.admin_url(order, "reject/"),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.client.force_authenticate(None)
                self.assertEqual(
                    self.client.post(url).status_code, status.HTTP_401_UNAUTHORIZED
                )
                self.client.force_authenticate(User.objects.get(pk=no_permission.pk))
                self.assertEqual(
                    self.client.post(url).status_code, status.HTTP_403_FORBIDDEN
                )

    def test_solicitacao_avisa_somente_quem_gerencia_pedidos(self):
        no_permission = User.objects.create_user(
            email="semperm@shio.com", name="Sem", is_staff=True
        )
        no_permission.user_permissions.clear()
        order = self.create_order()

        with self.captureOnCommitCallbacks(execute=True):
            self.ask_refund(order)

        recipients = sorted(message.to[0] for message in mail.outbox)
        self.assertEqual(recipients, ["admin@shio.com", "cliente@shio.com"])

    def test_patch_nao_cancela_pedido_pago_nem_repoe_estoque(self):
        paid = self.create_order()
        shipped = self.create_order(OrderStatus.SHIPPED)
        self.as_admin()

        canceled = self.client.patch(
            f"/api/orders/admin/{paid.pk}/",
            {"status": OrderStatus.CANCELED},
            format="json",
        )
        self.assertEqual(canceled.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("reembolso", canceled.data["message"])

        self.client.patch(
            f"/api/orders/admin/{shipped.pk}/",
            {
                "status": OrderStatus.SHIPPED,
                "tracking_code": "TRACK",
                "physical_return_confirmed": True,
            },
            format="json",
        )
        self.assert_stock(96)
        self.assertFalse(StockMovement.objects.filter(reason="DEVOLUCAO").exists())


class ConcurrentRefundTests(RefundFixtures, TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_registros_simultaneos_resultam_em_um_reembolso(self):
        order = self.create_order(OrderStatus.DELIVERED)
        start = threading.Barrier(2)
        results = []

        def register():
            try:
                start.wait(timeout=10)
                complete_refund(
                    order,
                    self.admin,
                    gateway_reference="REF",
                    refunded_at=timezone.now(),
                    reason=RefundReason.CUSTOMER_REQUEST,
                )
                results.append("ok")
            except RefundNotAllowed as exc:
                results.append(exc.detail["code"])
            finally:
                connection.close()

        threads = [threading.Thread(target=register) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(sorted(results), ["ok", "refund_in_progress"])
        self.assertEqual(order.refunds.count(), 1)

    @skipUnlessDBFeature("has_select_for_update")
    def test_solicitacao_e_envio_simultaneos_tem_um_vencedor(self):
        order = self.create_order(OrderStatus.PREPARING)
        start = threading.Barrier(2)
        results = {}

        def ask():
            try:
                start.wait(timeout=10)
                self.ask_refund(order)
                results["refund"] = "ok"
            except RefundNotAllowed as exc:
                results["refund"] = exc.detail["code"]
            finally:
                connection.close()

        def ship():
            try:
                start.wait(timeout=10)
                update_status(order, OrderStatus.SHIPPED, tracking_code="TRACK")
                results["ship"] = "ok"
            except Exception as exc:  # noqa: BLE001 - o teste reporta o erro
                results["ship"] = type(exc).__name__
            finally:
                connection.close()

        threads = [threading.Thread(target=ask), threading.Thread(target=ship)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        order.refresh_from_db()
        if order.status == OrderStatus.SHIPPED:
            self.assertEqual(results, {"ship": "ok", "refund": "order_in_transit"})
            self.assertFalse(order.refunds.exists())
        else:
            self.assertEqual(results, {"refund": "ok", "ship": "ValidationError"})
            self.assertEqual(order.refunds.get().status, RefundStatus.REQUESTED)
