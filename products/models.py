import uuid
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


class Category(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150, unique=True)
    slug = models.SlugField(max_length=150, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Categories"

    def __str__(self):
        return self.name


class DropCampaign(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    description = models.TextField(blank=True)
    is_public = models.BooleanField(default=True, verbose_name="Drop público")
    banner = models.ImageField(
        upload_to="drops/banners/",
        null=True,
        blank=True,
        verbose_name="Banner da campanha",
        validators=[
            FileExtensionValidator(allowed_extensions=["jpg", "jpeg", "png", "webp"])
        ],
    )
    launch_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True)
    max_quantity = models.IntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    def delete(self, *args, **kwargs):
        if self.banner:
            self.banner.delete(save=False)
        return super().delete(*args, **kwargs)


class Product(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    drop = models.ForeignKey(
        DropCampaign,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
    )
    name = models.CharField(max_length=255)
    description = models.TextField()
    base_price = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(0)]
    )
    cost_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    promotional_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    promo_start = models.DateTimeField(null=True, blank=True)
    promo_end = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def promotion_active_at(self, at):
        return bool(
            self.promotional_price is not None
            and self.promo_start
            and self.promo_end
            and self.promo_start <= at < self.promo_end
        )

    def price_at(self, at):
        return (
            self.promotional_price if self.promotion_active_at(at) else self.base_price
        )

    @property
    def effective_price(self):
        return self.price_at(timezone.now())

    @property
    def is_promotion_active(self):
        return self.promotion_active_at(timezone.now())

    @property
    def margin_amount(self):
        return (
            None if self.cost_price is None else self.effective_price - self.cost_price
        )

    @property
    def margin_percent(self):
        price = self.effective_price
        if self.cost_price is None or not price:
            return None
        return ((price - self.cost_price) * 100 / price).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    def __str__(self):
        return self.name


class ProductVariation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="variations"
    )
    size = models.CharField(max_length=50)
    color = models.CharField(max_length=100, default="", blank=True)
    sku = models.CharField(max_length=100, unique=True)
    stock_quantity = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Lower("size"),
                Lower("color"),
                "product",
                name="unique_product_size_color",
            ),
            models.UniqueConstraint(Lower("sku"), name="unique_normalized_sku"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            old = type(self).objects.get(pk=self.pk)
            if old.sku != self.sku:
                raise ValidationError("SKU é imutável.")
            if old.stock_quantity != self.stock_quantity:
                raise ValidationError("Altere o estoque pelo ledger.")
        super().save(*args, **kwargs)

    def __str__(self):
        if self.color:
            return f"{self.product.name} - {self.size} / {self.color}"
        return f"{self.product.name} - {self.size}"


class ProductImage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="images"
    )
    image = models.ImageField(
        upload_to="products/images/",
        validators=[
            FileExtensionValidator(allowed_extensions=["jpg", "jpeg", "png", "webp"])
        ],
        verbose_name="Imagem do produto",
    )
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "created_at"]

    def __str__(self):
        return f"{self.product.name} #{self.display_order}"

    def delete(self, *args, **kwargs):
        if self.image:
            self.image.delete(save=False)
        return super().delete(*args, **kwargs)


class StockMovementKind(models.TextChoices):
    ENTRADA = "ENTRADA", "Entrada"
    SAIDA = "SAIDA", "Saída"


class StockMovementReason(models.TextChoices):
    ESTOQUE_INICIAL = "ESTOQUE_INICIAL", "Estoque inicial"
    COMPRA = "COMPRA", "Compra"
    DEVOLUCAO = "DEVOLUCAO", "Devolução"
    AJUSTE = "AJUSTE", "Ajuste"
    PERDA = "PERDA", "Perda/Avaria"
    VENDA = "VENDA", "Venda"
    OUTRO = "OUTRO", "Outro"


class ImmutableQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Registros de auditoria são imutáveis.")

    def delete(self):
        raise ValidationError("Registros de auditoria não podem ser excluídos.")

    def bulk_update(self, *args, **kwargs):
        raise ValidationError("Registros de auditoria são imutáveis.")


class StockMovement(models.Model):
    objects = ImmutableQuerySet.as_manager()
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    variation = models.ForeignKey(
        ProductVariation,
        on_delete=models.PROTECT,
        related_name="stock_movements",
    )
    kind = models.CharField(max_length=10, choices=StockMovementKind.choices)
    reason = models.CharField(max_length=20, choices=StockMovementReason.choices)
    quantity = models.PositiveIntegerField()
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_movements",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    balance_after = models.PositiveIntegerField(null=True, blank=True)
    sequence = models.PositiveIntegerField(null=True, blank=True)
    is_legacy = models.BooleanField(default=False)
    origin_type = models.CharField(
        max_length=30,
        choices=[
            (v, v) for v in ("INITIAL_STOCK", "MANUAL_ADJUSTMENT", "ORDER", "LEGACY")
        ],
    )
    origin_id = models.UUIDField()
    order_item = models.ForeignKey(
        "orders.OrderItem",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_movements",
    )
    idempotency_key = models.CharField(max_length=150, unique=True)
    reverses_movement = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="compensation",
    )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Movimentos de estoque são imutáveis.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Use um movimento compensatório.")

    class Meta:
        ordering = ["-created_at", "-sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["variation", "sequence"], name="unique_stock_sequence"
            ),
            models.CheckConstraint(
                check=models.Q(is_legacy=True)
                | (
                    models.Q(balance_after__isnull=False)
                    & models.Q(sequence__isnull=False)
                    & models.Q(quantity__gt=0)
                ),
                name="stock_audited_balance",
            ),
        ]

    def __str__(self):
        return f"{self.kind} {self.quantity} ({self.reason})"


class StockOpeningBalance(models.Model):
    objects = ImmutableQuerySet.as_manager()
    variation = models.OneToOneField(
        ProductVariation, on_delete=models.PROTECT, related_name="opening_balance"
    )
    balance = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Saldo de abertura é imutável.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Saldo de abertura é imutável.")
