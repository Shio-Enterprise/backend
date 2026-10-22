from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from products.models import Product, ProductVariation
from products.services import get_inventory_summary


class InventorySummaryTests(TestCase):
    def test_aggregates_all_products_in_one_query_including_empty_inventory(self):
        product = Product.objects.create(name="Camisa", base_price="10.00")
        ProductVariation.objects.create(
            product=product, size="P", sku="CAMISA-P", stock_quantity=3
        )
        ProductVariation.objects.create(
            product=product, size="M", sku="CAMISA-M", stock_quantity=4
        )
        Product.objects.create(name="Boné", base_price="20.00")

        with CaptureQueriesContext(connection) as queries:
            summary = get_inventory_summary()

        self.assertEqual(len(queries), 1)
        self.assertEqual(
            {item["name"]: item["total_stock"] for item in summary},
            {"Camisa": 7, "Boné": 0},
        )
