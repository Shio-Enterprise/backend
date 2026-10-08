from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Coupon, CouponDiscountType

User = get_user_model()


class AdminCouponViewSetTests(APITestCase):
    def setUp(self):
        Coupon.objects.all().delete()
        self.admin_user = User.objects.create_superuser(
            email="admin@shio.com", password="adminpassword"
        )
        self.normal_user = User.objects.create_user(
            email="user@shio.com", password="userpassword"
        )
        self.list_url = reverse("orders:admin-coupons-list")

        self.coupon = Coupon.objects.create(
            code="TEST10",
            discount_type=CouponDiscountType.PERCENTAGE,
            discount_value=Decimal("10.00"),
            is_active=True,
        )
        self.detail_url = reverse("orders:admin-coupons-detail", args=[self.coupon.id])

    def test_admin_can_list_coupons(self):
        self.client.force_authenticate(user=self.admin_user)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # DRF test may paginate, DRF list uses response.data['results'] if paginated
        results = (
            response.data["results"] if "results" in response.data else response.data
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["code"], "TEST10")

    def test_normal_user_cannot_list_coupons(self):
        self.client.force_authenticate(user=self.normal_user)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_user_cannot_list_coupons(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_admin_can_create_coupon(self):
        self.client.force_authenticate(user=self.admin_user)
        payload = {
            "code": "NEW20",
            "discount_type": "FIXED_VALUE",
            "discount_value": "20.00",
            "max_uses_total": 50,
            "is_active": True,
        }
        response = self.client.post(self.list_url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Coupon.objects.filter(code="NEW20").exists())

    def test_admin_can_update_coupon(self):
        self.client.force_authenticate(user=self.admin_user)
        payload = {"is_active": False, "max_uses_total": 100}
        response = self.client.patch(self.detail_url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.coupon.refresh_from_db()
        self.assertFalse(self.coupon.is_active)
        self.assertEqual(self.coupon.max_uses_total, 100)

    def test_admin_can_delete_coupon(self):
        self.client.force_authenticate(user=self.admin_user)
        response = self.client.delete(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Coupon.objects.filter(id=self.coupon.id).exists())
