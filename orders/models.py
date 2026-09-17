import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class OrderStatus(models.TextChoices):
    AWAITING_PAYMENT = "AWAITING_PAYMENT", "Awaiting Payment"
    PAID = "PAID", "Paid"
    PREPARING = "PREPARING", "Preparing"
    SHIPPED = "SHIPPED", "Shipped"
    DELIVERED = "DELIVERED", "Delivered"
    CANCELED = "CANCELED", "Canceled"


class PaymentMethod(models.TextChoices):
    PIX = "PIX", "Pix"
    CREDIT_CARD = "CREDIT_CARD", "Credit Card"
    BOLETO = "BOLETO", "Boleto"


class PaymentStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PROCESSING = "PROCESSING", "Processing"
    PAID = "PAID", "Paid"
    FAILED = "FAILED", "Failed"
    REFUNDED = "REFUNDED", "Refunded"


class Coupon(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=50, unique=True)
    discount_type = models.CharField(
        max_length=20,
        choices=[("PERCENTAGE", "Percentage"), ("FIXED_VALUE", "Fixed Value")],
    )
    discount_value = models.DecimalField(max_digits=10, decimal_places=2)
    expiration_date = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Cart(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="carts"
    )
    status = models.CharField(
        max_length=20,
        default="ACTIVE",
        choices=[
            ("ACTIVE", "Active"),
            ("FINISHED", "Finished"),
            ("ABANDONED", "Abandoned"),
        ],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class CartItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="items")
    variation = models.ForeignKey("products.ProductVariation", on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    updated_at = models.DateTimeField(auto_now=True)


class ShippingQuote(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="shipping_quotes",
    )
    cart = models.ForeignKey(
        Cart, on_delete=models.CASCADE, related_name="shipping_quotes"
    )
    address = models.ForeignKey(
        "authentication.Address",
        on_delete=models.CASCADE,
        related_name="shipping_quotes",
    )
    snapshot = models.JSONField()
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)
    shipping_cost = models.DecimalField(max_digits=10, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    prazo_dias = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    invalidated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(
                    subtotal__gte=0,
                    shipping_cost__gte=0,
                    discount_amount__gte=0,
                    total_amount__gte=0,
                ),
                name="shipping_quote_nonnegative_amounts",
            ),
        ]


class CustomerOrder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.RESTRICT, related_name="orders"
    )
    address = models.ForeignKey(
        "authentication.Address", on_delete=models.SET_NULL, null=True, blank=True
    )
    coupon = models.ForeignKey(Coupon, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(
        max_length=50, choices=OrderStatus.choices, default=OrderStatus.AWAITING_PAYMENT
    )

    subtotal = models.DecimalField(max_digits=10, decimal_places=2)
    shipping_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    tracking_code = models.CharField(max_length=100, null=True, blank=True)
    reservation_expires_at = models.DateTimeField(null=True, blank=True)

    shipping_zip_code = models.CharField(max_length=9)
    shipping_street = models.CharField(max_length=255)
    shipping_number = models.CharField(max_length=20)
    shipping_complement = models.CharField(max_length=100, null=True, blank=True)
    shipping_neighborhood = models.CharField(max_length=100)
    shipping_city = models.CharField(max_length=100)
    shipping_state = models.CharField(max_length=2)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class CheckoutAttemptStatus(models.TextChoices):
    PROCESSING = "PROCESSING", "Processing"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    UNCERTAIN = "UNCERTAIN", "Uncertain"


class CheckoutAttempt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    idempotency_key = models.UUIDField()
    cart = models.OneToOneField(
        Cart, on_delete=models.PROTECT, related_name="checkout_attempt"
    )
    shipping_quote = models.ForeignKey(ShippingQuote, on_delete=models.PROTECT)
    address_id = models.UUIDField()
    order = models.OneToOneField(
        CustomerOrder, on_delete=models.PROTECT, related_name="checkout_attempt"
    )
    status = models.CharField(
        max_length=20,
        choices=CheckoutAttemptStatus.choices,
        default=CheckoutAttemptStatus.PROCESSING,
    )
    checkout_url = models.URLField(max_length=2048, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "idempotency_key"],
                name="checkout_attempt_user_key_unique",
            ),
        ]


class OrderItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(
        CustomerOrder, on_delete=models.CASCADE, related_name="items"
    )
    variation = models.ForeignKey(
        "products.ProductVariation", on_delete=models.SET_NULL, null=True
    )
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    product_name = models.CharField(max_length=255)
    sku_snapshot = models.CharField(max_length=100, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            old = type(self).objects.get(pk=self.pk)
            if old.unit_price != self.unit_price:
                from django.core.exceptions import ValidationError

                raise ValidationError("Preço contratado é imutável.")
        super().save(*args, **kwargs)


class Payment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.OneToOneField(
        CustomerOrder, on_delete=models.CASCADE, related_name="payment"
    )
    method = models.CharField(max_length=50, choices=PaymentMethod.choices)
    status = models.CharField(
        max_length=50, choices=PaymentStatus.choices, default=PaymentStatus.PENDING
    )
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    installments = models.IntegerField(default=1)
    installment_value = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    gateway_transaction_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True
    )
    qrcode_pix = models.TextField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        # `paid_at` representa a primeira confirmação e não acompanha mudanças
        # posteriores de status (por exemplo, PAID -> REFUNDED).
        if self.status == PaymentStatus.PAID and self.paid_at is None:
            self.paid_at = timezone.now()
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = set(update_fields) | {"paid_at"}
        super().save(*args, **kwargs)


class OrderStatusLog(models.Model):
    order = models.ForeignKey(
        CustomerOrder,
        on_delete=models.CASCADE,
        related_name="status_logs",
    )
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="order_status_changes",
    )
    previous_status = models.CharField(max_length=50, choices=OrderStatus.choices)
    new_status = models.CharField(max_length=50, choices=OrderStatus.choices)
    tracking_code = models.CharField(max_length=100, null=True, blank=True)
    comment = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"OrderStatusLog(order={self.order_id}, from={self.previous_status}, to={self.new_status})"
