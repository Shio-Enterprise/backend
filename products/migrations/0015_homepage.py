import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("products", "0014_dropwaitlist_seen_at")]

    operations = [
        migrations.CreateModel(
            name="HomepageSettings",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "tagline",
                    models.CharField(
                        default="Deus primeiro. Negócios depois.", max_length=255
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="HomepageBanner",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("image_desktop", models.ImageField(upload_to="homepage/banners/")),
                (
                    "image_mobile",
                    models.ImageField(
                        blank=True, null=True, upload_to="homepage/banners/"
                    ),
                ),
                ("alt_text", models.CharField(max_length=255)),
                ("link", models.CharField(blank=True, max_length=500)),
                ("display_order", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["display_order", "created_at", "id"]},
        ),
    ]
