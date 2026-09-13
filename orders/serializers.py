from django.contrib.auth import get_user_model
from rest_framework import serializers

from products.models import ProductVariation

from .models import CustomerOrder, OrderItem, OrderStatusLog, Payment

User = get_user_model()


class DashboardRecentOrderSerializer(serializers.ModelSerializer):
    customer_name = serializers.SerializerMethodField()

    class Meta:
        model = CustomerOrder
        fields = ["id", "customer_name", "total_amount", "status", "created_at"]

    def get_customer_name(self, obj) -> str:
        return getattr(obj.user, "name", None) or obj.user.email


class DashboardLowStockSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = ProductVariation
        fields = ["id", "product_name", "size", "sku", "stock_quantity"]


class SimpleOrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = [
            "id",
            "product_name",
            "sku_snapshot",
            "quantity",
            "unit_price",
            "updated_at",
        ]


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
            "created_at",
        ]


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
    status_logs = OrderStatusLogSerializer(many=True, read_only=True)

    class Meta:
        model = CustomerOrder
        fields = [
            "id",
            "user",
            "status",
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


class CartRepresentationSerializer(serializers.Serializer):
    id = serializers.UUIDField(allow_null=True)
    items = CartItemRepresentationSerializer(many=True)
    subtotal = serializers.DecimalField(max_digits=10, decimal_places=2)


class CartItemAddSerializer(serializers.Serializer):
    variation_id = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=1)


class CartItemUpdateSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=1)
