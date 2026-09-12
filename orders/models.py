import uuid

from django.conf import settings
from django.db import models


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

    shipping_zip_code = models.CharField(max_length=9)
    shipping_street = models.CharField(max_length=255)
    shipping_number = models.CharField(max_length=20)
    shipping_complement = models.CharField(max_length=100, null=True, blank=True)
    shipping_neighborhood = models.CharField(max_length=100)
    shipping_city = models.CharField(max_length=100)
    shipping_state = models.CharField(max_length=2)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class StockReservationStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    CONVERTED = "CONVERTED", "Converted"
    RELEASED = "RELEASED", "Released"


class StockReservation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order_item = models.OneToOneField(
        OrderItem, on_delete=models.CASCADE, related_name="reservation"
    )
    status = models.CharField(
        max_length=20,
        choices=StockReservationStatus.choices,
        default=StockReservationStatus.ACTIVE,
    )
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


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
