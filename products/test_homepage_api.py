from io import BytesIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APITestCase

from authentication.models import UserProfile, UserRole
from products.models import HomepageBanner
from products.tests import auth_header


def image_file(name="banner.png", size=(12, 12), format="PNG"):
    data = BytesIO()
    Image.new("RGB", size, "red").save(data, format=format)
    return SimpleUploadedFile(name, data.getvalue(), content_type="image/png")


def admin_with(code, email):
    user = get_user_model().objects.create_user(
        email=email, name="Admin", is_staff=True
    )
    UserProfile.objects.create(user=user, role=UserRole.ADMIN)
    user.user_permissions.set(
        Permission.objects.filter(
            content_type__app_label="authentication", codename=code
        )
    )
    return user


class HomepageApiTests(APITestCase):
    base = "/api/catalog/homepage/"

    def setUp(self):
        self.admin = admin_with("manage_homepage", "home@example.com")
        self.other = admin_with("manage_orders", "orders@example.com")

    def test_public_empty_and_configured(self):
        empty = self.client.get(self.base)
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.data["banners"], [])
        self.assertEqual(empty.data["tagline"], "Deus primeiro. Negócios depois.")
        self.assertEqual(empty["Cache-Control"], "no-store")
        changed = self.client.patch(
            self.base + "tagline/",
            {"tagline": "Novo drop"},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(self.client.get(self.base).data["tagline"], "Novo drop")

    def test_admin_endpoints_require_permission(self):
        for path, method, data in (
            ("admin/", "get", None),
            ("tagline/", "patch", {"tagline": "Teste"}),
            ("banners/", "post", {"alt_text": "Banner"}),
            ("banners/order/", "put", {"ids": []}),
        ):
            for headers, expected in (({}, 401), (auth_header(self.other), 403)):
                response = getattr(self.client, method)(
                    self.base + path, data, format="json", **headers
                )
                self.assertEqual(response.status_code, expected, (path, method))

    def test_create_order_edit_and_delete(self):
        def create(name):
            return self.client.post(
                self.base + "banners/",
                {
                    "image_desktop": image_file(name),
                    "alt_text": name,
                    "link": "/drop/novo",
                },
                format="multipart",
                **auth_header(self.admin),
            )

        first, second = create("one.png"), create("two.png")
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        first_id, second_id = first.data["id"], second.data["id"]
        self.assertEqual(
            [b["id"] for b in self.client.get(self.base).data["banners"]],
            [first_id, second_id],
        )
        reordered = self.client.put(
            self.base + "banners/order/",
            {"ids": [second_id, first_id]},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(reordered.status_code, 200)
        self.assertEqual(
            [b["id"] for b in self.client.get(self.base).data["banners"]],
            [second_id, first_id],
        )
        edited = self.client.patch(
            self.base + f"banners/{first_id}/",
            {"alt_text": "Editado", "link": ""},
            format="multipart",
            **auth_header(self.admin),
        )
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.data["link"], "")
        self.assertEqual(
            self.client.delete(
                self.base + f"banners/{second_id}/", **auth_header(self.admin)
            ).status_code,
            204,
        )
        self.assertEqual(HomepageBanner.objects.count(), 1)

    def test_mobile_image_can_be_removed_and_detail_is_protected(self):
        created = self.client.post(
            self.base + "banners/",
            {
                "image_desktop": image_file("desktop.png"),
                "image_mobile": image_file("mobile.png"),
                "alt_text": "Campanha",
            },
            format="multipart",
            **auth_header(self.admin),
        )
        self.assertEqual(created.status_code, 201)
        url = self.base + f"banners/{created.data['id']}/"
        self.assertEqual(
            self.client.patch(url, {"alt_text": "X"}, format="multipart").status_code,
            401,
        )
        self.assertEqual(
            self.client.delete(url, **auth_header(self.other)).status_code, 403
        )
        changed = self.client.patch(
            url,
            {"remove_image_mobile": "true"},
            format="multipart",
            **auth_header(self.admin),
        )
        self.assertEqual(changed.status_code, 200)
        self.assertIsNone(changed.data["image_mobile"])

    def test_invalid_upload_and_link_are_rejected(self):
        invalid = SimpleUploadedFile(
            "fake.png", b"not an image", content_type="image/png"
        )
        gif = image_file("banner.gif", format="GIF")
        for payload, field in (
            ({"image_desktop": invalid, "alt_text": "X"}, "image_desktop"),
            ({"image_desktop": gif, "alt_text": "X"}, "image_desktop"),
            (
                {
                    "image_desktop": image_file(),
                    "alt_text": "X",
                    "link": "//evil.example",
                },
                "link",
            ),
            (
                {
                    "image_desktop": image_file(),
                    "alt_text": "X",
                    "link": "javascript:alert(1)",
                },
                "link",
            ),
        ):
            response = self.client.post(
                self.base + "banners/",
                payload,
                format="multipart",
                **auth_header(self.admin),
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn(field, response.data)
        too_big = SimpleUploadedFile(
            "big.png", b"a" * (5 * 1024 * 1024 + 1), content_type="image/png"
        )
        response = self.client.post(
            self.base + "banners/",
            {"image_desktop": too_big, "alt_text": "X"},
            format="multipart",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("image_desktop", response.data)
        self.assertIn("5 MB", str(response.data["image_desktop"]))

    def test_invalid_order_does_not_change_existing_order(self):
        ids = []
        for name in ("one.png", "two.png"):
            response = self.client.post(
                self.base + "banners/",
                {"image_desktop": image_file(name), "alt_text": name},
                format="multipart",
                **auth_header(self.admin),
            )
            ids.append(response.data["id"])
        for bad in (
            [ids[0], ids[0]],
            [ids[0]],
            [ids[0], "00000000-0000-0000-0000-000000000000"],
        ):
            self.assertEqual(
                self.client.put(
                    self.base + "banners/order/",
                    {"ids": bad},
                    format="json",
                    **auth_header(self.admin),
                ).status_code,
                400,
            )
        self.assertEqual(
            [b["id"] for b in self.client.get(self.base).data["banners"]], ids
        )

    def test_replacing_and_deleting_banner_cleans_old_files_after_commit(self):
        created = self.client.post(
            self.base + "banners/",
            {
                "image_desktop": image_file("original.png"),
                "alt_text": "Original",
            },
            format="multipart",
            **auth_header(self.admin),
        )
        self.assertEqual(created.status_code, 201)
        banner = HomepageBanner.objects.get(pk=created.data["id"])
        old_name = banner.image_desktop.name
        storage = banner.image_desktop.storage
        self.assertTrue(storage.exists(old_name))
        url = self.base + f"banners/{banner.id}/"
        with self.captureOnCommitCallbacks(execute=True):
            changed = self.client.patch(
                url,
                {"image_desktop": image_file("replacement.png")},
                format="multipart",
                **auth_header(self.admin),
            )
        self.assertEqual(changed.status_code, 200)
        self.assertFalse(storage.exists(old_name))
        banner.refresh_from_db()
        new_name = banner.image_desktop.name
        with self.captureOnCommitCallbacks(execute=True):
            removed = self.client.delete(url, **auth_header(self.admin))
        self.assertEqual(removed.status_code, 204)
        self.assertFalse(storage.exists(new_name))
