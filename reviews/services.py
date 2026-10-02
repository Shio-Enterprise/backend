from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Avg, Count

from products.models import Product

from .models import ProductReview, ReviewStatus

TWO_PLACES = Decimal("0.01")


def recompute_product_rating(product):
    """Único ponto que escreve rating_avg/rating_count no produto."""
    with transaction.atomic():
        locked = Product.objects.select_for_update().get(pk=product.pk)
        stats = ProductReview.objects.filter(
            product_id=locked.pk, status=ReviewStatus.PUBLISHED
        ).aggregate(avg=Avg("rating"), count=Count("id"))
        avg = stats["avg"]
        locked.rating_avg = (
            Decimal(str(avg)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
            if avg is not None
            else Decimal("0.00")
        )
        locked.rating_count = stats["count"]
        locked.save(update_fields=["rating_avg", "rating_count"])
    product.rating_avg = locked.rating_avg
    product.rating_count = locked.rating_count
    return product
