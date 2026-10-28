import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class SiteEventType(models.TextChoices):
    PAGE_VIEW = "PAGE_VIEW", "Visualização de página"
    PRODUCT_VIEW = "PRODUCT_VIEW", "Visualização de produto"
    ADD_TO_CART = "ADD_TO_CART", "Adicionado ao carrinho"
    REMOVE_FROM_CART = "REMOVE_FROM_CART", "Removido do carrinho"
    CHECKOUT_STARTED = "CHECKOUT_STARTED", "Início do checkout"
    PURCHASE = "PURCHASE", "Compra"


class SiteEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_type = models.CharField(max_length=30, choices=SiteEventType.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="site_events",
    )
    anonymous_id = models.CharField(max_length=64, null=True, blank=True)
    product = models.ForeignKey(
        "products.Product",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="site_events",
    )
    variation = models.ForeignKey(
        "products.ProductVariation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="site_events",
    )
    path = models.CharField(max_length=255, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["event_type", "occurred_at"]),
            models.Index(fields=["user", "occurred_at"]),
        ]

    def __str__(self):
        return f"{self.event_type} em {self.occurred_at:%d/%m/%Y %H:%M}"
