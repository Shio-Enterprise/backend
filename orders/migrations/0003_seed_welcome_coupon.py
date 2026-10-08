from django.db import migrations


def seed_welcome_coupon(apps, schema_editor):
    Coupon = apps.get_model("orders", "Coupon")
    Coupon.objects.get_or_create(
        code="BEMVINDO10",
        defaults={
            "discount_type": "PERCENTAGE",
            "discount_value": 10,
            "is_active": True,
            "expiration_date": None,
        },
    )


def remove_welcome_coupon(apps, schema_editor):
    Coupon = apps.get_model("orders", "Coupon")
    Coupon.objects.filter(code="BEMVINDO10").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0002_orderstatuslog"),
    ]

    operations = [
        migrations.RunPython(seed_welcome_coupon, remove_welcome_coupon),
    ]
