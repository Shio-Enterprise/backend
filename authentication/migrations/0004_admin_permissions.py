from django.db import migrations
from django.db.models import Q


PERMISSIONS = [
    ("access_admin_dashboard", "Acessar dashboard administrativo"),
    ("manage_catalog", "Gerenciar catálogo e estoque"),
    ("manage_drops", "Gerenciar drops"),
    ("manage_orders", "Gerenciar pedidos"),
    ("manage_customers", "Gerenciar clientes"),
    ("manage_admin_permissions", "Gerenciar permissões administrativas"),
]


def create_and_assign_permissions(apps, schema_editor):
    User = apps.get_model("authentication", "User")
    UserProfile = apps.get_model("authentication", "UserProfile")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    content_type, _ = ContentType.objects.get_or_create(
        app_label="authentication",
        model="user",
    )

    permission_ids = []
    for codename, name in PERMISSIONS:
        permission, _ = Permission.objects.get_or_create(
            content_type=content_type,
            codename=codename,
            defaults={"name": name},
        )
        if permission.name != name:
            permission.name = name
            permission.save(update_fields=["name"])
        permission_ids.append(permission.pk)

    admin_profile_user_ids = UserProfile.objects.filter(role="ADMIN").values_list(
        "user_id", flat=True
    )
    admins = User.objects.filter(
        Q(is_staff=True)
        | Q(is_superuser=True)
        | Q(pk__in=admin_profile_user_ids)
    ).distinct()

    through = User.user_permissions.through
    rows = [
        through(user_id=user.pk, permission_id=permission_id)
        for user in admins
        for permission_id in permission_ids
    ]
    through.objects.bulk_create(rows, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [
        ("authentication", "0003_newslettersubscriber"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="user",
            options={
                "ordering": ["-created_at"],
                "permissions": PERMISSIONS,
                "verbose_name": "Utilizador",
                "verbose_name_plural": "Utilizadores",
            },
        ),
        migrations.RunPython(create_and_assign_permissions, migrations.RunPython.noop),
    ]
