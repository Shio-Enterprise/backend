from collections.abc import Mapping

from rest_framework import serializers
from rest_framework.pagination import PageNumberPagination

from .models import ProductReview, RemovalReason, ReviewFit, ReviewStatus


class ReviewPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 50


class RejectUnknownFieldsMixin:
    """Mesmo contrato do StrictSerializer de products: campo extra -> 400."""

    def to_internal_value(self, data):
        if not isinstance(data, Mapping):
            return super().to_internal_value(data)
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                {key: "Campo desconhecido ou somente leitura." for key in unknown}
            )
        return super().to_internal_value(data)


# ─── Entrada ──────────────────────────────────────────────────────────────────


class ReviewWriteSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    rating = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(
        max_length=1000, required=False, allow_blank=True, trim_whitespace=True
    )
    fit = serializers.ChoiceField(
        choices=ReviewFit.choices, required=False, allow_blank=True
    )


class ReviewListQuerySerializer(serializers.Serializer):
    rating = serializers.IntegerField(min_value=1, max_value=5, required=False)
    page = serializers.IntegerField(min_value=1, required=False)
    page_size = serializers.IntegerField(min_value=1, required=False)


# ─── Saída ────────────────────────────────────────────────────────────────────


class ReviewPublicSerializer(serializers.ModelSerializer):
    author_name = serializers.CharField(source="user.get_full_name", read_only=True)
    verified_purchase = serializers.SerializerMethodField()

    class Meta:
        model = ProductReview
        fields = [
            "id",
            "rating",
            "comment",
            "fit",
            "author_name",
            "purchased_size",
            "verified_purchase",
            "admin_reply",
            "admin_reply_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_verified_purchase(self, obj) -> bool:
        # Toda avaliação nasce de um pedido entregue (regra do serviço).
        return True


class ReviewMineSerializer(ReviewPublicSerializer):
    product_id = serializers.UUIDField(read_only=True)
    product_name = serializers.CharField(source="product.name", read_only=True)
    removal_reason = serializers.SerializerMethodField()
    removal_reason_label = serializers.SerializerMethodField()

    class Meta(ReviewPublicSerializer.Meta):
        fields = ReviewPublicSerializer.Meta.fields + [
            "product_id",
            "product_name",
            "status",
            "removal_reason",
            "removal_reason_label",
            "removal_note",
            "removed_at",
        ]
        read_only_fields = fields

    def get_removal_reason(self, obj) -> str | None:
        return obj.removal_reason or None

    def get_removal_reason_label(self, obj) -> str | None:
        return obj.get_removal_reason_display() if obj.removal_reason else None


class EligibilitySerializer(serializers.Serializer):
    can_review = serializers.BooleanField()
    review_id = serializers.UUIDField(allow_null=True)


class RatingSummarySerializer(serializers.Serializer):
    rating_avg = serializers.DecimalField(max_digits=3, decimal_places=2)
    rating_count = serializers.IntegerField()
    distribution = serializers.DictField(child=serializers.IntegerField())
    fit = serializers.DictField(child=serializers.IntegerField())


class ReviewAdminSerializer(ReviewMineSerializer):
    author_email = serializers.EmailField(source="user.email", read_only=True)
    removed_by_name = serializers.SerializerMethodField()

    class Meta(ReviewMineSerializer.Meta):
        fields = ReviewMineSerializer.Meta.fields + ["author_email", "removed_by_name"]
        read_only_fields = fields

    def get_removed_by_name(self, obj) -> str | None:
        return obj.removed_by.get_full_name() if obj.removed_by else None


class AdminReviewListQuerySerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=ReviewStatus.choices, required=False)
    rating = serializers.IntegerField(min_value=1, max_value=5, required=False)
    product = serializers.UUIDField(required=False)
    page = serializers.IntegerField(min_value=1, required=False)
    page_size = serializers.IntegerField(min_value=1, required=False)


class RemovalInputSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    reason = serializers.ChoiceField(choices=RemovalReason.choices)
    note = serializers.CharField(
        max_length=500, required=False, allow_blank=True, default=""
    )

    def validate(self, attrs):
        if attrs["reason"] == RemovalReason.OTHER and not attrs["note"].strip():
            raise serializers.ValidationError(
                {"note": "Descreva o motivo quando escolher 'Outro'."}
            )
        return attrs


class ReplyInputSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    text = serializers.CharField(max_length=1000)
