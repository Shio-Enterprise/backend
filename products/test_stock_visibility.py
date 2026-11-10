from django.contrib.auth.models import Permission
from rest_framework import status
from rest_framework.test import APITestCase

from authentication.models import UserRole
from products.models import DropCampaign, Product, ProductVariation, Wishlist
from products.tests import make_user

LIST_URL = "/api/catalog/products/"


def variations_of(payload):
    return {v["size"]: v for v in payload["variations"]}


class PublicCatalogHidesStockQuantityTests(APITestCase):
    def setUp(self):
        self.drop = DropCampaign.objects.create(
            name="Drop", slug="drop", is_public=True, is_active=True
        )
        self.product = Product.objects.create(
            name="Camiseta", description="d", base_price=100, drop=self.drop
        )
        ProductVariation.objects.create(
            product=self.product, size="M", sku="CAM-M", stock_quantity=12
        )
        ProductVariation.objects.create(
            product=self.product, size="G", sku="CAM-G", stock_quantity=0
        )
        self.detail_url = f"{LIST_URL}{self.product.id}/"
        self.customer = make_user("cliente@shio.com")

    def assert_hides_stock(self, variations):
        self.assertEqual(variations["M"]["in_stock"], True)
        self.assertEqual(variations["G"]["in_stock"], False)
        for variation in variations.values():
            self.assertNotIn("stock_quantity", variation)

    def test_anonymous_list_and_detail(self):
        listing = self.client.get(LIST_URL)
        detail = self.client.get(self.detail_url)

        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assert_hides_stock(variations_of(listing.data["results"][0]))
        self.assert_hides_stock(variations_of(detail.data))

    def test_customer_list_and_detail(self):
        self.client.force_authenticate(self.customer)
        listing = self.client.get(LIST_URL)
        detail = self.client.get(self.detail_url)

        self.assert_hides_stock(variations_of(listing.data["results"][0]))
        self.assert_hides_stock(variations_of(detail.data))

    def test_public_drop_page(self):
        response = self.client.get(f"/api/catalog/drops/slug/{self.drop.slug}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assert_hides_stock(variations_of(response.data["products"][0]))

    def test_wishlist(self):
        Wishlist.objects.create(user=self.customer, product=self.product)
        self.client.force_authenticate(self.customer)
        response = self.client.get("/api/catalog/wishlist/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assert_hides_stock(variations_of(response.data["results"][0]["product"]))

    def test_raw_json_has_no_quantity(self):
        body = self.client.get(self.detail_url).content.decode()

        self.assertNotIn("stock_quantity", body)
        self.assertNotIn('"12"', body)


class AdminStillSeesStockQuantityTests(APITestCase):
    def setUp(self):
        self.admin = make_user("admin@shio.com", role=UserRole.ADMIN)
        self.product = Product.objects.create(
            name="Moletom", description="d", base_price=200
        )
        self.variation = ProductVariation.objects.create(
            product=self.product, size="P", sku="MOL-P", stock_quantity=7
        )
        self.client.force_authenticate(self.admin)

    def test_admin_list_and_detail_show_quantity(self):
        listing = self.client.get(LIST_URL)
        detail = self.client.get(f"{LIST_URL}{self.product.id}/")

        listed = variations_of(listing.data["results"][0])["P"]
        detailed = variations_of(detail.data)["P"]
        for variation in (listed, detailed):
            self.assertEqual(variation["stock_quantity"], 7)
            self.assertTrue(variation["in_stock"])

    def test_admin_with_other_area_still_sees_quantity(self):
        orders_only = make_user("pedidos@shio.com", role=UserRole.ADMIN)
        orders_only.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label="authentication", codename="manage_orders"
            )
        )
        self.client.force_authenticate(orders_only)
        response = self.client.get(f"{LIST_URL}{self.product.id}/")

        self.assertEqual(variations_of(response.data)["P"]["stock_quantity"], 7)

    def test_create_and_update_variation_return_quantity(self):
        created = self.client.post(
            f"/api/catalog/products/{self.product.id}/variations/",
            {"size": "M", "stock_quantity": 4},
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertEqual(created.data["stock_quantity"], 4)

        updated = self.client.put(
            f"/api/catalog/variations/{self.variation.id}/",
            {"size": "PP"},
            format="json",
        )
        self.assertEqual(updated.status_code, status.HTTP_200_OK)
        self.assertEqual(updated.data["stock_quantity"], 7)
