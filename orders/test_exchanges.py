import datetime
import threading
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import connection
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from authentication.models import UserProfile, UserRole
from orders.dashboard_aggregates import dashboard_detail_aggregates
from orders.exchanges import (
    ExchangeNotAllowed,
    approve_exchange,
    cancel_exchange,
    complete_exchange,
    deadline_after,
    exchange_eligibility,
    receive_exchange,
    reject_exchange,
    request_exchange,
)
from orders.metrics import METRICS_TIMEZONE
from orders.models import (
    ExchangeItemCondition,
    ExchangeReason,
    ExchangeRequest,
    ExchangeStatus,
    OrderStatus,
    PaymentStatus,
    Refund,
    RefundReason,
    RefundStatus,
)
from orders.refunds import RefundNotAllowed, complete_refund, refund_eligibility
from orders.test_refunds import RefundFixtures
from products.models import Product, ProductVariation, StockMovement
from products.services import move_stock

User = get_user_model()


class ExchangeFixtures(RefundFixtures):
    """Pedido de 2 jaquetas M; a troca é por G (estoque 5) ou P (sem estoque)."""

    def setUp(self):
        super().setUp()
        product = self.variation.product
        self.large = self.add_variation(product, "G", 5)
        self.small = self.add_variation(product, "P", 0)
        self.other_product = Product.objects.create(
            name="Calça", description="x", base_price=100, category=product.category
        )
        self.other = self.add_variation(self.other_product, "M", 5)

    def add_variation(self, product, size, quantity):
        variation = ProductVariation.objects.create(
            product=product, size=size, sku=f"{product.name}-{size}".upper()
        )
        if quantity:
            move_stock(
                variation=variation,
                kind="ENTRADA",
                reason="COMPRA",
                quantity=quantity,
                origin_type="MANUAL_ADJUSTMENT",
                origin_id=variation.pk,
                idempotency_key=f"compra:{variation.pk}",
                created_by=self.admin,
                note="Compra",
            )
        return variation

    def delivered(self, days_ago=3, **kwargs):
        return self.create_order(
            OrderStatus.DELIVERED,
            delivered_at=timezone.now() - datetime.timedelta(days=days_ago),
            **kwargs,
        )

    def row(self, order, replacement=None, quantity=1):
        return {
            "order_item_id": order.items.get().pk,
            "replacement_variation_id": (replacement or self.large).pk,
            "quantity": quantity,
        }

    def ask(self, order, reason=ExchangeReason.SIZE_OR_COLOR, **row):
        return request_exchange(
            order, self.customer, reason, [self.row(order, **row)], "Ficou grande"
        )

    def stock(self, variation):
        variation.refresh_from_db()
        return variation.stock_quantity

    def movements(self, order):
        return list(
            StockMovement.objects.filter(origin_type="EXCHANGE")
            .filter(
                origin_id__in=list(order.exchanges.values_list("items__id", flat=True))
            )
            .order_by("created_at", "sequence")
            .values_list("variation__size", "kind", "reason", "quantity")
        )

    def request_url(self, order):
        return f"/api/orders/my-orders/{order.pk}/exchange-request/"

    def exchange_url(self, order, action):
        return f"/api/orders/admin/{order.pk}/exchange/{action}/"

    def payload(self, order, reason=ExchangeReason.SIZE_OR_COLOR, **row):
        return {
            "reason": reason,
            "comment": "Ficou grande",
            "items": [
                {key: str(value) for key, value in self.row(order, **row).items()}
            ],
        }


class ExchangeEligibilityTests(ExchangeFixtures, TestCase):
    def test_elegibilidade_por_status_do_pedido_e_do_pagamento(self):
        cases = [
            (OrderStatus.PAID, PaymentStatus.PAID, "order_not_delivered"),
            (OrderStatus.PREPARING, PaymentStatus.PAID, "order_not_delivered"),
            (OrderStatus.SHIPPED, PaymentStatus.PAID, "order_not_delivered"),
            (OrderStatus.CANCELED, PaymentStatus.PAID, "order_not_delivered"),
            (OrderStatus.DELIVERED, PaymentStatus.PENDING, "payment_not_confirmed"),
            (OrderStatus.DELIVERED, PaymentStatus.REFUNDED, "refund_exists"),
            (OrderStatus.DELIVERED, PaymentStatus.PAID, None),
        ]
        for order_status, payment_status, code in cases:
            with self.subTest(order=order_status, payment=payment_status):
                order = self.create_order(order_status, payment_status)
                eligibility = exchange_eligibility(order)
                self.assertEqual(eligibility.code, code)
                self.assertEqual(eligibility.can_request, code is None)

    def test_prazos_terminam_no_fim_do_30o_e_do_90o_dia_local(self):
        order = self.delivered()
        delivered = order.status_logs.get(new_status=OrderStatus.DELIVERED).created_at
        local_day = delivered.astimezone(METRICS_TIMEZONE).date()
        eligibility = exchange_eligibility(order)

        self.assertEqual(
            eligibility.size_or_color_deadline.date(),
            local_day + datetime.timedelta(days=30),
        )
        self.assertEqual(
            eligibility.defect_deadline.date(), local_day + datetime.timedelta(days=90)
        )
        self.assertEqual(
            eligibility.size_or_color_deadline, deadline_after(delivered, 30)
        )
        last_second = deadline_after(delivered, 90)
        self.assertTrue(exchange_eligibility(order, now=last_second).can_request)
        after = exchange_eligibility(
            order, now=last_second + datetime.timedelta(seconds=1)
        )
        self.assertEqual(after.code, "deadline_passed")

    def test_tamanho_ou_cor_so_ate_30_dias_e_defeito_ate_90(self):
        inside = self.delivered(days_ago=30)
        self.ask(inside)

        late = self.delivered(days_ago=31)
        with self.assertRaises(ExchangeNotAllowed) as ctx:
            self.ask(late)
        self.assertEqual(ctx.exception.detail["code"], "size_deadline_passed")
        self.ask(late, reason=ExchangeReason.DEFECT)

        expired = self.delivered(days_ago=91)
        with self.assertRaises(ExchangeNotAllowed) as ctx:
            self.ask(expired, reason=ExchangeReason.DEFECT)
        self.assertEqual(ctx.exception.detail["code"], "deadline_passed")

    def test_entrega_sem_data(self):
        order = self.delivered()
        order.status_logs.all().delete()
        self.assertEqual(exchange_eligibility(order).code, "delivery_date_unknown")

    def test_itens_quantidade_e_variacao_invalidos(self):
        order = self.delivered()
        other_order = self.delivered()
        foreign_item = other_order.items.get().pk
        cases = {
            "quantidade acima da comprada": [self.row(order, quantity=3)],
            "variação de outro produto": [self.row(order, replacement=self.other)],
            "mesma variação": [self.row(order, replacement=self.variation)],
            "item de outro pedido": [
                {**self.row(order), "order_item_id": foreign_item}
            ],
            "item repetido": [self.row(order), self.row(order)],
            "nenhum item": [],
        }
        for label, items in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValidationError):
                    request_exchange(
                        order, self.customer, ExchangeReason.SIZE_OR_COLOR, items
                    )
        self.assertFalse(order.exchanges.exists())

    def test_reposicao_sem_estoque_e_produto_inativo(self):
        order = self.delivered()
        with self.assertRaises(ExchangeNotAllowed) as ctx:
            self.ask(order, replacement=self.small)
        self.assertEqual(ctx.exception.detail["code"], "replacement_out_of_stock")
        self.assertEqual(ctx.exception.detail["variation_ids"], [str(self.small.pk)])

        # A soma das linhas conta: 2 unidades de G com 1 em estoque não passam.
        move_stock(
            variation=self.large,
            kind="SAIDA",
            reason="PERDA",
            quantity=4,
            origin_type="MANUAL_ADJUSTMENT",
            origin_id=self.large.pk,
            idempotency_key="perda-g",
            created_by=self.admin,
            note="Avaria",
        )
        with self.assertRaises(ExchangeNotAllowed):
            self.ask(order, quantity=2)

        Product.objects.filter(pk=self.variation.product_id).update(is_active=False)
        with self.assertRaises(ValidationError):
            self.ask(order)
        self.assertFalse(order.exchanges.exists())

    def test_nova_solicitacao_com_troca_aberta_recusada_ou_cancelada(self):
        order = self.delivered()
        exchange = self.ask(order)
        with self.assertRaises(ExchangeNotAllowed) as ctx:
            self.ask(order)
        self.assertEqual(ctx.exception.detail["code"], "exchange_in_progress")

        cancel_exchange(order, self.admin, "Cliente desistiu")
        self.ask(order)

        reject_exchange(order, self.admin, "Peça usada")
        with self.assertRaises(ExchangeNotAllowed) as ctx:
            self.ask(order)
        self.assertEqual(ctx.exception.detail["code"], "exchange_rejected")
        self.assertEqual(
            sorted(order.exchanges.values_list("status", flat=True)),
            [ExchangeStatus.CANCELED, ExchangeStatus.REJECTED],
        )
        exchange.refresh_from_db()
        self.assertEqual(exchange.status, ExchangeStatus.CANCELED)

    def test_troca_parcial_e_itens_ja_trocados(self):
        order = self.delivered()
        self.ask(order)
        approve_exchange(order, self.admin)
        item = order.exchanges.get().items.get()
        receive_exchange(order, self.admin, {item.pk: ExchangeItemCondition.RESTOCK})
        complete_exchange(order, self.admin, "BR1")

        eligibility = exchange_eligibility(order)
        self.assertTrue(eligibility.can_request)
        with self.assertRaises(ValidationError):
            self.ask(order, quantity=2)
        self.ask(order)
        approve_exchange(order, self.admin)
        item = order.exchanges.get(status=ExchangeStatus.APPROVED).items.get()
        receive_exchange(order, self.admin, {item.pk: ExchangeItemCondition.RESTOCK})
        complete_exchange(order, self.admin, "BR2")

        self.assertEqual(exchange_eligibility(order).code, "nothing_to_exchange")


class ExchangeRefundInteractionTests(ExchangeFixtures, TestCase):
    def test_reembolso_ativo_ou_concluido_impede_troca(self):
        for refund_status in (
            RefundStatus.REQUESTED,
            RefundStatus.APPROVED,
            RefundStatus.COMPLETED,
        ):
            with self.subTest(refund_status):
                order = self.delivered()
                Refund.objects.create(
                    order=order,
                    status=refund_status,
                    amount=order.total_amount,
                    reason=RefundReason.CUSTOMER_REQUEST,
                    gateway_reference="REF",
                    refunded_at=timezone.now(),
                )
                self.assertEqual(exchange_eligibility(order).code, "refund_exists")

        rejected = self.delivered()
        Refund.objects.create(
            order=rejected,
            status=RefundStatus.REJECTED,
            amount=rejected.total_amount,
            reason=RefundReason.CUSTOMER_REQUEST,
        )
        self.assertTrue(exchange_eligibility(rejected).can_request)

    def test_troca_aberta_ou_concluida_impede_reembolso(self):
        order = self.delivered()
        self.ask(order)
        self.assertEqual(refund_eligibility(order).code, "exchange_exists")
        with self.assertRaises(RefundNotAllowed) as ctx:
            complete_refund(
                order,
                self.admin,
                gateway_reference="REF",
                refunded_at=timezone.now(),
                reason=RefundReason.CUSTOMER_REQUEST,
            )
        self.assertEqual(ctx.exception.detail["code"], "exchange_exists")
        self.assertEqual(ctx.exception.status_code, status.HTTP_409_CONFLICT)

        reject_exchange(order, self.admin, "Fora da política")
        self.assertTrue(refund_eligibility(order).can_request)

    def test_troca_nao_altera_metricas(self):
        order = self.delivered()
        before = dashboard_detail_aggregates({})

        self.ask(order)
        approve_exchange(order, self.admin)
        item = order.exchanges.get().items.get()
        receive_exchange(order, self.admin, {item.pk: ExchangeItemCondition.LOSS})
        complete_exchange(order, self.admin, "BR1")

        after = dashboard_detail_aggregates({})
        for section in (
            "financial",
            "sales_series",
            "product_rankings",
            "orders_by_status",
            "customers",
        ):
            self.assertEqual(after.get(section), before.get(section), section)


class ExchangeStockTests(ExchangeFixtures, TestCase):
    def test_aprovar_reserva_a_reposicao(self):
        order = self.delivered()
        self.ask(order)
        self.assertEqual(self.stock(self.large), 5)

        approve_exchange(order, self.admin, "Envie pelos Correios")

        self.assertEqual(self.stock(self.large), 4)
        self.assertEqual(self.movements(order), [("G", "SAIDA", "TROCA", 1)])
        exchange = order.exchanges.get()
        self.assertEqual(exchange.status, ExchangeStatus.APPROVED)
        self.assertEqual(exchange.reviewed_by, self.admin)

    def test_recebida_para_estoque(self):
        order = self.delivered()
        self.ask(order)
        approve_exchange(order, self.admin)
        item = order.exchanges.get().items.get()

        receive_exchange(order, self.admin, {item.pk: ExchangeItemCondition.RESTOCK})

        self.assert_stock(99)
        self.assertEqual(
            self.movements(order),
            [("G", "SAIDA", "TROCA", 1), ("M", "ENTRADA", "DEVOLUCAO", 1)],
        )
        returned = StockMovement.objects.get(reason="DEVOLUCAO")
        self.assertEqual(returned.order_item, item.order_item)
        self.assertEqual(returned.created_by, self.admin)

    def test_recebida_como_perda(self):
        order = self.delivered()
        self.ask(order)
        approve_exchange(order, self.admin)
        item = order.exchanges.get().items.get()

        receive_exchange(order, self.admin, {item.pk: ExchangeItemCondition.LOSS})

        self.assert_stock(98)
        self.assertEqual(
            self.movements(order),
            [
                ("G", "SAIDA", "TROCA", 1),
                ("M", "ENTRADA", "DEVOLUCAO", 1),
                ("M", "SAIDA", "PERDA", 1),
            ],
        )
        item.refresh_from_db()
        self.assertEqual(item.condition, ExchangeItemCondition.LOSS)

    def test_cancelar_aprovada_devolve_a_reserva(self):
        order = self.delivered()
        self.ask(order)
        approve_exchange(order, self.admin)

        cancel_exchange(order, self.admin, "Reposição com defeito")

        self.assertEqual(self.stock(self.large), 5)
        self.assertEqual(
            self.movements(order),
            [("G", "SAIDA", "TROCA", 1), ("G", "ENTRADA", "TROCA", 1)],
        )
        release = StockMovement.objects.get(kind="ENTRADA", reason="TROCA")
        self.assertEqual(release.reverses_movement.kind, "SAIDA")

    def test_cancelar_solicitada_nao_movimenta_estoque(self):
        order = self.delivered()
        self.ask(order)
        cancel_exchange(order, self.admin, "Cliente desistiu")
        self.assertEqual(self.movements(order), [])

    def test_transicoes_fora_de_ordem_sao_recusadas(self):
        order = self.delivered()
        self.ask(order)
        item = order.exchanges.get().items.get()
        steps = [
            (lambda: receive_exchange(order, self.admin, {item.pk: "RESTOCK"}), 409),
            (lambda: complete_exchange(order, self.admin, "BR"), 409),
        ]
        for step, code in steps:
            with self.assertRaises(ExchangeNotAllowed) as ctx:
                step()
            self.assertEqual(ctx.exception.status_code, code)

        approve_exchange(order, self.admin)
        with self.assertRaises(ExchangeNotAllowed):
            approve_exchange(order, self.admin)
        with self.assertRaises(ExchangeNotAllowed):
            reject_exchange(order, self.admin, "x")
        with self.assertRaises(ValidationError):
            receive_exchange(order, self.admin, {})
        receive_exchange(order, self.admin, {item.pk: "RESTOCK"})
        with self.assertRaises(ExchangeNotAllowed) as ctx:
            cancel_exchange(order, self.admin, "x")
        self.assertEqual(ctx.exception.detail["code"], "not_cancelable")
        complete_exchange(order, self.admin, "BR")
        with self.assertRaises(ExchangeNotAllowed):
            complete_exchange(order, self.admin, "BR")
        self.assertEqual(len(self.movements(order)), 2)

    def test_aprovar_sem_estoque_nao_grava_nada(self):
        order = self.delivered()
        self.ask(order)
        move_stock(
            variation=self.large,
            kind="SAIDA",
            reason="PERDA",
            quantity=5,
            origin_type="MANUAL_ADJUSTMENT",
            origin_id=self.large.pk,
            idempotency_key="perda-g",
            created_by=self.admin,
            note="Avaria",
        )
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(ExchangeNotAllowed) as ctx:
                approve_exchange(order, self.admin)
        self.assertEqual(ctx.exception.detail["code"], "replacement_out_of_stock")
        self.assertEqual(order.exchanges.get().status, ExchangeStatus.REQUESTED)
        self.assertEqual(self.movements(order), [])
        self.assertEqual(mail.outbox, [])

    def test_falha_em_um_passo_desfaz_tudo_sem_email(self):
        order = self.delivered()
        self.ask(order)
        with (
            patch("orders.exchanges.move_stock", side_effect=RuntimeError("x")),
            self.captureOnCommitCallbacks(execute=True),
            self.assertRaises(RuntimeError),
        ):
            approve_exchange(order, self.admin)
        self.assertEqual(order.exchanges.get().status, ExchangeStatus.REQUESTED)

        approve_exchange(order, self.admin)
        item = order.exchanges.get().items.get()
        real = move_stock
        calls = []

        def fail_on_loss(**kwargs):
            calls.append(kwargs["reason"])
            if kwargs["reason"] == "PERDA":
                raise RuntimeError("x")
            return real(**kwargs)

        mail.outbox.clear()
        with (
            patch("orders.exchanges.move_stock", side_effect=fail_on_loss),
            self.captureOnCommitCallbacks(execute=True),
            self.assertRaises(RuntimeError),
        ):
            receive_exchange(order, self.admin, {item.pk: ExchangeItemCondition.LOSS})

        self.assertEqual(calls, ["DEVOLUCAO", "PERDA"])
        self.assertEqual(order.exchanges.get().status, ExchangeStatus.APPROVED)
        self.assertEqual(self.movements(order), [("G", "SAIDA", "TROCA", 1)])
        self.assert_stock(98)
        self.assertEqual(mail.outbox, [])


class ExchangeEmailTests(ExchangeFixtures, TestCase):
    def test_cliente_e_avisado_em_cada_etapa(self):
        order = self.delivered()
        with self.captureOnCommitCallbacks(execute=True):
            self.ask(order)
        with self.captureOnCommitCallbacks(execute=True):
            approve_exchange(order, self.admin, "Envie para a Rua X")
        item = order.exchanges.get().items.get()
        with self.captureOnCommitCallbacks(execute=True):
            receive_exchange(order, self.admin, {item.pk: "RESTOCK"})
        with self.captureOnCommitCallbacks(execute=True):
            complete_exchange(order, self.admin, "BR123")

        self.assertEqual(
            [(m.to[0], m.subject) for m in mail.outbox],
            [
                ("cliente@shio.com", "Recebemos seu pedido de troca"),
                ("admin@shio.com", "Novo pedido de troca"),
                ("cliente@shio.com", "Sua troca foi aprovada"),
                ("cliente@shio.com", "Recebemos a peça da sua troca"),
                ("cliente@shio.com", "A nova peça da sua troca foi enviada"),
            ],
        )
        self.assertIn("Envie para a Rua X", mail.outbox[2].body)
        self.assertIn("M -> G", mail.outbox[2].body)
        self.assertIn("BR123", mail.outbox[4].body)

    def test_recusa_e_cancelamento_levam_a_justificativa(self):
        order = self.delivered()
        self.ask(order)
        with self.captureOnCommitCallbacks(execute=True):
            cancel_exchange(order, self.admin, "Acabou a peça")
        self.ask(order)
        with self.captureOnCommitCallbacks(execute=True):
            reject_exchange(order, self.admin, "Peça usada")
        self.assertEqual(
            [m.subject for m in mail.outbox],
            ["Sua troca foi cancelada", "Sua troca foi recusada"],
        )
        self.assertIn("Acabou a peça", mail.outbox[0].body)
        self.assertIn("Peça usada", mail.outbox[1].body)

    def test_falha_no_envio_do_email_nao_desfaz_a_troca(self):
        order = self.delivered()
        with (
            patch(
                "django.core.mail.EmailMultiAlternatives.send",
                side_effect=OSError("smtp"),
            ),
            self.assertLogs("notifications.email", level="ERROR"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.ask(order)
        self.assertEqual(order.exchanges.get().status, ExchangeStatus.REQUESTED)


class ExchangeApiTests(ExchangeFixtures, APITestCase):
    def test_fluxo_completo_pela_api(self):
        order = self.delivered()
        self.as_customer()
        detail = self.client.get(f"/api/orders/my-orders/{order.pk}/").data
        options = detail["exchange_eligibility"]["items"][0]
        self.assertEqual(options["available_quantity"], 2)
        self.assertEqual(
            {(o["size"], o["in_stock"]) for o in options["options"]},
            {("G", True), ("P", False)},
        )

        response = self.client.post(
            self.request_url(order), self.payload(order), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["exchanges"][0]["status"], "REQUESTED")
        self.assertFalse(response.data["exchange_eligibility"]["can_request"])

        self.as_admin()
        for action, body in [
            ("approve", {"message": "Envie"}),
            ("receive", None),
            ("complete", {"tracking_code": "BR1"}),
        ]:
            if body is None:
                item = order.exchanges.get().items.get()
                body = {"items": [{"id": str(item.pk), "condition": "RESTOCK"}]}
            response = self.client.post(
                self.exchange_url(order, action), body, format="json"
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, action)

        exchange = response.data["exchanges"][0]
        self.assertEqual(exchange["status"], "COMPLETED")
        self.assertEqual(exchange["tracking_code"], "BR1")
        self.assertEqual(exchange["completed_by"]["email"], "admin@shio.com")

    def test_validacoes_de_entrada(self):
        order = self.delivered()
        self.as_customer()
        no_comment = {**self.payload(order, reason="DEFECT"), "comment": " "}
        cases = [
            no_comment,
            {**self.payload(order), "items": []},
            {**self.payload(order), "reason": "OUTRO"},
            self.payload(order, quantity=0),
        ]
        for body in cases:
            with self.subTest(body=body):
                response = self.client.post(
                    self.request_url(order), body, format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.ask(order)
        self.as_admin()
        cases = [
            ("reject", {"message": ""}),
            ("cancel", {}),
            ("complete", {"tracking_code": ""}),
        ]
        for action, body in cases:
            with self.subTest(action=action):
                response = self.client.post(
                    self.exchange_url(order, action), body, format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        approve_exchange(order, self.admin)
        item = str(order.exchanges.get().items.get().pk)
        duplicated = {
            "items": [
                {"id": item, "condition": "RESTOCK"},
                {"id": item, "condition": "LOSS"},
            ]
        }
        response = self.client.post(
            self.exchange_url(order, "receive"), duplicated, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_erros_de_regra_trazem_codigo(self):
        order = self.delivered(days_ago=40)
        self.as_customer()
        response = self.client.post(
            self.request_url(order), self.payload(order), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "size_deadline_passed")

        self.ask(order, reason=ExchangeReason.DEFECT)
        response = self.client.post(
            self.request_url(order),
            self.payload(order, reason="DEFECT"),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "exchange_in_progress")

    def test_pedido_de_outro_cliente_e_anonimo(self):
        order = self.delivered()
        other = User.objects.create_user(email="outro@shio.com", name="Outro")
        UserProfile.objects.create(user=other, role=UserRole.CUSTOMER)

        self.client.force_authenticate(other)
        response = self.client.post(
            self.request_url(order), self.payload(order), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.client.force_authenticate(None)
        response = self.client.post(
            self.request_url(order), self.payload(order), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(order.exchanges.exists())

    def test_detalhe_do_cliente_nao_expoe_dados_internos(self):
        order = self.delivered()
        self.ask(order)
        approve_exchange(order, self.admin)
        item = order.exchanges.get().items.get()
        receive_exchange(order, self.admin, {item.pk: "LOSS"}, "Peça manchada")

        self.as_customer()
        exchange = self.client.get(f"/api/orders/my-orders/{order.pk}/").data[
            "exchanges"
        ][0]
        for field in (
            "internal_note",
            "requested_by",
            "reviewed_by",
            "received_by",
            "completed_by",
            "canceled_by",
        ):
            self.assertNotIn(field, exchange)

        self.as_admin()
        exchange = self.client.get(f"/api/orders/admin/{order.pk}/").data["exchanges"][
            0
        ]
        self.assertEqual(exchange["internal_note"], "Peça manchada")
        self.assertEqual(exchange["items"][0]["condition"], "LOSS")

    def test_listas_trazem_e_filtram_status_da_troca(self):
        with_exchange = self.delivered()
        self.ask(with_exchange)
        without = self.delivered()

        self.as_customer()
        rows = {
            row["id"]: row["exchange_status"]
            for row in self.client.get("/api/orders/my-orders/").data
        }
        self.assertEqual(rows[str(with_exchange.pk)], "REQUESTED")
        self.assertIsNone(rows[str(without.pk)])

        self.as_admin()
        response = self.client.get("/api/orders/admin/?exchange_status=REQUESTED")
        self.assertEqual([row["id"] for row in response.data], [str(with_exchange.pk)])
        response = self.client.get("/api/orders/admin/?exchange_status=NOPE")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rotas_administrativas_exigem_manage_orders(self):
        order = self.delivered()
        self.ask(order)
        no_permission = User.objects.create_user(
            email="semperm@shio.com", name="Sem", is_staff=True
        )
        no_permission.user_permissions.clear()
        for action in ("approve", "reject", "receive", "complete", "cancel"):
            url = self.exchange_url(order, action)
            with self.subTest(action=action):
                self.client.force_authenticate(None)
                self.assertEqual(
                    self.client.post(url).status_code, status.HTTP_401_UNAUTHORIZED
                )
                self.client.force_authenticate(self.customer)
                self.assertEqual(
                    self.client.post(url).status_code, status.HTTP_403_FORBIDDEN
                )
                self.client.force_authenticate(User.objects.get(pk=no_permission.pk))
                self.assertEqual(
                    self.client.post(url).status_code, status.HTTP_403_FORBIDDEN
                )
        self.assertEqual(order.exchanges.get().status, ExchangeStatus.REQUESTED)

    def test_solicitacao_avisa_somente_quem_gerencia_pedidos(self):
        no_permission = User.objects.create_user(
            email="semperm@shio.com", name="Sem", is_staff=True
        )
        no_permission.user_permissions.clear()
        order = self.delivered()
        with self.captureOnCommitCallbacks(execute=True):
            self.ask(order)
        recipients = sorted(message.to[0] for message in mail.outbox)
        self.assertEqual(recipients, ["admin@shio.com", "cliente@shio.com"])

    def test_ajuste_manual_nao_aceita_motivo_troca(self):
        self.as_admin()
        response = self.client.post(
            f"/api/catalog/variations/{self.large.pk}/stock-movements/",
            {
                "kind": "SAIDA",
                "reason": "TROCA",
                "quantity": 1,
                "note": "x",
                "idempotency_key": "manual-troca",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("reason", response.data["details"])


class ConcurrentExchangeTests(ExchangeFixtures, TransactionTestCase):
    def run_together(self, *targets):
        start = threading.Barrier(len(targets))
        results = {}

        def wrap(name, target):
            try:
                start.wait(timeout=10)
                target()
                results[name] = "ok"
            except ExchangeNotAllowed as exc:
                results[name] = exc.detail["code"]
            except Exception as exc:  # noqa: BLE001 - o teste reporta o erro
                results[name] = type(exc).__name__
            finally:
                connection.close()

        threads = [
            threading.Thread(target=wrap, args=(name, target))
            for name, target in targets
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return results

    @skipUnlessDBFeature("has_select_for_update")
    def test_solicitacoes_simultaneas_resultam_em_uma_troca(self):
        order = self.delivered()
        results = self.run_together(
            ("a", lambda: self.ask(order)), ("b", lambda: self.ask(order))
        )
        self.assertEqual(sorted(results.values()), ["exchange_in_progress", "ok"])
        self.assertEqual(ExchangeRequest.objects.filter(order=order).count(), 1)

    @skipUnlessDBFeature("has_select_for_update")
    def test_aprovacoes_simultaneas_reservam_uma_vez(self):
        order = self.delivered()
        self.ask(order)
        results = self.run_together(
            ("a", lambda: approve_exchange(order, self.admin)),
            ("b", lambda: approve_exchange(order, self.admin)),
        )
        self.assertEqual(sorted(results.values()), ["no_pending_request", "ok"])
        self.assertEqual(self.movements(order), [("G", "SAIDA", "TROCA", 1)])
        self.assertEqual(self.stock(self.large), 4)

    @skipUnlessDBFeature("has_select_for_update")
    def test_troca_e_reembolso_simultaneos_tem_um_vencedor(self):
        from orders.refunds import request_refund

        order = self.delivered()
        results = self.run_together(
            ("exchange", lambda: self.ask(order)),
            (
                "refund",
                lambda: request_refund(
                    order, self.customer, RefundReason.CUSTOMER_REQUEST
                ),
            ),
        )
        if results["exchange"] == "ok":
            self.assertEqual(results["refund"], "RefundNotAllowed")
            self.assertFalse(order.refunds.exists())
        else:
            self.assertEqual(results, {"refund": "ok", "exchange": "refund_exists"})
            self.assertFalse(order.exchanges.exists())

    @skipUnlessDBFeature("has_select_for_update")
    def test_ultima_unidade_nao_e_vendida_e_reservada_ao_mesmo_tempo(self):
        move_stock(
            variation=self.large,
            kind="SAIDA",
            reason="PERDA",
            quantity=4,
            origin_type="MANUAL_ADJUSTMENT",
            origin_id=self.large.pk,
            idempotency_key="perda-g",
            created_by=self.admin,
            note="Avaria",
        )
        order = self.delivered()
        self.ask(order)
        buyer_order = self.create_order()
        buyer_item = buyer_order.items.create(
            variation=self.large,
            quantity=1,
            unit_price=100,
            product_name="Jaqueta - G",
        )

        def sell():
            move_stock(
                variation=self.large,
                kind="SAIDA",
                reason="VENDA",
                quantity=1,
                origin_type="ORDER",
                origin_id=buyer_order.pk,
                order_item=buyer_item,
                idempotency_key=f"sale:{buyer_item.pk}",
            )

        results = self.run_together(
            ("approve", lambda: approve_exchange(order, self.admin)),
            ("sale", sell),
        )
        self.assertEqual(self.stock(self.large), 0)
        if results["approve"] == "ok":
            self.assertEqual(results["sale"], "ValidationError")
        else:
            self.assertEqual(
                results, {"sale": "ok", "approve": "replacement_out_of_stock"}
            )
            self.assertEqual(order.exchanges.get().status, ExchangeStatus.REQUESTED)
