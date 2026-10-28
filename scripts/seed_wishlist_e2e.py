"""Create deterministic catalog fixtures only in the isolated wishlist E2E DB.

Run from backend/: python manage.py shell --settings=core.settings.wishlist_e2e
    < scripts/seed_wishlist_e2e.py
"""

import json
from decimal import Decimal
from uuid import UUID

from django.conf import settings
from django.db import transaction

from products.models import Category, DropCampaign, Product, ProductVariation

if (
    settings.SETTINGS_MODULE != "core.settings.wishlist_e2e"
    or settings.DATABASES["default"]["NAME"] != "wishlist_validation"
):
    raise RuntimeError("Use only core.settings.wishlist_e2e / wishlist_validation.")

with transaction.atomic():
    category, _ = Category.objects.get_or_create(
        slug="wishlist-e2e", defaults={"name": "Wishlist E2E"}
    )
    drop, _ = DropCampaign.objects.update_or_create(
        slug="wishlist-e2e-restored",
        defaults={
            "name": "Wishlist restored drop",
            "is_public": True,
            "is_active": True,
        },
    )
    for index in range(1, 16):
        product, _ = Product.objects.update_or_create(
            id=UUID(f"00000000-0000-4000-8000-{index:012d}"),
            defaults={
                "name": f"Wishlist E2E {index:02d}",
                "description": "Isolated wishlist validation fixture",
                "category": category,
                "base_price": Decimal("99.90"),
                "is_active": True,
                "drop": drop if index == 15 else None,
            },
        )
        ProductVariation.objects.update_or_create(
            product=product,
            size="M",
            color="",
            defaults={
                "sku": f"WISHLIST-E2E-{index:02d}",
                "stock_quantity": 0 if index == 14 else 10,
            },
        )

print(json.dumps({"products": 15, "out_of_stock": 14, "restored_drop": str(drop.id)}))
