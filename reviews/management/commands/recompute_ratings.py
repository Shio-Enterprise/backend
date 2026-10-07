from django.core.management.base import BaseCommand

from products.models import Product
from reviews.services import recompute_product_rating


class Command(BaseCommand):
    help = "Recalcula rating_avg e rating_count de todos os produtos."

    def handle(self, *args, **options):
        total = 0
        for product in Product.objects.only("pk").iterator():
            recompute_product_rating(product)
            total += 1
        self.stdout.write(self.style.SUCCESS(f"{total} produtos recalculados."))
