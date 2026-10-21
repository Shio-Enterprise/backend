import uuid
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from products.models import DropCampaign, ProductVariation, StockAlert
from products.stock_alerts import schedule_product_alerts, send_stock_alerts
from products.tests import make_product, make_user


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
        with patch(
            "products.stock_alerts.send_email", side_effect=[False, True]
        ):
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
