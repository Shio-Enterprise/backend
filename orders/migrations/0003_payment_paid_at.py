from django.db import migrations, models
from django.db.models import F


def backfill_paid_at(apps, schema_editor):
    Payment = apps.get_model("orders", "Payment")
    Payment.objects.filter(status__in=["PAID", "REFUNDED"], paid_at__isnull=True).update(
        paid_at=F("updated_at")
    )


class Migration(migrations.Migration):
    dependencies = [("orders", "0002_orderstatuslog")]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="paid_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(backfill_paid_at, migrations.RunPython.noop),
    ]
