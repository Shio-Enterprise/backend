"""#105: rotas de cupom pedem Pedidos (manage_orders) e o formulário de cupom
vê os drops privados com essa permissão."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from products.models import DropCampaign
from products.tests import make_user

from .models import Coupon, CouponDiscountType


def make_admin(email, *codenames):
    user = make_user(email, is_staff=True)
    user.user_permissions.set(
        Permission.objects.filter(
            content_type__app_label="authentication", codename__in=codenames
        )
    )
    return user


class CouponRoutesRequireManageOrdersTests(APITestCase):
    def setUp(self):
        self.coupon = Coupon.objects.create(
            code="PERM105",
            discount_type=CouponDiscountType.PERCENTAGE,
            discount_value=Decimal("10.00"),
            is_active=True,
        )
        self.orders_admin = make_admin("pedidos@example.com", "manage_orders")
        self.other_admin = make_admin(
            "catalogo@example.com", "manage_catalog", "manage_drops", "manage_customers"
        )
        self.list_url = reverse("orders:admin-coupons-list")
        self.detail_url = reverse("orders:admin-coupons-detail", args=[self.coupon.id])
        self.email_urls = [
            reverse(name, args=[self.coupon.id])
            for name in (
                "orders:admin-coupon-email",
                "orders:admin-coupon-email-preview",
                "orders:admin-coupon-email-test",
                "orders:admin-coupon-email-send",
            )
        ]

    def test_admin_without_manage_orders_gets_403_on_every_coupon_route(self):
        self.client.force_authenticate(self.other_admin)
        calls = [
            ("get", self.list_url),
            ("post", self.list_url),
            ("get", self.detail_url),
            ("patch", self.detail_url),
            ("put", self.detail_url),
            ("delete", self.detail_url),
            ("get", self.email_urls[0]),
            ("post", self.email_urls[1]),
            ("post", self.email_urls[2]),
            ("post", self.email_urls[3]),
        ]
        for method, url in calls:
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(url, {}, format="json")
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Coupon.objects.filter(id=self.coupon.id).exists())

    def test_admin_with_only_manage_orders_can_use_coupon_routes(self):
        self.client.force_authenticate(self.orders_admin)
        self.assertEqual(self.client.get(self.list_url).status_code, 200)
        self.assertEqual(self.client.get(self.detail_url).status_code, 200)
        response = self.client.patch(
            self.detail_url, {"discount_value": "15.00"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(self.email_urls[0]).status_code, 200)
        response = self.client.post(
            self.list_url,
            {
                "code": "NOVO105",
                "discount_type": "FIXED_VALUE",
                "discount_value": "5.00",
                "is_active": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.client.delete(self.detail_url).status_code, 204)

    def test_orders_admin_can_link_coupon_to_private_drop(self):
        drop = DropCampaign.objects.create(
            name="Drop privado",
            slug="drop-privado",
            launch_date=timezone.now() + timedelta(days=3),
            is_public=False,
        )
        self.client.force_authenticate(self.orders_admin)
        response = self.client.patch(
            self.detail_url, {"drops": [str(drop.id)]}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(self.coupon.drops.all()), [drop])

    @patch("orders.views.send_coupon_test", return_value=True)
    def test_orders_admin_can_send_test_email(self, _send):
        self.client.force_authenticate(self.orders_admin)
        response = self.client.post(self.email_urls[2], {}, format="json")
        self.assertEqual(response.status_code, 200)


class DropListForCouponFormTests(APITestCase):
    def setUp(self):
        now = timezone.now()
        self.public = DropCampaign.objects.create(
            name="Público",
            slug="publico",
            launch_date=now - timedelta(days=1),
            is_public=True,
        )
        self.private = DropCampaign.objects.create(
            name="Privado",
            slug="privado",
            launch_date=now + timedelta(days=5),
            is_public=False,
        )
        self.url = reverse("products:drop-list-create")

    def names(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return [drop["name"] for drop in response.data["results"]]

    def test_orders_admin_sees_private_drops_newest_first(self):
        self.client.force_authenticate(
            make_admin("so-pedidos@example.com", "manage_orders")
        )
        self.assertEqual(self.names(), ["Privado", "Público"])

    def test_orders_admin_preview_still_hides_private(self):
        self.client.force_authenticate(
            make_admin("preview@example.com", "manage_orders")
        )
        response = self.client.get(self.url, {"visible": "true"})
        self.assertEqual([d["name"] for d in response.data["results"]], ["Público"])

    def test_anonymous_customer_and_unrelated_admin_see_only_public(self):
        users = [
            None,
            make_user("cliente@example.com"),
            make_admin("clientes@example.com", "manage_customers"),
        ]
        for user in users:
            with self.subTest(user=getattr(user, "email", "anônimo")):
                self.client.force_authenticate(user)
                self.assertEqual(self.names(), ["Público"])
