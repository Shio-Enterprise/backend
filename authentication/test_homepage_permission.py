from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.apps import apps
from importlib import import_module

from authentication.admin_permissions import ADMIN_PERMISSION_CODES
from authentication.permissions import user_has_admin_permission
from products.tests import make_user


class HomepagePermissionTests(TestCase):
    def test_homepage_permission_is_managed_and_specific(self):
        self.assertIn("manage_homepage", ADMIN_PERMISSION_CODES)
        admin = make_user("homepage-permission@example.com", is_staff=True)
        permission = Permission.objects.get(content_type__app_label="authentication", codename="manage_homepage")
        admin.user_permissions.set([permission])
        self.assertTrue(user_has_admin_permission(admin, "manage_homepage"))
        self.assertFalse(user_has_admin_permission(admin, "manage_orders"))

    def test_migration_grants_existing_full_admin_but_not_limited_admin(self):
        old_codes = (
            "access_admin_dashboard", "manage_catalog", "manage_drops",
            "manage_orders", "manage_customers", "manage_admin_permissions",
        )
        old_permissions = Permission.objects.filter(content_type__app_label="authentication", codename__in=old_codes)
        full = make_user("full-homepage@example.com", is_staff=True)
        limited = make_user("limited-homepage@example.com", is_staff=True)
        full.user_permissions.set(old_permissions)
        limited.user_permissions.set(old_permissions.filter(codename="manage_orders"))
        migration = import_module("authentication.migrations.0006_manage_homepage")
        migration.grant_existing_full_admins(apps, None)
        full.refresh_from_db()
        limited.refresh_from_db()
        self.assertTrue(user_has_admin_permission(full, "manage_homepage"))
        self.assertFalse(user_has_admin_permission(limited, "manage_homepage"))

    def test_migration_preserves_full_access_granted_through_group(self):
        old_codes = (
            "access_admin_dashboard", "manage_catalog", "manage_drops",
            "manage_orders", "manage_customers", "manage_admin_permissions",
        )
        group = Group.objects.create(name="Administradores completos")
        group.permissions.set(Permission.objects.filter(content_type__app_label="authentication", codename__in=old_codes))
        admin = make_user("group-homepage@example.com", is_staff=True)
        admin.user_permissions.clear()
        admin.groups.add(group)
        import_module("authentication.migrations.0006_manage_homepage").grant_existing_full_admins(apps, None)
        self.assertTrue(user_has_admin_permission(admin, "manage_homepage"))
