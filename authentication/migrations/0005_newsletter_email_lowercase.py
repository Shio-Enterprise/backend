from django.db import migrations


def lowercase_emails(apps, schema_editor):
    Subscriber = apps.get_model("authentication", "NewsletterSubscriber")
    groups = {}
    for sub in Subscriber.objects.all().order_by("subscribed_at"):
        groups.setdefault(sub.email.strip().lower(), []).append(sub)

    for email, subs in groups.items():
        keeper, others = subs[0], subs[1:]
        blocked = [s.unsubscribed_at for s in subs if s.unsubscribed_at]
        consent = any(s.consent_lgpd for s in subs)
        for other in others:
            other.delete()
        keeper.email = email
        keeper.consent_lgpd = consent
        # Bloqueio vence: mantém a data de descadastro, se houver alguma.
        keeper.unsubscribed_at = min(blocked) if blocked else None
        keeper.save()


class Migration(migrations.Migration):
    dependencies = [
        ("authentication", "0004_admin_permissions"),
    ]

    operations = [
        migrations.RunPython(lowercase_emails, migrations.RunPython.noop),
    ]
