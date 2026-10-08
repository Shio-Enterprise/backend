from rest_framework import permissions

from .admin_permissions import ADMIN_PERMISSION_SET


def user_has_admin_permission(user, codename):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if not getattr(user, "is_admin", False):
        return False
    if getattr(user, "is_superuser", False):
        return True

    managed_permissions = {
        perm.split(".", 1)[1]
        for perm in user.get_all_permissions()
        if perm.startswith("authentication.")
        and perm.split(".", 1)[1] in ADMIN_PERMISSION_SET
    }
    return codename in managed_permissions


class IsStaffOrSuperUser(permissions.BasePermission):
    """Permite acesso a qualquer conta administrativa."""

    message = "Acesso negado. Apenas administradores podem aceder a este recurso."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "is_admin", False)
        )


class AdminPermissionRequired(permissions.BasePermission):
    codename = None
    message = "Acesso negado. A sua conta não possui a permissão administrativa necessária."

    def has_permission(self, request, view):
        if not self.codename:
            return False
        return user_has_admin_permission(request.user, self.codename)


class CanAccessAdminDashboard(AdminPermissionRequired):
    codename = "access_admin_dashboard"


class CanManageCatalog(AdminPermissionRequired):
    codename = "manage_catalog"


class CanManageDrops(AdminPermissionRequired):
    codename = "manage_drops"


class CanManageOrders(AdminPermissionRequired):
    codename = "manage_orders"


class CanManageCustomers(AdminPermissionRequired):
    codename = "manage_customers"


class CanManageAdminPermissions(AdminPermissionRequired):
    codename = "manage_admin_permissions"
