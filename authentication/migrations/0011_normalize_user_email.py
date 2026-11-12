from django.db import migrations
from django.db.models import Count
from django.db.models.functions import Lower, Trim


def normalize_user_emails(apps, schema_editor):
    """Salva os e-mails das contas em minúsculas e sem espaços (issue #97).

    Se duas contas só diferem em maiúsculas/minúsculas, a migração para sem
    mexer em nada: qual conta manter (e o que fazer com os pedidos da outra)
    é decisão do time, não da migração.
    """
    User = apps.get_model("authentication", "User")
    duplicated = list(
        User.objects.annotate(normalized=Lower(Trim("email")))
        .values("normalized")
        .annotate(total=Count("id"))
        .filter(total__gt=1)
        .values_list("normalized", flat=True)
    )
    if duplicated:
        raise RuntimeError(
            "Existem contas cujo e-mail só difere em maiúsculas/minúsculas. "
            "Resolva antes de migrar (issue #97): " + ", ".join(sorted(duplicated))
        )

    for user in User.objects.only("pk", "email"):
        normalized = (user.email or "").strip().lower()
        if user.email != normalized:
            User.objects.filter(pk=user.pk).update(email=normalized)


class Migration(migrations.Migration):
    dependencies = [
        ("authentication", "0010_normalize_profile_phone"),
    ]

    operations = [
        migrations.RunPython(normalize_user_emails, migrations.RunPython.noop),
    ]
