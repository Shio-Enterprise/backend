import re

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import mail
from rest_framework import status
from rest_framework.test import APITestCase

from authentication.admin_permissions import ADMIN_PERMISSION_CODES
from authentication.models import UserProfile, UserRole

User = get_user_model()

ADMINS_URL = "/api/auth/admins/"
LOOKUP_URL = "/api/auth/admins/lookup/"


def admin_codes(user):
    return set(
        user.user_permissions.filter(
            content_type__app_label="authentication",
            codename__in=ADMIN_PERMISSION_CODES,
        ).values_list("codename", flat=True)
    )


class AdminAccountsTestBase(APITestCase):
    def setUp(self):
        self.perms = {
            p.codename: p
            for p in Permission.objects.filter(
                content_type__app_label="authentication",
                codename__in=ADMIN_PERMISSION_CODES,
            )
        }
        self.manager = self.make_admin("manager@example.com", "Manager")
        self.manager.user_permissions.set([self.perms["manage_admin_permissions"]])

        self.no_perm_admin = self.make_admin("ops@example.com", "Ops")
        self.no_perm_admin.user_permissions.set([self.perms["manage_orders"]])

        self.customer = User.objects.create_user(
            email="cliente@example.com", name="Cliente", password="senha-forte-123"
        )
        UserProfile.objects.create(user=self.customer, role=UserRole.CUSTOMER)

    def make_admin(self, email, name, **extra):
        user = User.objects.create_user(email=email, name=name, **extra)
        UserProfile.objects.create(user=user, role=UserRole.ADMIN)
        return user


class AdminAccountsAccessTests(AdminAccountsTestBase):
    def test_anonymous_gets_401(self):
        target = self.make_admin("alvo@example.com", "Alvo")
        payload = {"email": "x@example.com", "name": "X", "admin_permissions": []}

        self.assertEqual(
            self.client.post(ADMINS_URL, payload, format="json").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.client.get(LOOKUP_URL, {"email": "x@example.com"}).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.client.delete(f"{ADMINS_URL}{target.pk}/").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_customer_and_admin_without_permission_get_403(self):
        target = self.make_admin("alvo@example.com", "Alvo")
        payload = {
            "email": "novo@example.com",
            "name": "Novo",
            "admin_permissions": ["manage_catalog"],
        }
        for user in (self.customer, self.no_perm_admin):
            self.client.force_authenticate(user)
            with self.subTest(user=user.email):
                self.assertEqual(
                    self.client.post(ADMINS_URL, payload, format="json").status_code,
                    status.HTTP_403_FORBIDDEN,
                )
                self.assertEqual(
                    self.client.get(
                        LOOKUP_URL, {"email": "cliente@example.com"}
                    ).status_code,
                    status.HTTP_403_FORBIDDEN,
                )
                self.assertEqual(
                    self.client.delete(f"{ADMINS_URL}{target.pk}/").status_code,
                    status.HTTP_403_FORBIDDEN,
                )
        self.assertFalse(User.objects.filter(email="novo@example.com").exists())
        target.refresh_from_db()
        self.assertTrue(target.is_admin)


class AdminLookupTests(AdminAccountsTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.manager)

    def test_lookup_existing_customer(self):
        response = self.client.get(LOOKUP_URL, {"email": " CLIENTE@example.com "})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["exists"])
        self.assertEqual(response.data["email"], "cliente@example.com")
        self.assertEqual(response.data["name"], "Cliente")
        self.assertFalse(response.data["is_admin"])

    def test_lookup_existing_admin(self):
        response = self.client.get(LOOKUP_URL, {"email": "ops@example.com"})
        self.assertTrue(response.data["exists"])
        self.assertTrue(response.data["is_admin"])

    def test_lookup_unknown_email(self):
        response = self.client.get(LOOKUP_URL, {"email": "ninguem@example.com"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data, {"email": "ninguem@example.com", "exists": False}
        )

    def test_lookup_invalid_email(self):
        for value in ("", "nao-e-email"):
            with self.subTest(value=value):
                response = self.client.get(LOOKUP_URL, {"email": value})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class PromoteCustomerTests(AdminAccountsTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.manager)

    def test_promotes_existing_customer_with_chosen_permissions(self):
        response = self.client.post(
            ADMINS_URL,
            {
                "email": "Cliente@Example.com",
                "admin_permissions": ["manage_catalog", "manage_orders"],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["action"], "promoted")
        self.assertEqual(response.data["id"], self.customer.pk)
        self.assertEqual(
            set(response.data["admin_permissions"]),
            {"manage_catalog", "manage_orders"},
        )

        self.customer.refresh_from_db()
        self.assertTrue(self.customer.is_admin)
        self.assertFalse(self.customer.is_staff)
        self.assertEqual(self.customer.profile.role, UserRole.ADMIN)
        self.assertEqual(
            admin_codes(self.customer), {"manage_catalog", "manage_orders"}
        )
        # Promoção não troca senha nem manda convite.
        self.assertTrue(self.customer.check_password("senha-forte-123"))
        self.assertEqual(len(mail.outbox), 0)

    def test_promotes_customer_without_profile(self):
        user = User.objects.create_user(email="semperfil@example.com", name="Sem")
        response = self.client.post(
            ADMINS_URL,
            {"email": user.email, "admin_permissions": ["manage_drops"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertTrue(user.is_admin)
        self.assertEqual(admin_codes(user), {"manage_drops"})

    def test_promoted_customer_has_access_only_to_chosen_areas(self):
        self.client.post(
            ADMINS_URL,
            {"email": self.customer.email, "admin_permissions": ["manage_catalog"]},
            format="json",
        )

        # Busca de novo: cada request real carrega o usuário do banco.
        self.client.force_authenticate(User.objects.get(pk=self.customer.pk))
        catalog = self.client.post(
            "/api/catalog/categories/", {"name": "Bonés"}, format="json"
        )
        orders = self.client.get("/api/orders/admin/")
        self.assertEqual(catalog.status_code, status.HTTP_201_CREATED)
        self.assertEqual(orders.status_code, status.HTTP_403_FORBIDDEN)

    def test_rejects_email_that_is_already_admin(self):
        response = self.client.post(
            ADMINS_URL,
            {"email": "OPS@example.com", "admin_permissions": ["manage_catalog"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["email"], ["Este e-mail já pertence a um administrador."]
        )
        self.assertEqual(admin_codes(self.no_perm_admin), {"manage_orders"})

    def test_rejects_superuser_email(self):
        User.objects.create_superuser(
            email="root@example.com", name="Root", password="senha-forte-123"
        )
        response = self.client.post(
            ADMINS_URL,
            {"email": "root@example.com", "admin_permissions": ["manage_catalog"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Superusuários", response.data["email"][0])

    def test_rejects_inactive_account(self):
        self.customer.is_active = False
        self.customer.save()
        response = self.client.post(
            ADMINS_URL,
            {"email": self.customer.email, "admin_permissions": ["manage_catalog"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.is_admin)

    def test_requires_at_least_one_valid_permission(self):
        for perms in ([], ["nao_existe"]):
            with self.subTest(perms=perms):
                response = self.client.post(
                    ADMINS_URL,
                    {"email": self.customer.email, "admin_permissions": perms},
                    format="json",
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("admin_permissions", response.data)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.is_admin)


class CreateAdminAccountTests(AdminAccountsTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.manager)

    def create(self, **overrides):
        payload = {
            "email": "Nova.Admin@Example.com",
            "name": "Nova Admin",
            "admin_permissions": ["manage_catalog"],
        }
        payload.update(overrides)
        return self.client.post(ADMINS_URL, payload, format="json")

    def test_creates_new_admin_account_without_password(self):
        response = self.create()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["action"], "created")
        self.assertTrue(response.data["invite_sent"])
        self.assertEqual(response.data["email"], "nova.admin@example.com")
        self.assertEqual(response.data["admin_permissions"], ["manage_catalog"])
        self.assertNotIn("password", response.data)

        user = User.objects.get(email="nova.admin@example.com")
        self.assertEqual(user.name, "Nova Admin")
        self.assertTrue(user.is_admin)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(admin_codes(user), {"manage_catalog"})

    def test_new_admin_gets_set_password_email(self):
        self.create()

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["nova.admin@example.com"])
        self.assertIn("/reset-password/", message.body)
        self.assertIn("Manager", message.body)

    def test_invite_link_sets_password_and_permissions_work_on_first_login(self):
        self.create()
        link = re.search(r"/reset-password/([^/\s]+)/([^/\s]+)", mail.outbox[0].body)
        self.assertIsNotNone(link)
        uidb64, token = link.groups()

        self.client.force_authenticate(None)
        confirm = self.client.post(
            "/api/auth/password-reset-confirm/",
            {"uidb64": uidb64, "token": token, "new_password": "Senha-Nova-987"},
            format="json",
        )
        self.assertEqual(confirm.status_code, status.HTTP_200_OK)

        login = self.client.post(
            "/api/auth/login/",
            {"email": "nova.admin@example.com", "password": "Senha-Nova-987"},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        self.assertEqual(login.data["user"]["admin_permissions"], ["manage_catalog"])

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        catalog = self.client.post(
            "/api/catalog/categories/", {"name": "Meias"}, format="json"
        )
        orders = self.client.get("/api/orders/admin/")
        self.assertEqual(catalog.status_code, status.HTTP_201_CREATED)
        self.assertEqual(orders.status_code, status.HTTP_403_FORBIDDEN)

    def test_name_is_required_for_new_account(self):
        for name in (None, "", "   "):
            with self.subTest(name=name):
                payload = {
                    "email": "sem-nome@example.com",
                    "admin_permissions": ["manage_catalog"],
                }
                if name is not None:
                    payload["name"] = name
                response = self.client.post(ADMINS_URL, payload, format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("name", response.data)
        self.assertFalse(User.objects.filter(email="sem-nome@example.com").exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_invalid_email_is_rejected(self):
        response = self.create(email="nao-e-email")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data)

    def test_new_account_appears_in_admin_list(self):
        self.create()
        response = self.client.get(ADMINS_URL)
        emails = {row["email"] for row in response.data}
        self.assertIn("nova.admin@example.com", emails)


class RemoveAdminAccessTests(AdminAccountsTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.manager)

    def test_removes_admin_access(self):
        target = self.make_admin("alvo@example.com", "Alvo", is_staff=True)
        target.user_permissions.set([self.perms["manage_orders"]])

        response = self.client.delete(f"{ADMINS_URL}{target.pk}/")

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        target.refresh_from_db()
        self.assertFalse(target.is_admin)
        self.assertFalse(target.is_staff)
        self.assertTrue(target.is_active)
        self.assertEqual(target.profile.role, UserRole.CUSTOMER)
        self.assertEqual(admin_codes(target), set())

        self.client.force_authenticate(User.objects.get(pk=target.pk))
        self.assertEqual(
            self.client.get("/api/orders/admin/").status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_removed_account_leaves_admin_list_and_can_be_promoted_again(self):
        self.client.delete(f"{ADMINS_URL}{self.no_perm_admin.pk}/")
        emails = {row["email"] for row in self.client.get(ADMINS_URL).data}
        self.assertNotIn(self.no_perm_admin.email, emails)

        again = self.client.post(
            ADMINS_URL,
            {"email": self.no_perm_admin.email, "admin_permissions": ["manage_drops"]},
            format="json",
        )
        self.assertEqual(again.status_code, status.HTTP_200_OK)
        self.assertEqual(admin_codes(self.no_perm_admin), {"manage_drops"})

    def test_cannot_remove_own_access(self):
        response = self.client.delete(f"{ADMINS_URL}{self.manager.pk}/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.manager.refresh_from_db()
        self.assertTrue(self.manager.is_admin)

    def test_cannot_remove_superuser(self):
        root = User.objects.create_superuser(
            email="root@example.com", name="Root", password="senha-forte-123"
        )
        response = self.client.delete(f"{ADMINS_URL}{root.pk}/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        root.refresh_from_db()
        self.assertTrue(root.is_superuser)
        self.assertTrue(root.is_staff)

    def test_customer_id_returns_404(self):
        response = self.client.delete(f"{ADMINS_URL}{self.customer.pk}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
