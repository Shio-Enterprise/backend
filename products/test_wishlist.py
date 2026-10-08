import uuid
from threading import Barrier
from unittest import skipIf

from django.db import IntegrityError, connection, transaction
from django.test import TransactionTestCase
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from .models import DropCampaign, Wishlist
from .tests import auth_header, make_product, make_stocked_product, make_user


class WishlistApiTests(APITestCase):
    list_url = "/api/catalog/wishlist/"
    ids_url = "/api/catalog/wishlist/ids/"

    def setUp(self):
        self.user = make_user("wishlist@example.com")
        self.other = make_user("other-wishlist@example.com")
        self.product = make_stocked_product(name="Favorito")
        self.out_of_stock = make_product(name="Sem estoque")
        self.hidden = make_product(name="Oculto", is_active=False)

    def headers(self, user=None):
        return auth_header(user or self.user)

    def test_anonymous_requires_authentication(self):
        self.assertEqual(
            self.client.get(self.list_url).status_code, status.HTTP_401_UNAUTHORIZED
        )
        self.assertEqual(
            self.client.post(
                self.list_url, {"product": str(self.product.id)}, format="json"
            ).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.client.get(self.ids_url).status_code, status.HTTP_401_UNAUTHORIZED
        )
        self.assertEqual(
            self.client.delete(f"{self.list_url}{self.product.id}/").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_create_is_idempotent_and_unique_per_user(self):
        first = self.client.post(
            self.list_url,
            {"product": str(self.product.id)},
            format="json",
            **self.headers(),
        )
        second = self.client.post(
            self.list_url,
            {"product": str(self.product.id)},
            format="json",
            **self.headers(),
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(Wishlist.objects.filter(user=self.user).count(), 1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Wishlist.objects.create(user=self.user, product=self.product)

    def test_out_of_stock_is_listed_but_hidden_cannot_be_added(self):
        response = self.client.post(
            self.list_url,
            {"product": str(self.out_of_stock.id)},
            format="json",
            **self.headers(),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        hidden = self.client.post(
            self.list_url,
            {"product": str(self.hidden.id)},
            format="json",
            **self.headers(),
        )
        self.assertEqual(hidden.status_code, status.HTTP_404_NOT_FOUND)
        listed = self.client.get(self.list_url, **self.headers()).json()["results"]
        self.assertEqual(
            [row["product"]["id"] for row in listed], [str(self.out_of_stock.id)]
        )

    def test_saved_hidden_is_omitted_and_reappears_when_restored(self):
        entry = Wishlist.objects.create(user=self.user, product=self.product)
        self.product.is_active = False
        self.product.save(update_fields=["is_active"])
        self.assertEqual(
            self.client.get(self.list_url, **self.headers()).json()["count"], 0
        )
        self.assertEqual(
            self.client.get(self.ids_url, **self.headers()).json()["product_ids"], []
        )
        self.product.is_active = True
        self.product.save(update_fields=["is_active"])
        self.assertEqual(
            self.client.get(self.list_url, **self.headers()).json()["count"], 1
        )
        self.assertEqual(
            self.client.get(self.ids_url, **self.headers()).json()["product_ids"],
            [str(self.product.id)],
        )
        self.assertTrue(Wishlist.objects.filter(pk=entry.pk).exists())

    def test_private_drop_is_hidden_even_for_admin(self):
        private = DropCampaign.objects.create(
            name="Private", slug="private", is_public=False
        )
        product = make_stocked_product(name="Private product", drop=private)
        admin = make_user("wishlist-admin@example.com", is_staff=True)
        for user in (self.user, admin):
            response = self.client.post(
                self.list_url,
                {"product": str(product.id)},
                format="json",
                **self.headers(user),
            )
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_isolation_and_ids_are_not_page_limited(self):
        products = [make_stocked_product(name=f"P{i}") for i in range(22)]
        for product in [self.product, *products]:
            self.client.post(
                self.list_url,
                {"product": str(product.id)},
                format="json",
                **self.headers(),
            )
        self.client.post(
            self.list_url,
            {"product": str(self.product.id)},
            format="json",
            **self.headers(self.other),
        )
        page = self.client.get(self.list_url, **self.headers()).json()
        self.assertEqual(page["count"], 23)
        ids = self.client.get(self.ids_url, **self.headers()).json()["product_ids"]
        self.assertEqual(len(ids), 23)
        self.assertEqual(
            self.client.get(self.ids_url, **self.headers(self.other)).json()[
                "product_ids"
            ],
            [str(self.product.id)],
        )

    def test_delete_is_idempotent_and_can_remove_hidden_favorite(self):
        Wishlist.objects.create(user=self.user, product=self.hidden)
        response = self.client.delete(
            f"{self.list_url}{self.hidden.id}/", **self.headers()
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Wishlist.objects.filter(user=self.user).exists())
        self.assertEqual(
            self.client.delete(
                f"{self.list_url}{self.hidden.id}/", **self.headers()
            ).status_code,
            status.HTTP_204_NO_CONTENT,
        )

    def test_delete_is_scoped_to_user(self):
        own = Wishlist.objects.create(user=self.user, product=self.product)
        other = Wishlist.objects.create(user=self.other, product=self.product)
        response = self.client.delete(
            f"{self.list_url}{self.product.id}/", **self.headers()
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Wishlist.objects.filter(pk=own.pk).exists())
        self.assertTrue(Wishlist.objects.filter(pk=other.pk).exists())

    def test_input_cannot_spoof_user_and_delete_product_is_safe(self):
        response = self.client.post(
            self.list_url,
            {"product": str(self.product.id), "user": str(self.other.id)},
            format="json",
            **self.headers(),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            Wishlist.objects.filter(user=self.user, product=self.product).exists()
        )
        self.assertFalse(
            Wishlist.objects.filter(user=self.other, product=self.product).exists()
        )
        variation = self.product.variations.get()
        self.assertEqual(variation.stock_quantity, 1)

    def test_missing_or_invalid_product_is_rejected(self):
        missing = self.client.post(self.list_url, {}, format="json", **self.headers())
        invalid = self.client.post(
            self.list_url, {"product": "nope"}, format="json", **self.headers()
        )
        unknown = self.client.post(
            self.list_url,
            {"product": "00000000-0000-0000-0000-000000000000"},
            format="json",
            **self.headers(),
        )
        self.assertEqual(missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(unknown.status_code, status.HTTP_404_NOT_FOUND)

    def test_cascade_on_user_and_product(self):
        product = make_product(name="Cascade")
        user = make_user("cascade-wishlist@example.com")
        Wishlist.objects.create(user=user, product=product)
        user.delete()
        self.assertFalse(Wishlist.objects.filter(product=product).exists())
        Wishlist.objects.create(user=self.user, product=product)
        product.delete()
        self.assertFalse(Wishlist.objects.filter(user=self.user).exists())

    def test_page_two_contains_remaining_favorites(self):
        second = make_stocked_product(name="Segundo")
        for product in (self.product, second):
            self.client.post(
                self.list_url,
                {"product": str(product.id)},
                format="json",
                **self.headers(),
            )
        first_page = self.client.get(
            f"{self.list_url}?page_size=1&page=1", **self.headers()
        ).json()
        second_page = self.client.get(
            f"{self.list_url}?page_size=1&page=2", **self.headers()
        ).json()
        self.assertEqual(first_page["count"], 2)
        self.assertEqual(second_page["count"], 2)
        self.assertNotEqual(
            first_page["results"][0]["id"], second_page["results"][0]["id"]
        )

    def test_schema_describes_actual_wishlist_responses(self):
        from drf_spectacular.generators import SchemaGenerator
        from drf_spectacular.validation import validate_schema

        schema = SchemaGenerator().get_schema(public=True)
        validate_schema(schema)
        list_schema = schema["paths"][self.list_url]["get"]["responses"]["200"]
        ids_schema = schema["paths"][self.ids_url]["get"]["responses"]["200"]
        self.assertIn("content", list_schema)
        self.assertIn("content", ids_schema)

        list_payload = list_schema["content"]["application/json"]["schema"]
        ids_payload = ids_schema["content"]["application/json"]["schema"]
        self.assertEqual(list_payload["$ref"], "#/components/schemas/WishlistPage")
        self.assertEqual(ids_payload["$ref"], "#/components/schemas/WishlistIds")
        components = schema["components"]["schemas"]
        self.assertEqual(
            set(components["WishlistPage"]["required"]),
            {"count", "next", "previous", "results"},
        )
        self.assertEqual(set(components["WishlistIds"]["required"]), {"product_ids"})
        self.assertEqual(
            set(components["Wishlist"]["required"]), {"id", "product", "created_at"}
        )
        self.assertEqual(components["Wishlist"]["properties"]["id"]["format"], "uuid")
        self.assertEqual(
            components["WishlistIds"]["properties"]["product_ids"]["items"]["format"],
            "uuid",
        )

    def test_response_envelopes_and_uuid_items_match_contract(self):
        self.client.post(
            self.list_url,
            {"product": str(self.product.id)},
            format="json",
            **self.headers(),
        )
        page = self.client.get(self.list_url, **self.headers()).json()
        ids = self.client.get(self.ids_url, **self.headers()).json()
        self.assertEqual(set(page), {"count", "next", "previous", "results"})
        self.assertEqual(set(ids), {"product_ids"})
        self.assertIsInstance(page["results"], list)
        self.assertIsInstance(uuid.UUID(page["results"][0]["id"]), uuid.UUID)
        self.assertEqual(page["results"][0]["product"]["id"], str(self.product.id))
        self.assertIsInstance(uuid.UUID(ids["product_ids"][0]), uuid.UUID)
        self.assertEqual(ids["product_ids"], [str(self.product.id)])


@skipIf(connection.vendor == "sqlite", "Concurrent wishlist test requires PostgreSQL")
class WishlistConcurrentApiTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = make_user("wishlist-concurrent@example.com")
        self.product = make_stocked_product(name="Concurrent favorite")
        self.url = "/api/catalog/wishlist/"

    def test_same_pair_concurrent_posts_are_idempotent(self):
        # A separate APIClient in each worker ensures independent request and DB
        # connections. The barrier makes both requests enter the view together.
        barrier = Barrier(2)
        token = str(auth_header(self.user)["HTTP_AUTHORIZATION"])

        def post_favorite():
            client = APIClient()
            try:
                barrier.wait(timeout=10)
                response = client.post(
                    self.url,
                    {"product": str(self.product.id)},
                    format="json",
                    HTTP_AUTHORIZATION=token,
                )
                return response.status_code
            finally:
                connection.close()

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(lambda _: post_favorite(), range(2)))

        self.assertCountEqual(statuses, [status.HTTP_201_CREATED, status.HTTP_200_OK])
        self.assertEqual(
            Wishlist.objects.filter(user=self.user, product=self.product).count(), 1
        )
