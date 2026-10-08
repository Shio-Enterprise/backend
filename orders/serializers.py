from django.contrib.auth import get_user_model
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from products.models import ProductVariation

from .models import (
    CustomerOrder,
    OrderItem,
    OrderStatus,
    OrderStatusLog,
    Payment,
    PaymentMethod,
    PaymentStatus,
)

User = get_user_model()


class DashboardRecentOrderSerializer(serializers.ModelSerializer):
    customer_name = serializers.SerializerMethodField()
    paid_at = serializers.DateTimeField(source="payment.paid_at", read_only=True)
    payment_status = serializers.CharField(source="payment.status", read_only=True)
    revenue_value = serializers.SerializerMethodField()

    class Meta:
        model = CustomerOrder
        fields = [
            "id",
            "customer_name",
            "total_amount",
            "status",
            "payment_status",
            "paid_at",
            "created_at",
            "revenue_value",
        ]

    def get_customer_name(self, obj) -> str:
        return getattr(obj.user, "name", None) or obj.user.email

    def get_revenue_value(self, obj) -> str:
        from .metrics import revenue_value

        return f"{revenue_value(obj):.2f}"


class DashboardLowStockSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = ProductVariation
        fields = ["id", "product_name", "size", "sku", "stock_quantity"]


class DashboardPeriodSerializer(serializers.Serializer):
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    granularity = serializers.ChoiceField(choices=["day", "month"])
    timezone = serializers.CharField()


class DashboardFinancialSerializer(serializers.Serializer):
    gross_revenue = serializers.DecimalField(max_digits=20, decimal_places=2)
    refunds = serializers.DecimalField(max_digits=20, decimal_places=2)
    net_revenue = serializers.DecimalField(max_digits=20, decimal_places=2)
    average_ticket = serializers.DecimalField(max_digits=20, decimal_places=2)
    valid_sales = serializers.IntegerField()


class DashboardSalesPointSerializer(serializers.Serializer):
    period = serializers.DateField()
    gross_revenue = serializers.DecimalField(max_digits=20, decimal_places=2)
    refunds = serializers.DecimalField(max_digits=20, decimal_places=2)
    net_revenue = serializers.DecimalField(max_digits=20, decimal_places=2)
    valid_sales = serializers.IntegerField()


class DashboardProductRankingRowSerializer(serializers.Serializer):
    product_id = serializers.UUIDField()
    product_name = serializers.CharField()
    units = serializers.IntegerField()
    revenue = serializers.DecimalField(max_digits=20, decimal_places=2)


class DashboardUnclassifiedItemsSerializer(serializers.Serializer):
    units = serializers.IntegerField()
    revenue = serializers.DecimalField(max_digits=20, decimal_places=2)


class DashboardProductRankingsSerializer(serializers.Serializer):
    by_units = DashboardProductRankingRowSerializer(many=True)
    by_revenue = DashboardProductRankingRowSerializer(many=True)
    unclassified = DashboardUnclassifiedItemsSerializer()


class DashboardDropRevenueRowSerializer(serializers.Serializer):
    drop_id = serializers.UUIDField(allow_null=True)
    name = serializers.CharField()
    units = serializers.IntegerField()
    revenue = serializers.DecimalField(max_digits=20, decimal_places=2)


class DashboardCategoryRevenueRowSerializer(serializers.Serializer):
    category_id = serializers.UUIDField(allow_null=True)
    name = serializers.CharField()
    units = serializers.IntegerField()
    revenue = serializers.DecimalField(max_digits=20, decimal_places=2)


class DashboardItemRevenueSerializer(serializers.Serializer):
    by_drop = DashboardDropRevenueRowSerializer(many=True)
    by_category = DashboardCategoryRevenueRowSerializer(many=True)
    basis = serializers.CharField()


class DashboardStatusRowSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=OrderStatus.choices)
    orders = serializers.IntegerField()


class DashboardOrdersByStatusSerializer(serializers.Serializer):
    date_basis = serializers.CharField()
    rows = DashboardStatusRowSerializer(many=True)


class DashboardPaymentMethodRowSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=PaymentMethod.choices)
    valid_sales = serializers.IntegerField()
    gross_revenue = serializers.DecimalField(max_digits=20, decimal_places=2)


class DashboardCustomersSerializer(serializers.Serializer):
    total_registered = serializers.IntegerField()
    new_in_period = serializers.IntegerField()
    recurring_customers = serializers.IntegerField()


class DashboardStockAttentionSerializer(serializers.Serializer):
    variation_id = serializers.UUIDField()
    product_id = serializers.UUIDField()
    product_name = serializers.CharField()
    size = serializers.CharField()
    color = serializers.CharField(allow_blank=True)
    sku = serializers.CharField()
    stock_quantity = serializers.IntegerField()
    admin_path = serializers.CharField()


class DashboardStockSerializer(serializers.Serializer):
    low_count = serializers.IntegerField()
    out_count = serializers.IntegerField()
    attention = DashboardStockAttentionSerializer(many=True)


class DashboardDetailSerializer(serializers.Serializer):
    period = DashboardPeriodSerializer()
    financial = DashboardFinancialSerializer()
    sales_series = DashboardSalesPointSerializer(many=True)
    product_rankings = DashboardProductRankingsSerializer()
    item_revenue = DashboardItemRevenueSerializer()
    orders_by_status = DashboardOrdersByStatusSerializer()
    sales_by_payment_method = DashboardPaymentMethodRowSerializer(many=True)
    customers = DashboardCustomersSerializer()
    stock = DashboardStockSerializer()


class DashboardOrderDrillDownSerializer(serializers.ModelSerializer):
    customer_name = serializers.SerializerMethodField()
    payment_status = serializers.CharField(
        source="payment.status", read_only=True, allow_null=True, default=None
    )
    payment_method = serializers.CharField(
        source="payment.method", read_only=True, allow_null=True, default=None
    )
    paid_at = serializers.DateTimeField(
        source="payment.paid_at", read_only=True, allow_null=True, default=None
    )
    revenue_value = serializers.SerializerMethodField()
    metric_units = serializers.SerializerMethodField()
    metric_item_revenue = serializers.SerializerMethodField()
    admin_path = serializers.SerializerMethodField()

    class Meta:
        model = CustomerOrder
        fields = [
            "id",
            "customer_name",
            "total_amount",
            "status",
            "payment_status",
            "payment_method",
            "paid_at",
            "created_at",
            "revenue_value",
            "metric_units",
            "metric_item_revenue",
            "admin_path",
        ]

    def get_customer_name(self, obj) -> str:
        return getattr(obj.user, "name", None) or obj.user.email

    @extend_schema_field(serializers.DecimalField(max_digits=20, decimal_places=2))
    def get_revenue_value(self, obj) -> str:
        payment = getattr(obj, "payment", None)
        if payment and payment.status == PaymentStatus.REFUNDED:
            return f"{-obj.total_amount:.2f}"
        if (
            payment
            and payment.status == PaymentStatus.PAID
            and obj.status == OrderStatus.DELIVERED
        ):
            return f"{obj.total_amount:.2f}"
        return "0.00"

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_metric_units(self, obj) -> int | None:
        return getattr(obj, "metric_units", None)

    @extend_schema_field(
        serializers.DecimalField(max_digits=20, decimal_places=2, allow_null=True)
    )
    def get_metric_item_revenue(self, obj) -> str | None:
        value = getattr(obj, "metric_item_revenue", None)
        return f"{value:.2f}" if value is not None else None

    def get_admin_path(self, obj) -> str:
        return f"/admin/orders/{obj.id}"


class DashboardOrderPageSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = DashboardOrderDrillDownSerializer(many=True)
    metric = serializers.CharField()
    date_basis = serializers.CharField()


class SimpleOrderItemSerializer(serializers.ModelSerializer):
    product_id = serializers.UUIDField(
        source="variation.product_id", read_only=True, allow_null=True
    )
    can_review = serializers.SerializerMethodField()
    review_id = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = [
            "id",
            "product_id",
            "product_name",
            "sku_snapshot",
            "quantity",
            "unit_price",
            "updated_at",
            "can_review",
            "review_id",
        ]

    def get_can_review(self, obj) -> bool:
        return (
            obj.order.status == OrderStatus.DELIVERED
            and obj.variation_id is not None
            and obj.variation.product.is_active
        )

    def get_review_id(self, obj) -> str | None:
        from reviews.models import ProductReview

        if obj.variation_id is None:
            return None
        review_id = (
            ProductReview.objects.filter(
                user_id=obj.order.user_id, product_id=obj.variation.product_id
            )
            .values_list("id", flat=True)
            .first()
        )
        return str(review_id) if review_id else None


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = [
            "id",
            "method",
            "status",
            "total_amount",
            "installments",
            "installment_value",
            "gateway_transaction_id",
            "qrcode_pix",
            "paid_at",
            "created_at",
        ]


class PaymentReturnSerializer(serializers.Serializer):
    order_nsu = serializers.UUIDField()


class PaymentWebhookSerializer(PaymentReturnSerializer):
    transaction_nsu = serializers.CharField(max_length=255)
    invoice_slug = serializers.CharField(max_length=255)


class AdminAddressSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    zip_code = serializers.CharField()
    street = serializers.CharField()
    address_number = serializers.CharField()
    complement = serializers.CharField(allow_null=True, allow_blank=True)
    neighborhood = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()


class AdminUserSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    email = serializers.EmailField()
    name = serializers.CharField(allow_null=True, allow_blank=True)


class OrderStatusLogSerializer(serializers.ModelSerializer):
    changed_by = AdminUserSerializer(read_only=True)

    class Meta:
        model = OrderStatusLog
        fields = [
            "id",
            "previous_status",
            "new_status",
            "tracking_code",
            "comment",
            "changed_by",
            "created_at",
        ]


class OrderDetailSerializer(serializers.ModelSerializer):
    items = SimpleOrderItemSerializer(many=True)
    payment = PaymentSerializer(read_only=True)
    user = AdminUserSerializer(read_only=True)
    status_logs = serializers.SerializerMethodField()

    def get_status_logs(self, obj):
        logs = obj.status_logs.order_by("created_at")

        return OrderStatusLogSerializer(
            logs,
            many=True,
        ).data

    class Meta:
        model = CustomerOrder
        fields = [
            "id",
            "user",
            "status",
            "reservation_expires_at",
            "subtotal",
            "shipping_cost",
            "discount_amount",
            "total_amount",
            "tracking_code",
            "shipping_zip_code",
            "shipping_street",
            "shipping_number",
            "shipping_complement",
            "shipping_neighborhood",
            "shipping_city",
            "shipping_state",
            "created_at",
            "updated_at",
            "items",
            "payment",
            "status_logs",
        ]


class OrderStatusUpdateSerializer(serializers.Serializer):
    physical_return_confirmed = serializers.BooleanField(required=False, default=False)
    status = serializers.ChoiceField(
        choices=CustomerOrder._meta.get_field("status").choices
    )
    tracking_code = serializers.CharField(allow_blank=True, required=False)
    comment = serializers.CharField(allow_blank=True, required=False)


class CartItemRepresentationSerializer(serializers.Serializer):
    base_price = serializers.DecimalField(max_digits=10, decimal_places=2)
    is_promotion_active = serializers.BooleanField()
    variation_id = serializers.UUIDField()
    product_id = serializers.UUIDField()
    product_name = serializers.CharField()
    size = serializers.CharField()
    sku = serializers.CharField()
    quantity = serializers.IntegerField()
    unit_price = serializers.DecimalField(max_digits=10, decimal_places=2)
    total_price = serializers.DecimalField(max_digits=10, decimal_places=2)
    stock_quantity = serializers.IntegerField()
    is_sellable = serializers.BooleanField()


class CartRepresentationSerializer(serializers.Serializer):
    id = serializers.UUIDField(allow_null=True)
    items = CartItemRepresentationSerializer(many=True)
    subtotal = serializers.DecimalField(max_digits=10, decimal_places=2)
    eligible_for_welcome_discount = serializers.BooleanField()
    welcome_discount_amount = serializers.DecimalField(max_digits=10, decimal_places=2)


class CartItemAddSerializer(serializers.Serializer):
    variation_id = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=1)


class CartItemUpdateSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=1)


class CheckoutCalculationInputSerializer(serializers.Serializer):
    address_id = serializers.UUIDField()

    def to_internal_value(self, data):
        if isinstance(data, dict):
            unexpected = set(data) - set(self.fields)
            if unexpected:
                raise serializers.ValidationError(
                    {
                        field: "Campo não aceito. Os valores são calculados pelo servidor."
                        for field in sorted(unexpected)
                    }
                )
        return super().to_internal_value(data)


class CheckoutInputSerializer(CheckoutCalculationInputSerializer):
    shipping_quote_id = serializers.UUIDField()
    idempotency_key = serializers.UUIDField()


class CheckoutQuoteItemSerializer(CartItemRepresentationSerializer):
    stock_quantity = serializers.IntegerField(required=False)


class CheckoutCalculationSerializer(serializers.Serializer):
    shipping_quote_id = serializers.UUIDField(source="id")
    expires_at = serializers.DateTimeField()
    address = serializers.DictField(source="snapshot.address")
    items = CheckoutQuoteItemSerializer(source="snapshot.items", many=True)
    prazo_dias = serializers.IntegerField(allow_null=True)
    subtotal = serializers.DecimalField(max_digits=10, decimal_places=2)
    shipping_cost = serializers.DecimalField(max_digits=10, decimal_places=2)
    discount_amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    total_amount = serializers.DecimalField(max_digits=10, decimal_places=2)
