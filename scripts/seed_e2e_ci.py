"""Idempotent E2E catalog and account fixtures for isolated CI PostgreSQL runs.

Run from backend/:
    python manage.py shell --settings=core.settings.e2e_ci < scripts/seed_e2e_ci.py
"""

import json
import os
from decimal import Decimal
from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction

from products.models import Category, DropCampaign, Product, ProductVariation

if settings.SETTINGS_MODULE != "core.settings.e2e_ci":
    raise RuntimeError("seed_e2e_ci.py must run only under core.settings.e2e_ci")

ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", "admin-e2e@shio.test")
ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "ShioE2EAdmin!2026")
USER_EMAIL = os.environ.get("E2E_USER_EMAIL", "cliente-e2e@shio.test")
USER_PASSWORD = os.environ.get("E2E_USER_PASSWORD", "ShioE2EUser!2026")

User = get_user_model()

with transaction.atomic():
    admin, admin_created = User.objects.get_or_create(
        email=ADMIN_EMAIL,
        defaults={
            "name": "Admin E2E",
            "is_staff": True,
            "is_superuser": True,
            "is_active": True,
            "is_new_user": False,
        },
    )
    if admin_created or not admin.check_password(ADMIN_PASSWORD):
        admin.is_staff = True
        admin.is_superuser = True
        admin.is_active = True
        admin.set_password(ADMIN_PASSWORD)
        admin.save()

    customer, customer_created = User.objects.get_or_create(
        email=USER_EMAIL,
        defaults={
            "name": "Cliente E2E",
            "is_staff": False,
            "is_superuser": False,
            "is_active": True,
            "is_new_user": False,
        },
    )
    if customer_created or not customer.check_password(USER_PASSWORD):
        customer.is_staff = False
        customer.is_superuser = False
        customer.is_active = True
        customer.set_password(USER_PASSWORD)
        customer.save()

    category, _ = Category.objects.update_or_create(
        slug="e2e-catalog",
        defaults={"name": "Camisetas E2E"},
    )
    drop, _ = DropCampaign.objects.update_or_create(
        slug="e2e-drop",
        defaults={
            "name": "Drop E2E Ativo",
            "description": "Drop deterministico para testes E2E.",
            "is_public": True,
            "is_active": True,
        },
    )

    seeded_products = [
        {
            "index": 1,
            "name": "Camiseta Shio E2E Tee",
            "price": Decimal("149.90"),
            "stock": 15,
            "sizes": ["P", "M", "G"],
        },
        {
            "index": 2,
            "name": "Moletom Shio E2E Oversized",
            "price": Decimal("299.90"),
            "stock": 8,
            "sizes": ["M", "G"],
        },
        {
            "index": 3,
            "name": "Jaqueta Shio E2E Esgotada",
            "price": Decimal("399.90"),
            "stock": 0,
            "sizes": ["M"],
        },
    ]

    for spec in seeded_products:
        product, _ = Product.objects.update_or_create(
            id=UUID(f"00000000-0000-4000-8000-{spec['index']:012d}"),
            defaults={
                "name": spec["name"],
                "description": f"Fixture deterministica E2E ({spec['name']}).",
                "category": category,
                "drop": drop,
                "base_price": spec["price"],
                "is_active": True,
            },
        )
        for size in spec["sizes"]:
            sku = f"SHIO-E2E-{spec['index']:02d}-{size}"
            ProductVariation.objects.get_or_create(
                sku=sku,
                defaults={
                    "product": product,
                    "size": size,
                    "color": "",
                    "stock_quantity": spec["stock"],
                    "is_active": True,
                },
            )

print(
    json.dumps(
        {
            "admin": ADMIN_EMAIL,
            "customer": USER_EMAIL,
            "products": len(seeded_products),
            "drop": drop.slug,
        }
    )
)
