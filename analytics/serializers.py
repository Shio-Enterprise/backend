from rest_framework import serializers

from products.models import Product, ProductVariation

from .models import SiteEventType


class SiteEventCreateSerializer(serializers.Serializer):
    event_type = serializers.ChoiceField(choices=SiteEventType.choices)
    anonymous_id = serializers.CharField(
        max_length=64, required=False, allow_blank=True, allow_null=True
    )
    product = serializers.PrimaryKeyRelatedField(
        queryset=Product.objects.all(), required=False, allow_null=True
    )
    variation = serializers.PrimaryKeyRelatedField(
        queryset=ProductVariation.objects.all(), required=False, allow_null=True
    )
    path = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate(self, attrs):
        request = self.context["request"]
        if not request.user.is_authenticated and not attrs.get("anonymous_id"):
            raise serializers.ValidationError(
                {
                    "anonymous_id": "Informe o identificador anônimo para visitantes não autenticados."
                }
            )
        return attrs
