from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from authentication.models import Address
from orders.models import Cart, CartItem
from orders.tests import SHIPPING_TEST_SETTINGS
from products.models import Product, ProductVariation

User = get_user_model()

CART_URL = "/api/orders/cart/"
ITEMS_URL = "/api/orders/cart/items/"


class CartHidesStockQuantityTests(APITestCase):
    def setUp(self):
        self.product = Product.objects.create(
            name="Tênis", description="d", base_price=150
        )
        self.variation = ProductVariation.objects.create(
            product=self.product, size="40", sku="TEN-40", stock_quantity=3
        )
        self.user = User.objects.create_user(
            email="cliente@shio.com", name="Cliente", password="senha-forte-123"
        )

    def add(self, quantity):
        return self.client.post(
            ITEMS_URL,
            {"variation_id": str(self.variation.id), "quantity": quantity},
            format="json",
        )

    def assert_item_hides_stock(self, response):
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item = response.data["items"][0]
        self.assertTrue(item["in_stock"])
        self.assertNotIn("stock_quantity", item)

    def test_anonymous_cart(self):
        self.assert_item_hides_stock(self.add(1))
        self.assert_item_hides_stock(self.client.get(CART_URL))

    def test_authenticated_cart(self):
        self.client.force_authenticate(self.user)
        self.assert_item_hides_stock(self.add(1))
        self.assert_item_hides_stock(self.client.get(CART_URL))

    def test_sold_out_item_in_cart_is_marked(self):
        for user in (None, self.user):
            with self.subTest(authenticated=user is not None):
                self.client.force_authenticate(user)
                stock = ProductVariation.objects.filter(pk=self.variation.pk)
                stock.update(stock_quantity=3)
                self.add(1)
                stock.update(stock_quantity=0)

                item = self.client.get(CART_URL).data["items"][0]

                self.assertFalse(item["in_stock"])
                self.assertNotIn("stock_quantity", item)

    def test_adding_more_than_stock_is_refused_without_the_number(self):
        for user in (None, self.user):
            with self.subTest(authenticated=user is not None):
                self.client.force_authenticate(user)
                response = self.add(4)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(
                    response.data["message"], "Estoque insuficiente para Tênis."
                )

    def test_adding_up_to_stock_then_one_more_is_refused(self):
        self.client.force_authenticate(self.user)
        self.assertEqual(self.add(3).status_code, status.HTTP_200_OK)
        response = self.add(1)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("3", response.data["message"])

    def test_updating_quantity_above_stock_is_refused_without_the_number(self):
        for user in (None, self.user):
            with self.subTest(authenticated=user is not None):
                self.client.force_authenticate(user)
                self.add(1)
                response = self.client.patch(
                    f"{ITEMS_URL}{self.variation.id}/", {"quantity": 9}, format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(
                    response.data["message"], "Estoque insuficiente para Tênis."
                )


@override_settings(**SHIPPING_TEST_SETTINGS)
@patch(
    "orders.services.fetch_shipping_deadline_by_service_and_ceps",
    return_value={"prazoEntrega": 3},
)
@patch(
    "orders.services.fetch_shipping_price_by_service_and_ceps",
    return_value={"pcFinal": "15,00"},
)
class CheckoutQuoteHidesStockQuantityTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="cliente@shio.com", name="Cliente", password="senha-forte-123"
        )
        self.address = Address.objects.create(
            user=self.user,
            zip_code="71000000",
            street="Rua",
            address_number="1",
            neighborhood="Centro",
            city="Brasília",
            state="DF",
        )
        product = Product.objects.create(name="Boné", description="d", base_price=80)
        self.variation = ProductVariation.objects.create(
            product=product, size="U", sku="BON-U", stock_quantity=5
        )
        self.cart = Cart.objects.create(user=self.user, status="ACTIVE")
        self.item = CartItem.objects.create(
            cart=self.cart, variation=self.variation, quantity=2, unit_price=80
        )
        self.client.force_authenticate(self.user)

    def calculate(self):
        return self.client.post(
            "/api/orders/checkout/calculate/",
            {"address_id": str(self.address.pk)},
            format="json",
        )

    def test_quote_items_hide_quantity(self, *_mocks):
        response = self.calculate()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item = response.data["items"][0]
        self.assertNotIn("stock_quantity", item)
        self.assertNotIn("in_stock", item)

    def test_quote_refuses_more_than_stock(self, *_mocks):
        self.item.quantity = 6
        self.item.save()

        response = self.calculate()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("5", str(response.data))
