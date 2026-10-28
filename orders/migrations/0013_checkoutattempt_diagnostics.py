from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0012_coupon_limits_scope_and_partner")]

    operations = [
        migrations.AddField(
            model_name="checkoutattempt",
            name="last_error_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="checkoutattempt",
            name="last_error_code",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
