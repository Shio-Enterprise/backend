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
    UNKNOWN = "UNKNOWN", "Not confirmed"
    PIX = "PIX", "Pix"
    CREDIT_CARD = "CREDIT_CARD", "Credit Card"
    BOLETO = "BOLETO", "Boleto"


class PaymentStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PROCESSING = "PROCESSING", "Processing"
    PAID = "PAID", "Paid"
    FAILED = "FAILED", "Failed"
    REFUNDED = "REFUNDED", "Refunded"


class CouponDiscountType(models.TextChoices):
    PERCENTAGE = "PERCENTAGE", "Percentage"
    FIXED_VALUE = "FIXED_VALUE", "Fixed Value"


def normalize_coupon_code(code):
    """Sem espaços e em maiúsculas: " verao 20" -> "VERAO20"."""
    return "".join(str(code).split()).upper()


class Coupon(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=50, unique=True)
    description = models.CharField(max_length=255, blank=True)
    discount_type = models.CharField(max_length=20, choices=CouponDiscountType.choices)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2)
    max_discount_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    # Comparado com o subtotal do carrinho inteiro, sem frete.
    min_order_value = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    starts_at = models.DateTimeField(null=True, blank=True)
    expiration_date = models.DateTimeField(null=True, blank=True)
    # null = ilimitado.
    max_uses_total = models.PositiveIntegerField(null=True, blank=True)
    max_uses_per_user = models.PositiveIntegerField(null=True, blank=True)
    first_purchase_only = models.BooleanField(default=False)
    auto_apply = models.BooleanField(default=False)
    # Sem drops nem categorias, o cupom vale para o carrinho inteiro.
    drops = models.ManyToManyField(
        "products.DropCampaign", blank=True, related_name="coupons"
    )
    categories = models.ManyToManyField(
        "products.Category", blank=True, related_name="coupons"
    )
    partner = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Cobre também update() e bulk_create(), que não chamam save().
            models.CheckConstraint(
                check=models.Q(code__regex=r"^[A-Z0-9_-]+$"),
                name="coupon_code_normalized",
            ),
            models.CheckConstraint(
                check=models.Q(discount_value__gt=0),
                name="coupon_discount_value_positive",
            ),
            models.CheckConstraint(
                check=~models.Q(discount_type=CouponDiscountType.PERCENTAGE)
                | models.Q(discount_value__lte=100),
                name="coupon_percentage_at_most_100",
            ),
            models.CheckConstraint(
                check=models.Q(max_discount_amount__isnull=True)
                | models.Q(
                    max_discount_amount__gt=0,
                    discount_type=CouponDiscountType.PERCENTAGE,
                ),
                name="coupon_max_discount_only_percentage",
            ),
            models.CheckConstraint(
                check=models.Q(min_order_value__isnull=True)
                | models.Q(min_order_value__gt=0),
                name="coupon_min_order_value_positive",
            ),
            models.CheckConstraint(
                check=models.Q(starts_at__isnull=True)
                | models.Q(expiration_date__isnull=True)
                | models.Q(starts_at__lt=models.F("expiration_date")),
                name="coupon_starts_before_expiration",
            ),
        ]

    def __str__(self):
        return self.code

    def save(self, *args, **kwargs):
        self.code = normalize_coupon_code(self.code)
        super().save(*args, **kwargs)


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
    coupon_code = models.CharField(max_length=50, blank=True, default="")
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
    # PROTECT: apagar um cupom usado apagaria a origem das vendas.
    coupon = models.ForeignKey(
        Coupon,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="orders",
    )
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
    last_error_code = models.CharField(max_length=64, blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)
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
    method = models.CharField(
        max_length=50, choices=PaymentMethod.choices, default=PaymentMethod.UNKNOWN
    )
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
    gateway_invoice_slug = models.CharField(max_length=255, blank=True)
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


class CouponEmailSend(models.Model):
    """Histórico de envios de um cupom por e-mail (um registro por campanha)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    coupon = models.ForeignKey(
        Coupon, on_delete=models.CASCADE, related_name="email_sends"
    )
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="coupon_email_sends",
    )
    sent_at = models.DateTimeField(auto_now_add=True)
    message = models.TextField(blank=True)
    sent_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-sent_at"]

    def __str__(self):
        return f"CouponEmailSend(coupon={self.coupon_id}, sent={self.sent_count}, failed={self.failed_count})"


class RefundStatus(models.TextChoices):
    REQUESTED = "REQUESTED", "Solicitado"
    APPROVED = "APPROVED", "Aprovado"
    REJECTED = "REJECTED", "Recusado"
    COMPLETED = "COMPLETED", "Reembolsado"


class RefundReason(models.TextChoices):
    CUSTOMER_REQUEST = "CUSTOMER_REQUEST", "Desistência do cliente"
    PRODUCT_ISSUE = "PRODUCT_ISSUE", "Problema com o produto"
    DELIVERY_ISSUE = "DELIVERY_ISSUE", "Problema na entrega"
    PAID_AFTER_CANCELLATION = "PAID_AFTER_CANCELLATION", "Pagamento após cancelamento"
    OTHER = "OTHER", "Outro"


class Refund(models.Model):
    """Reembolso integral de um pedido, do pedido do cliente ao estorno.

    O estorno é feito no painel da InfinitePay; aqui fica só o registro.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(
        CustomerOrder, on_delete=models.PROTECT, related_name="refunds"
    )
    status = models.CharField(max_length=20, choices=RefundStatus.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.CharField(max_length=30, choices=RefundReason.choices)
    customer_comment = models.CharField(max_length=1000, blank=True)
    # Visível ao cliente: instruções da aprovação ou justificativa da recusa.
    review_message = models.TextField(blank=True)
    internal_note = models.TextField(blank=True)
    gateway_reference = models.CharField(max_length=255, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Recusas ficam no histórico; só um reembolso vivo por pedido.
            models.UniqueConstraint(
                fields=["order"],
                condition=~models.Q(status=RefundStatus.REJECTED),
                name="refund_one_active_per_order",
            ),
            models.CheckConstraint(
                check=models.Q(amount__gt=0), name="refund_amount_positive"
            ),
            models.CheckConstraint(
                check=~models.Q(status=RefundStatus.COMPLETED)
                | (
                    models.Q(refunded_at__isnull=False)
                    & ~models.Q(gateway_reference="")
                ),
                name="refund_completed_has_gateway_data",
            ),
        ]

    def __str__(self):
        return f"Refund(order={self.order_id}, status={self.status})"


class ExchangeStatus(models.TextChoices):
    REQUESTED = "REQUESTED", "Solicitada"
    APPROVED = "APPROVED", "Aprovada"
    RECEIVED = "RECEIVED", "Recebida"
    COMPLETED = "COMPLETED", "Concluída"
    REJECTED = "REJECTED", "Recusada"
    CANCELED = "CANCELED", "Cancelada"


# Trocas que ainda seguram itens do pedido (e travam o reembolso).
EXCHANGE_OPEN_STATUSES = (
    ExchangeStatus.REQUESTED,
    ExchangeStatus.APPROVED,
    ExchangeStatus.RECEIVED,
)


class ExchangeReason(models.TextChoices):
    SIZE_OR_COLOR = "SIZE_OR_COLOR", "Tamanho ou cor"
    DEFECT = "DEFECT", "Defeito"


class ExchangeItemCondition(models.TextChoices):
    RESTOCK = "RESTOCK", "Volta ao estoque"
    LOSS = "LOSS", "Perda/avaria"


class ExchangeRequest(models.Model):
    """Troca de itens de um pedido entregue por outra variação do mesmo produto.

    As variações de um produto têm o mesmo preço: a troca não movimenta dinheiro.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(
        CustomerOrder, on_delete=models.PROTECT, related_name="exchanges"
    )
    status = models.CharField(max_length=20, choices=ExchangeStatus.choices)
    reason = models.CharField(max_length=20, choices=ExchangeReason.choices)
    customer_comment = models.CharField(max_length=1000, blank=True)
    # Visível ao cliente: instruções de envio, recusa ou cancelamento.
    review_message = models.TextField(blank=True)
    internal_note = models.TextField(blank=True)
    tracking_code = models.CharField(max_length=100, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    canceled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Recusadas, canceladas e concluídas ficam no histórico.
            models.UniqueConstraint(
                fields=["order"],
                condition=models.Q(status__in=EXCHANGE_OPEN_STATUSES),
                name="exchange_one_open_per_order",
            ),
            models.CheckConstraint(
                check=~models.Q(status=ExchangeStatus.COMPLETED)
                | ~models.Q(tracking_code=""),
                name="exchange_completed_has_tracking",
            ),
        ]

    def __str__(self):
        return f"ExchangeRequest(order={self.order_id}, status={self.status})"


class ExchangeItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    exchange = models.ForeignKey(
        ExchangeRequest, on_delete=models.CASCADE, related_name="items"
    )
    order_item = models.ForeignKey(
        OrderItem, on_delete=models.PROTECT, related_name="exchange_items"
    )
    replacement_variation = models.ForeignKey(
        "products.ProductVariation", on_delete=models.PROTECT, related_name="+"
    )
    quantity = models.PositiveIntegerField()
    # Preenchida ao receber a peça devolvida.
    condition = models.CharField(
        max_length=10, choices=ExchangeItemCondition.choices, blank=True
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["exchange", "order_item"], name="exchange_item_unique"
            ),
            models.CheckConstraint(
                check=models.Q(quantity__gt=0), name="exchange_item_quantity_positive"
            ),
        ]

    def __str__(self):
        return f"ExchangeItem(item={self.order_item_id}, qty={self.quantity})"
