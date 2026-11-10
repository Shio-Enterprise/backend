from django.db import migrations
from django.db.models import Q

OLD_CODES = (
    "access_admin_dashboard",
    "manage_catalog",
    "manage_drops",
    "manage_orders",
    "manage_customers",
    "manage_admin_permissions",
)


def grant_existing_full_admins(apps, schema_editor):
    User = apps.get_model("authentication", "User")
    UserProfile = apps.get_model("authentication", "UserProfile")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    content_type = ContentType.objects.get(app_label="authentication", model="user")
    permission, _ = Permission.objects.get_or_create(
        content_type=content_type,
        codename="manage_homepage",
        defaults={"name": "Gerenciar página inicial"},
    )
    old_ids = set(
        Permission.objects.filter(
            content_type=content_type, codename__in=OLD_CODES
        ).values_list("id", flat=True)
    )
    profile_ids = UserProfile.objects.filter(role="ADMIN").values_list(
        "user_id", flat=True
    )
    for user in User.objects.filter(
        Q(is_staff=True) | Q(pk__in=profile_ids), is_superuser=False
    ).distinct():
        held = set(
            user.user_permissions.filter(id__in=old_ids).values_list("id", flat=True)
        )
        held.update(
            permission_id
            for permission_id in user.groups.values_list("permissions__id", flat=True)
            if permission_id in old_ids
        )
        if held == old_ids and len(old_ids) == len(OLD_CODES):
            user.user_permissions.add(permission)


class Migration(migrations.Migration):
    dependencies = [("authentication", "0007_newslettersend_status")]

    operations = [
        migrations.AlterModelOptions(
            name="user",
            options={
                "ordering": ["-created_at"],
                "permissions": [
                    ("access_admin_dashboard", "Acessar dashboard administrativo"),
                    ("manage_catalog", "Gerenciar catálogo e estoque"),
                    ("manage_drops", "Gerenciar drops"),
                    ("manage_homepage", "Gerenciar página inicial"),
                    ("manage_orders", "Gerenciar pedidos"),
                    ("manage_customers", "Gerenciar clientes"),
                    (
                        "manage_admin_permissions",
                        "Gerenciar permissões administrativas",
                    ),
                ],
                "verbose_name": "Utilizador",
                "verbose_name_plural": "Utilizadores",
            },
        ),
        migrations.RunPython(grant_existing_full_admins, migrations.RunPython.noop),
    ]
