from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("products", "0007_product_rating_avg_product_rating_count"),
        ("products", "0007_wishlist"),
    ]

    operations = []
