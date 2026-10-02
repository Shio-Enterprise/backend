import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class ReviewStatus(models.TextChoices):
    PUBLISHED = "PUBLISHED", "Publicada"
    REMOVED = "REMOVED", "Removida"


class ReviewFit(models.TextChoices):
    SMALL = "SMALL", "Pequeno"
    TRUE_TO_SIZE = "TRUE_TO_SIZE", "Certo"
    LARGE = "LARGE", "Grande"


class RemovalReason(models.TextChoices):
    OFFENSIVE = "OFFENSIVE", "Linguagem ofensiva"
    SPAM = "SPAM", "Spam ou propaganda"
    PERSONAL_DATA = "PERSONAL_DATA", "Dados pessoais"
    OFF_TOPIC = "OFF_TOPIC", "Não fala do produto"
    OTHER = "OTHER", "Outro"


class ProductReview(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(
        "products.Product", on_delete=models.CASCADE, related_name="reviews"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="product_reviews",
    )
    # Prova da compra; o tamanho é copiado porque OrderItem.variation é SET_NULL.
    order_item = models.ForeignKey(
        "orders.OrderItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviews",
    )
    purchased_size = models.CharField(max_length=50, blank=True)
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    comment = models.TextField(max_length=1000, blank=True)
    fit = models.CharField(max_length=20, choices=ReviewFit.choices, blank=True)
    status = models.CharField(
        max_length=20, choices=ReviewStatus.choices, default=ReviewStatus.PUBLISHED
    )
    removal_reason = models.CharField(
        max_length=20, choices=RemovalReason.choices, blank=True
    )
    removal_note = models.TextField(max_length=500, blank=True)
    removed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    removed_at = models.DateTimeField(null=True, blank=True)
    admin_reply = models.TextField(max_length=1000, blank=True)
    admin_reply_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "user"], name="unique_review_per_user_product"
            ),
            models.CheckConstraint(
                check=models.Q(rating__gte=1, rating__lte=5),
                name="review_rating_between_1_and_5",
            ),
        ]
        indexes = [
            models.Index(
                fields=["product", "status", "-created_at"],
                name="review_product_status_idx",
            ),
        ]

    def __str__(self):
        return f"{self.product} - {self.rating}★ ({self.status})"
