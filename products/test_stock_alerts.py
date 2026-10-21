import uuid
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from products.models import DropCampaign, Product, ProductVariation, StockAlert
from products.services import move_stock
from products.stock_alerts import schedule_product_alerts, send_stock_alerts
from products.tests import auth_header, make_product, make_user


def make_variation(product, size="M", stock=3, color=""):
    return ProductVariation.objects.create(
        product=product,
        sku=str(uuid.uuid4()),
        size=size,
        color=color,
        stock_quantity=stock,
    )


class StockAlertSendTests(TestCase):
    def setUp(self):
        self.product = make_product("Camiseta Shio")
        self.variation = make_variation(self.product)
        self.u1 = make_user("a@x.com", name="Ana")
        self.u2 = make_user("b@x.com", name="Beto")

    def alert(self, user, variation=None, **kwargs):
        return StockAlert.objects.create(
            user=user, variation=variation or self.variation, **kwargs
        )

    def test_sends_to_all_pending_in_request_order(self):
        a2 = self.alert(self.u2)
        a1 = self.alert(self.u1)
        StockAlert.objects.filter(pk=a2.pk).update(
            created_at=timezone.now() - timedelta(minutes=5)
        )
        self.assertEqual(send_stock_alerts(self.variation.id), 2)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(mail.outbox[0].to, ["b@x.com"])
        self.assertEqual(mail.outbox[1].to, ["a@x.com"])
        for a in (a1, a2):
            a.refresh_from_db()
            self.assertIsNotNone(a.notified_at)

    def test_email_has_product_size_link_and_no_reservation_notice(self):
        variation = make_variation(self.product, size="G", color="Azul")
        self.alert(self.u1, variation)
        send_stock_alerts(variation.id)
        msg = mail.outbox[0]
        html = msg.alternatives[0][0]
        link = f"{settings.FRONTEND_URL}/product/{self.product.id}"
        self.assertEqual(msg.subject, "Chegou! Camiseta Shio — Shio")
        for body in (msg.body, html):
            self.assertIn("Camiseta Shio", body)
            self.assertIn("G / Azul", body)
            self.assertIn(link, body)
            self.assertIn("quem comprar primeiro leva", body.lower())

    def test_email_size_without_color(self):
        self.alert(self.u1)
        send_stock_alerts(self.variation.id)
        self.assertIn("(M)", mail.outbox[0].body)

    def test_other_variation_alert_not_sent(self):
        other = make_variation(self.product, size="G", stock=0)
        pending = self.alert(self.u1, other)
        self.alert(self.u2)
        self.assertEqual(send_stock_alerts(self.variation.id), 1)
        self.assertEqual(mail.outbox[0].to, ["b@x.com"])
        pending.refresh_from_db()
        self.assertIsNone(pending.notified_at)

    def test_failure_keeps_alert_pending_and_others_still_sent(self):
        a1 = self.alert(self.u1)
        a2 = self.alert(self.u2)
        StockAlert.objects.filter(pk=a1.pk).update(
            created_at=timezone.now() - timedelta(minutes=5)
        )
        with patch("products.stock_alerts.send_email", side_effect=[False, True]):
            self.assertEqual(send_stock_alerts(self.variation.id), 1)
        a1.refresh_from_db()
        a2.refresh_from_db()
        self.assertIsNone(a1.notified_at)
        self.assertIsNotNone(a2.notified_at)

    def test_not_sellable_product_sends_nothing(self):
        a = self.alert(self.u1)

        self.product.is_active = False
        self.product.save()
        self.assertEqual(send_stock_alerts(self.variation.id), 0)
        self.product.is_active = True
        self.product.save()

        drop = DropCampaign.objects.create(name="D", slug="d", is_public=False)
        self.product.drop = drop
        self.product.save()
        self.assertEqual(send_stock_alerts(self.variation.id), 0)

        drop.is_public = True
        drop.is_active = True
        drop.end_date = timezone.now() - timedelta(days=1)
        drop.save()
        self.assertEqual(send_stock_alerts(self.variation.id), 0)

        self.assertEqual(len(mail.outbox), 0)
        a.refresh_from_db()
        self.assertIsNone(a.notified_at)

    def test_out_of_stock_again_sends_nothing(self):
        variation = make_variation(self.product, size="P", stock=0)
        a = self.alert(self.u1, variation)
        self.assertEqual(send_stock_alerts(variation.id), 0)
        a.refresh_from_db()
        self.assertIsNone(a.notified_at)

    def test_already_notified_not_resent(self):
        self.alert(self.u1, notified_at=timezone.now())
        self.assertEqual(send_stock_alerts(self.variation.id), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_inactive_user_is_skipped(self):
        self.u1.is_active = False
        self.u1.save()
        a = self.alert(self.u1)
        self.alert(self.u2)
        self.assertEqual(send_stock_alerts(self.variation.id), 1)
        self.assertEqual([m.to for m in mail.outbox], [["b@x.com"]])
        a.refresh_from_db()
        self.assertIsNone(a.notified_at)

    def test_claimed_alert_is_not_sent_twice(self):
        self.alert(self.u1)
        self.assertEqual(send_stock_alerts(self.variation.id), 1)
        self.assertEqual(send_stock_alerts(self.variation.id), 0)
        self.assertEqual(len(mail.outbox), 1)

    def test_pending_unique_but_can_request_again_after_notified(self):
        first = self.alert(self.u1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.alert(self.u1)
        first.notified_at = timezone.now()
        first.save()
        self.alert(self.u1)
        self.assertEqual(StockAlert.objects.filter(user=self.u1).count(), 2)

    def test_schedule_product_alerts_only_stocked_variations_with_pending(self):
        empty = make_variation(self.product, size="G", stock=0)
        self.alert(self.u1)
        self.alert(self.u2, empty)
        with self.captureOnCommitCallbacks(execute=True):
            schedule_product_alerts(self.product)
        self.assertEqual([m.to for m in mail.outbox], [["a@x.com"]])


class StockAlertTriggerTests(APITestCase):
    def setUp(self):
        from authentication.models import UserRole

        self.admin = make_user(
            "admin@x.com", role=UserRole.ADMIN, name="Admin", is_staff=True
        )
        self.customer = make_user("c@x.com", name="Cliente")
        self.product = make_product("Camiseta Shio", cost_price=50)
        self.variation = make_variation(self.product, stock=0)

    def pending(self, variation=None):
        return StockAlert.objects.create(
            user=self.customer, variation=variation or self.variation
        )

    def move(self, kind="ENTRADA", quantity=2, key="k1", variation=None):
        return move_stock(
            variation=variation or self.variation,
            kind=kind,
            reason="AJUSTE",
            quantity=quantity,
            origin_type="MANUAL_ADJUSTMENT",
            origin_id=uuid.UUID(int=1),
            idempotency_key=key,
            created_by=self.admin,
            note="x",
        )

    def test_restock_from_zero_sends(self):
        self.pending()
        with self.captureOnCommitCallbacks(execute=True):
            self.move()
        self.assertEqual(len(mail.outbox), 1)

    def test_restock_from_positive_does_not_send(self):
        ProductVariation.objects.filter(pk=self.variation.pk).update(stock_quantity=3)
        self.pending()
        with self.captureOnCommitCallbacks(execute=True):
            self.move(quantity=2)
        self.assertEqual(len(mail.outbox), 0)

    def test_idempotent_replay_does_not_dispatch(self):
        self.pending()
        with self.captureOnCommitCallbacks(execute=True):
            self.move(quantity=2, key="same")
        self.assertEqual(len(mail.outbox), 1)
        with self.captureOnCommitCallbacks(execute=True):
            self.move(kind="SAIDA", quantity=2, key="out")
        self.pending()
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            self.move(quantity=2, key="same")
        self.assertEqual(callbacks, [])
        self.assertEqual(len(mail.outbox), 1)

    def test_rollback_does_not_dispatch(self):
        self.pending()
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    self.move()
                    raise RuntimeError("abort")
        self.assertEqual(callbacks, [])
        self.assertEqual(len(mail.outbox), 0)

    def test_order_cancel_restock_sends(self):
        from orders.models import CustomerOrder, OrderItem, OrderStatus
        from orders.services import restore_order_stock

        ProductVariation.objects.filter(pk=self.variation.pk).update(stock_quantity=1)
        order = CustomerOrder.objects.create(
            user=self.customer,
            subtotal=100,
            total_amount=100,
            status=OrderStatus.CANCELED,
            shipping_zip_code="000",
            shipping_street="X",
            shipping_number="1",
            shipping_neighborhood="Y",
            shipping_city="Z",
            shipping_state="DF",
        )
        item = OrderItem.objects.create(
            order=order,
            variation=self.variation,
            quantity=1,
            unit_price=100,
            product_name="Camiseta Shio - M",
            sku_snapshot=self.variation.sku,
        )
        move_stock(
            variation=self.variation,
            kind="SAIDA",
            reason="VENDA",
            quantity=1,
            origin_type="ORDER",
            origin_id=order.pk,
            order_item=item,
            idempotency_key=f"sale:{item.pk}",
            created_by=self.customer,
        )
        other = make_user("o@x.com", name="Outro")
        StockAlert.objects.create(user=other, variation=self.variation)
        with self.captureOnCommitCallbacks(execute=True):
            restore_order_stock(order, changed_by=self.admin)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["o@x.com"])

    def test_product_reactivation_sends(self):
        Product.objects.filter(pk=self.product.pk).update(is_active=False)
        ProductVariation.objects.filter(pk=self.variation.pk).update(stock_quantity=2)
        self.pending()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                f"/api/catalog/products/{self.product.id}/",
                {"is_active": True},
                format="json",
                **auth_header(self.admin),
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(mail.outbox), 1)

    def test_saving_active_product_does_not_send(self):
        ProductVariation.objects.filter(pk=self.variation.pk).update(stock_quantity=2)
        self.pending()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                f"/api/catalog/products/{self.product.id}/",
                {"name": "Novo nome"},
                format="json",
                **auth_header(self.admin),
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(mail.outbox), 0)

    def test_drop_made_public_sends(self):
        drop = DropCampaign.objects.create(
            name="Drop", slug="drop-x", is_public=False, is_active=True
        )
        Product.objects.filter(pk=self.product.pk).update(drop=drop)
        ProductVariation.objects.filter(pk=self.variation.pk).update(stock_quantity=2)
        self.pending()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.put(
                f"/api/catalog/drops/{drop.id}/",
                {
                    "name": "Drop",
                    "slug": "drop-x",
                    "is_public": True,
                    "is_active": True,
                },
                format="json",
                **auth_header(self.admin),
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(mail.outbox), 1)

    def test_product_moved_out_of_closed_drop_sends(self):
        drop = DropCampaign.objects.create(
            name="Drop",
            slug="drop-y",
            is_public=True,
            is_active=True,
            end_date=timezone.now() - timedelta(days=1),
        )
        Product.objects.filter(pk=self.product.pk).update(drop=drop)
        ProductVariation.objects.filter(pk=self.variation.pk).update(stock_quantity=2)
        self.pending()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(
                f"/api/catalog/drops/{drop.id}/products/{self.product.id}/",
                **auth_header(self.admin),
            )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(len(mail.outbox), 1)


class StockAlertApiTests(APITestCase):
    list_url = "/api/catalog/stock-alerts/"
    pending_url = "/api/catalog/stock-alerts/pending-variations/"

    def setUp(self):
        self.user = make_user("cliente@x.com", name="Cliente")
        self.other = make_user("outro@x.com", name="Outro")
        self.product = make_product("Camiseta Shio")
        self.variation = make_variation(self.product, size="M", stock=0)

    def headers(self, user=None):
        return auth_header(user or self.user)

    def post(self, variation_id, user=None):
        return self.client.post(
            self.list_url,
            {"variation_id": str(variation_id)},
            format="json",
            **self.headers(user),
        )

    def test_anonymous_gets_401(self):
        alert = StockAlert.objects.create(user=self.user, variation=self.variation)
        self.assertEqual(self.client.get(self.list_url).status_code, 401)
        self.assertEqual(
            self.client.post(
                self.list_url, {"variation_id": str(self.variation.id)}, format="json"
            ).status_code,
            401,
        )
        self.assertEqual(self.client.get(self.pending_url).status_code, 401)
        self.assertEqual(
            self.client.delete(f"{self.list_url}{alert.id}/").status_code, 401
        )

    def test_create_then_duplicate_returns_200_already_pending(self):
        first = self.post(self.variation.id)
        self.assertEqual(first.status_code, 201)
        self.assertFalse(first.data["already_pending"])
        self.assertEqual(first.data["variation_id"], str(self.variation.id))
        self.assertEqual(first.data["size"], "M")
        self.assertIsNone(first.data["notified_at"])
        self.assertEqual(first.data["product"]["id"], str(self.product.id))
        self.assertTrue(first.data["product"]["is_visible"])
        self.assertIsNone(first.data["product"]["image"])
        second = self.post(self.variation.id)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.data["already_pending"])
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(StockAlert.objects.count(), 1)

    def test_create_on_stocked_variation_400(self):
        stocked = make_variation(self.product, size="G", stock=2)
        response = self.post(stocked.id)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data["variation_id"], ["Este tamanho está disponível."]
        )
        self.assertEqual(StockAlert.objects.count(), 0)

    def test_create_on_not_sellable_400(self):
        drop = DropCampaign.objects.create(
            name="Drop", slug="drop-x", is_public=True, is_active=False
        )
        product = make_product("Do drop", drop=drop)
        variation = make_variation(product, stock=0)
        response = self.post(variation.id)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data["variation_id"],
            ["Este produto não está à venda no momento."],
        )

    def test_create_on_hidden_or_missing_404(self):
        hidden = make_product("Oculto", is_active=False)
        hidden_variation = make_variation(hidden, stock=0)
        self.assertEqual(self.post(hidden_variation.id).status_code, 404)
        self.assertEqual(self.post(uuid.uuid4()).status_code, 404)

    def test_list_only_own_pending_first(self):
        v2 = make_variation(self.product, size="G", stock=0)
        v3 = make_variation(self.product, size="P", stock=0)
        notified = StockAlert.objects.create(
            user=self.user, variation=self.variation, notified_at=timezone.now()
        )
        old_pending = StockAlert.objects.create(user=self.user, variation=v2)
        new_pending = StockAlert.objects.create(user=self.user, variation=v3)
        StockAlert.objects.filter(pk=old_pending.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        StockAlert.objects.filter(pk=notified.pk).update(
            created_at=timezone.now() + timedelta(days=1)
        )
        StockAlert.objects.create(user=self.other, variation=v3)
        response = self.client.get(self.list_url, **self.headers())
        self.assertEqual(response.status_code, 200)
        ids = [item["id"] for item in response.data["results"]]
        self.assertEqual(
            ids, [str(new_pending.id), str(old_pending.id), str(notified.id)]
        )
        self.assertIsNotNone(response.data["results"][2]["notified_at"])

    def test_pending_variations_ids(self):
        v2 = make_variation(self.product, size="G", stock=0)
        StockAlert.objects.create(user=self.user, variation=self.variation)
        StockAlert.objects.create(
            user=self.user, variation=v2, notified_at=timezone.now()
        )
        StockAlert.objects.create(user=self.other, variation=v2)
        response = self.client.get(self.pending_url, **self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"variation_ids": [str(self.variation.id)]})

    def test_delete_own_pending_204(self):
        alert = StockAlert.objects.create(user=self.user, variation=self.variation)
        response = self.client.delete(f"{self.list_url}{alert.id}/", **self.headers())
        self.assertEqual(response.status_code, 204)
        self.assertFalse(StockAlert.objects.filter(pk=alert.pk).exists())

    def test_delete_notified_or_other_user_404(self):
        notified = StockAlert.objects.create(
            user=self.user, variation=self.variation, notified_at=timezone.now()
        )
        theirs = StockAlert.objects.create(user=self.other, variation=self.variation)
        for pk in (notified.pk, theirs.pk, uuid.uuid4()):
            response = self.client.delete(f"{self.list_url}{pk}/", **self.headers())
            self.assertEqual(response.status_code, 404)
        self.assertEqual(StockAlert.objects.count(), 2)

    def test_can_request_again_after_notified_201(self):
        StockAlert.objects.create(
            user=self.user, variation=self.variation, notified_at=timezone.now()
        )
        response = self.post(self.variation.id)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(StockAlert.objects.filter(user=self.user).count(), 2)
