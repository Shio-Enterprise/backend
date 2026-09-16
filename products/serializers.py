import re
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import serializers

from .models import (
    Category,
    DropCampaign,
    Product,
    ProductImage,
    ProductVariation,
    StockMovement,
)
from .services import create_variation, move_stock, normalize_color, normalize_variation


class CatalogPageQuerySerializer(serializers.Serializer):
    page = serializers.IntegerField(min_value=1, required=False)
    page_size = serializers.IntegerField(min_value=1, required=False)


class ProductListQuerySerializer(CatalogPageQuerySerializer):
    """Valida a query antes de construir filtros ou executar a paginação."""

    is_active = serializers.BooleanField(required=False)
    category = serializers.SlugField(max_length=150, required=False)
    drop = serializers.UUIDField(required=False)
    search = serializers.CharField(required=False, allow_blank=True)
    size = serializers.CharField(max_length=50, required=False)
    color = serializers.ListField(
        child=serializers.CharField(max_length=100), required=False, allow_empty=False
    )
    min_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=0, required=False
    )
    max_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=0, required=False
    )
    ordering = serializers.ChoiceField(
        choices=("-created_at", "base_price", "-base_price", "-sales_count"),
        default="-created_at",
    )

    def validate_color(self, values):
        return [normalize_color(value) for value in values]

    def validate(self, attrs):
        minimum = attrs.get("min_price")
        maximum = attrs.get("max_price")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise serializers.ValidationError(
                {"max_price": "Deve ser maior ou igual a min_price."}
            )
        return attrs


class CatalogFilterOptionsSerializer(serializers.Serializer):
    min_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, allow_null=True
    )
    max_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, allow_null=True
    )
    sizes = serializers.ListField(child=serializers.CharField())
    colors = serializers.ListField(child=serializers.CharField())


class CategorySerializer(serializers.ModelSerializer):
    """Serializer de Category — usado em list, detail, create e update (PUT)."""

    class Meta:
        model = Category
        fields = ["id", "name", "slug", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]
        extra_kwargs = {
            "slug": {"required": False, "allow_blank": True},
        }

    def create(self, validated_data):
        if not validated_data.get("slug"):
            base = slugify(validated_data["name"])
            slug = base
            suffix = 2
            while Category.objects.filter(slug=slug).exists():
                slug = f"{base}-{suffix}"
                suffix += 1
            validated_data["slug"] = slug
        return super().create(validated_data)

    def update(self, instance, validated_data):
        new_name = validated_data.get("name", instance.name)
        slug_sent = bool(validated_data.get("slug"))
        if new_name != instance.name and not slug_sent:
            validated_data["slug"] = slugify(new_name)
        return super().update(instance, validated_data)


class ProductNestedSerializer(serializers.ModelSerializer):
    effective_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )
    is_promotion_active = serializers.BooleanField(read_only=True)
    """Resumo de Product usado dentro do detalhe de DropCampaign."""

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "base_price",
            "effective_price",
            "is_promotion_active",
            "promotional_price",
            "promo_start",
            "promo_end",
            "is_active",
        ]
        read_only_fields = fields


class DropCampaignSerializer(serializers.ModelSerializer):
    """Serializer de DropCampaign — list, create e update (PUT)."""

    class Meta:
        model = DropCampaign
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "is_public",
            "banner",
            "launch_date",
            "end_date",
            "max_quantity",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
        extra_kwargs = {
            "slug": {"required": False, "allow_blank": True},
        }

    def validate_banner(self, value):
        max_mb = 5
        if value and value.size > max_mb * 1024 * 1024:
            raise serializers.ValidationError(f"Banner não pode passar de {max_mb}MB.")
        return value

    def validate(self, attrs):
        launch = attrs.get("launch_date")
        end = attrs.get("end_date")
        if launch and end and end <= launch:
            raise serializers.ValidationError(
                {"end_date": "end_date deve ser posterior a launch_date."}
            )
        return attrs

    def create(self, validated_data):
        if not validated_data.get("slug"):
            base = slugify(validated_data["name"])
            slug = base
            suffix = 2
            while DropCampaign.objects.filter(slug=slug).exists():
                slug = f"{base}-{suffix}"
                suffix += 1
            validated_data["slug"] = slug
        return super().create(validated_data)

    def update(self, instance, validated_data):
        new_name = validated_data.get("name", instance.name)
        slug_sent = bool(validated_data.get("slug"))
        if new_name != instance.name and not slug_sent:
            validated_data["slug"] = slugify(new_name)

        new_banner = validated_data.get("banner", serializers.empty)
        replacing_banner = (
            new_banner is not serializers.empty
            and instance.banner
            and new_banner != instance.banner
        )
        if replacing_banner:
            instance.banner.delete(save=False)
        return super().update(instance, validated_data)


class DropCampaignDetailSerializer(DropCampaignSerializer):
    """Detalhe do drop com produtos aninhados (resumidos)."""

    products = ProductNestedSerializer(many=True, read_only=True)

    class Meta(DropCampaignSerializer.Meta):
        fields = DropCampaignSerializer.Meta.fields + ["products"]


# ─── Product / Variation / Image / StockMovement ──────────────────────────────


class StrictSerializer(serializers.ModelSerializer):
    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
        readonly = {
            key for key in data if key in self.fields and self.fields[key].read_only
        }
        if unknown or readonly:
            raise serializers.ValidationError(
                {
                    key: "Campo desconhecido ou somente leitura."
                    for key in unknown | readonly
                }
            )
        return super().to_internal_value(data)


class ProductVariationSerializer(StrictSerializer):
    class Meta:
        model = ProductVariation
        fields = [
            "id",
            "size",
            "color",
            "sku",
            "stock_quantity",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
        extra_kwargs = {
            "sku": {"required": False, "allow_blank": True, "validators": []},
            "size": {"required": False, "allow_blank": True},
        }
        validators = []

    def validate(self, attrs):
        if self.instance and "stock_quantity" in attrs:
            raise serializers.ValidationError(
                {"stock_quantity": "Use os movimentos de estoque."}
            )
        original = self.instance
        normalized = normalize_variation(
            {
                **(
                    {
                        "size": original.size,
                        "color": original.color,
                        "sku": original.sku,
                    }
                    if original
                    else {}
                ),
                **attrs,
            }
        )
        if original and normalized["sku"] != original.sku:
            raise serializers.ValidationError({"sku": "SKU é imutável."})
        if (
            normalized["sku"]
            and ProductVariation.objects.filter(sku__iexact=normalized["sku"])
            .exclude(pk=getattr(original, "pk", None))
            .exists()
        ):
            raise serializers.ValidationError({"sku": "SKU já cadastrado."})
        return normalized

    def create(self, validated_data):
        product = validated_data.pop("product")
        return create_variation(
            product, validated_data, getattr(self.context.get("request"), "user", None)
        )

    @transaction.atomic
    def update(self, instance, validated_data):
        Product.objects.select_for_update().get(pk=instance.product_id)
        instance = ProductVariation.objects.select_for_update().get(pk=instance.pk)
        siblings = instance.product.variations.exclude(pk=instance.pk)
        size, color = validated_data["size"], validated_data["color"]
        if (
            siblings.filter(size__iexact=size, color__iexact=color).exists()
            or (size == "Único" and not color and siblings.exists())
            or siblings.filter(size="Único", color="").exists()
        ):
            raise serializers.ValidationError(
                {"size": "Combinação repetida ou incompatível com a variação padrão."}
            )
        try:
            with transaction.atomic():
                return super().update(instance, validated_data)
        except IntegrityError:
            raise serializers.ValidationError(
                {"size": "Combinação já cadastrada."}
            ) from None


class ProductVariationInputSerializer(ProductVariationSerializer):
    class Meta(ProductVariationSerializer.Meta):
        fields = ["size", "color", "sku", "stock_quantity"]
        read_only_fields = []


class ProductImageSerializer(serializers.ModelSerializer):
    """Imagem de produto — usado em list, detail e response de upload."""

    class Meta:
        model = ProductImage
        fields = ["id", "image", "display_order", "created_at", "updated_at"]
        read_only_fields = ["id", "display_order", "created_at", "updated_at"]

    def validate_image(self, value):
        max_mb = 5
        if value and value.size > max_mb * 1024 * 1024:
            raise serializers.ValidationError(f"Imagem não pode passar de {max_mb}MB.")
        return value


class CategoryNestedSerializer(serializers.ModelSerializer):
    """Resumo de Category usado em ProductDetailSerializer."""

    class Meta:
        model = Category
        fields = ["id", "name", "slug"]
        read_only_fields = fields


class DropNestedSerializer(serializers.ModelSerializer):
    """Resumo de DropCampaign usado em ProductDetailSerializer."""

    class Meta:
        model = DropCampaign
        fields = ["id", "name", "slug"]
        read_only_fields = fields


PRICE_FIELDS = [
    "promotional_price",
    "promo_start",
    "promo_end",
    "effective_price",
    "is_promotion_active",
    "cost_price",
    "margin_amount",
    "margin_percent",
]


class ProductPricingSerializer(serializers.ModelSerializer):
    effective_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )
    is_promotion_active = serializers.BooleanField(read_only=True)
    margin_amount = serializers.DecimalField(
        max_digits=11, decimal_places=2, read_only=True, allow_null=True
    )
    margin_percent = serializers.DecimalField(
        max_digits=18, decimal_places=2, read_only=True, allow_null=True
    )

    def to_representation(self, instance):
        data = super().to_representation(instance)
        at = self.context.setdefault("price_at", timezone.now())
        price = instance.price_at(at)
        data["effective_price"] = str(price)
        data["is_promotion_active"] = instance.promotion_active_at(at)
        request = self.context.get("request")
        if not request or not getattr(request.user, "is_admin", False):
            for key in ("cost_price", "margin_amount", "margin_percent"):
                data.pop(key, None)
        else:
            margin = (
                None if instance.cost_price is None else price - instance.cost_price
            )
            data["margin_amount"] = None if margin is None else str(margin)
            data["margin_percent"] = (
                None
                if margin is None or not price
                else str(
                    (margin * 100 / price).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                )
            )
        return data


class ProductListSerializer(ProductPricingSerializer):
    """Versão enxuta de Product para listagem pública."""

    variations = ProductVariationSerializer(many=True, read_only=True)
    images = ProductImageSerializer(many=True, read_only=True)
    category_details = CategoryNestedSerializer(source="category", read_only=True)
    drop_details = DropNestedSerializer(source="drop", read_only=True)

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "base_price",
            *PRICE_FIELDS,
            "is_active",
            "category",
            "drop",
            "category_details",
            "drop_details",
            "variations",
            "images",
            "created_at",
        ]
        read_only_fields = fields


class ProductDetailSerializer(ProductPricingSerializer):
    """Detalhe completo com variations, images e relações expandidas."""

    variations = ProductVariationSerializer(many=True, read_only=True)
    images = ProductImageSerializer(many=True, read_only=True)
    category = CategoryNestedSerializer(read_only=True)
    drop = DropNestedSerializer(read_only=True)

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "description",
            "base_price",
            *PRICE_FIELDS,
            "is_active",
            "category",
            "drop",
            "variations",
            "images",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class ProductWriteSerializer(StrictSerializer):
    """Create/update de Product com variations opcionais aninhadas no create."""

    cost_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=0, required=True
    )
    variations = ProductVariationInputSerializer(many=True, required=False)

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "description",
            "base_price",
            "cost_price",
            "promotional_price",
            "promo_start",
            "promo_end",
            "is_active",
            "category",
            "drop",
            "variations",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
        extra_kwargs = {"description": {"allow_blank": True, "required": False}}

    def validate(self, attrs):
        def value(key):
            return attrs.get(key, getattr(self.instance, key, None))

        if value("cost_price") is None:
            raise serializers.ValidationError(
                {"cost_price": "Informe o custo unitário."}
            )
        if self.instance and "variations" in attrs:
            raise serializers.ValidationError(
                {"variations": "Use o endpoint de variações."}
            )
        if "promotional_price" in attrs and attrs["promotional_price"] is None:
            attrs["promo_start"] = attrs["promo_end"] = None
        promo, start, end = (
            value("promotional_price"),
            value("promo_start"),
            value("promo_end"),
        )
        if promo is not None:
            if promo <= 0 or promo >= value("base_price"):
                raise serializers.ValidationError(
                    {"promotional_price": "Deve ser positivo e inferior ao preço base."}
                )
            if not start or not end or end <= start:
                raise serializers.ValidationError(
                    {"promo_end": "Informe início e fim, com fim posterior ao início."}
                )
        elif start or end:
            raise serializers.ValidationError(
                {"promotional_price": "Datas exigem preço promocional."}
            )
        for key in ("promo_start", "promo_end"):
            raw = self.initial_data.get(key)
            if (
                raw
                and isinstance(raw, str)
                and not (raw.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", raw))
            ):
                raise serializers.ValidationError({key: "Informe o fuso horário."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        variations = validated_data.pop("variations", []) or [{}]
        product = Product.objects.create(**validated_data)
        for index, var in enumerate(variations):
            try:
                create_variation(
                    product, var, getattr(self.context.get("request"), "user", None)
                )
            except serializers.ValidationError as exc:
                raise serializers.ValidationError(
                    {"variations": {index: exc.detail}}
                ) from exc
        return product


class StockMovementSerializer(StrictSerializer):
    """Movimentação de estoque — read e write."""

    new_stock = serializers.SerializerMethodField()

    class Meta:
        model = StockMovement
        fields = [
            "id",
            "kind",
            "reason",
            "quantity",
            "note",
            "created_by",
            "created_at",
            "new_stock",
            "balance_after",
            "sequence",
            "is_legacy",
            "origin_type",
            "origin_id",
            "order_item",
            "idempotency_key",
            "reverses_movement",
        ]
        read_only_fields = [
            "id",
            "created_by",
            "created_at",
            "new_stock",
            "balance_after",
            "sequence",
            "is_legacy",
            "origin_type",
            "origin_id",
            "order_item",
        ]
        extra_kwargs = {
            "idempotency_key": {"validators": []},
            "reverses_movement": {"validators": []},
        }

    def get_new_stock(self, obj) -> int | None:
        return obj.balance_after

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantidade deve ser maior que zero.")
        return value

    def validate(self, attrs):
        if not attrs.get("note", "").strip():
            raise serializers.ValidationError({"note": "Informe a justificativa."})
        if attrs.get("reason") in ("VENDA", "ESTOQUE_INICIAL"):
            raise serializers.ValidationError(
                {"reason": "Motivo reservado para operações automáticas."}
            )
        return attrs

    def create(self, validated_data):
        import uuid

        key = validated_data["idempotency_key"]
        return move_stock(
            **validated_data,
            origin_type="MANUAL_ADJUSTMENT",
            origin_id=uuid.uuid5(uuid.NAMESPACE_URL, key),
        )


class DuplicateVariationSerializer(serializers.Serializer):
    source_id = serializers.UUIDField()
    sku = serializers.CharField(required=False, allow_blank=True, max_length=100)
    stock_quantity = serializers.IntegerField(required=False, default=0, min_value=0)

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("Informe uma variação.")
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                {k: "Campo desconhecido." for k in unknown}
            )
        return super().to_internal_value(data)


class ProductDuplicateSerializer(serializers.Serializer):
    name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    cost_price = serializers.DecimalField(
        required=False, allow_null=True, max_digits=10, decimal_places=2, min_value=0
    )
    variations = DuplicateVariationSerializer(many=True)

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("Informe os dados da cópia.")
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                {k: "Campo desconhecido." for k in unknown}
            )
        return super().to_internal_value(data)
