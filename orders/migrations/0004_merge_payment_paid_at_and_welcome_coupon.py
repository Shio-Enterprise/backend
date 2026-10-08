from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0003_payment_paid_at"),
        ("orders", "0003_seed_welcome_coupon"),
    ]

    operations = []
