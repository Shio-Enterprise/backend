from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from rest_framework import status
from rest_framework.test import APITestCase

from authentication.admin_permissions import ADMIN_PERMISSION_CODES
from authentication.models import UserProfile, UserRole

User = get_user_model()


class AdminPermissionManagementTests(APITestCase):
    def setUp(self):
        self.manager = User.objects.create_user(
            email="manager@example.com", name="Manager", is_staff=True
        )
        UserProfile.objects.create(user=self.manager, role=UserRole.ADMIN)
        self.target = User.objects.create_user(
            email="target@example.com", name="Target", is_staff=True
        )
        UserProfile.objects.create(user=self.target, role=UserRole.ADMIN)
        self.customer = User.objects.create_user(
            email="customer@example.com", name="Customer"
        )
        UserProfile.objects.create(user=self.customer, role=UserRole.CUSTOMER)

        self.permissions = {
            permission.codename: permission
            for permission in Permission.objects.filter(
                content_type__app_label="authentication",
                codename__in=ADMIN_PERMISSION_CODES,
            )
        }
        self.manager.user_permissions.set(
            [self.permissions["manage_admin_permissions"]]
        )
        self.target.user_permissions.clear()

    def test_customer_cannot_list_administrators(self):
        self.client.force_authenticate(self.customer)
        response = self.client.get("/api/auth/admins/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_manager_can_list_administrators_and_permission_options(self):
        self.client.force_authenticate(self.manager)

        admins = self.client.get("/api/auth/admins/")
        options = self.client.get("/api/auth/admins/permissions/")

        self.assertEqual(admins.status_code, status.HTTP_200_OK)
        self.assertEqual(options.status_code, status.HTTP_200_OK)
        admin_results = (
            admins.data.get("results", [])
            if isinstance(admins.data, dict)
            else admins.data
        )
        self.assertTrue(any(row["email"] == self.target.email for row in admin_results))
        codes = {row["code"] for row in options.data["results"]}
        self.assertEqual(codes, set(ADMIN_PERMISSION_CODES))

    def test_manager_can_replace_target_application_permissions(self):
        self.client.force_authenticate(self.manager)
        response = self.client.patch(
            f"/api/auth/admins/{self.target.pk}/",
            {"admin_permissions": ["manage_catalog", "manage_orders"]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            set(response.data["admin_permissions"]),
            {"manage_catalog", "manage_orders"},
        )
        self.target.refresh_from_db()
        target_codes = set(
            self.target.user_permissions.filter(
                content_type__app_label="authentication",
                codename__in=ADMIN_PERMISSION_CODES,
            ).values_list("codename", flat=True)
        )
        self.assertEqual(target_codes, {"manage_catalog", "manage_orders"})

    def test_cannot_save_admin_without_any_application_permission(self):
        self.client.force_authenticate(self.manager)
        response = self.client.patch(
            f"/api/auth/admins/{self.target.pk}/",
            {"admin_permissions": []},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_manager_cannot_remove_own_permission_management_access(self):
        self.client.force_authenticate(self.manager)
        response = self.client.patch(
            f"/api/auth/admins/{self.manager.pk}/",
            {"admin_permissions": ["manage_catalog"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_superuser_permissions_are_implicit_and_not_editable(self):
        superuser = User.objects.create_superuser(
            email="root@example.com", name="Root", password="strong-pass-123"
        )
        self.client.force_authenticate(self.manager)
        response = self.client.patch(
            f"/api/auth/admins/{superuser.pk}/",
            {"admin_permissions": ["manage_catalog"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_configured_permission_is_enforced_by_domain(self):
        self.target.user_permissions.add(self.permissions["manage_catalog"])
        self.client.force_authenticate(self.target)

        catalog = self.client.post(
            "/api/catalog/categories/", {"name": "Acessórios"}, format="json"
        )
        orders = self.client.get("/api/orders/admin/")
        crm = self.client.get("/api/auth/crm/customers/")

        self.assertEqual(catalog.status_code, status.HTTP_201_CREATED)
        self.assertEqual(orders.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(crm.status_code, status.HTTP_403_FORBIDDEN)

    def test_me_exposes_effective_admin_permissions(self):
        self.client.force_authenticate(self.manager)
        response = self.client.get("/api/auth/me/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["admin_permissions"],
            ["manage_admin_permissions"],
        )

    def test_new_admin_receives_default_application_permissions(self):
        admin = User.objects.create_user(
            email="new-admin@example.com", name="New Admin", is_staff=True
        )
        UserProfile.objects.create(user=admin, role=UserRole.ADMIN)

        codes = set(
            admin.user_permissions.filter(
                content_type__app_label="authentication",
                codename__in=ADMIN_PERMISSION_CODES,
            ).values_list("codename", flat=True)
        )
        self.assertEqual(codes, set(ADMIN_PERMISSION_CODES))

        self.client.force_authenticate(admin)
        response = self.client.get("/api/orders/admin/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
