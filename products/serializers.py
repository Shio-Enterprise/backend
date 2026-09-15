from django.utils.text import slugify
from rest_framework import serializers

from .models import (
    Category,
    DropCampaign,
    Product,
    ProductImage,
    ProductVariation,
    StockMovement,
    StockMovementKind,
)


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
    """Resumo de Product usado dentro do detalhe de DropCampaign."""

    class Meta:
        model = Product
        fields = ["id", "name", "base_price", "is_active"]
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


class ProductVariationSerializer(serializers.ModelSerializer):
    """Variação de produto — usado em list, detail, create e update."""

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


class ProductVariationInputSerializer(serializers.ModelSerializer):
    """Variação aninhada no payload de criação de Product."""

    class Meta:
        model = ProductVariation
        fields = ["size", "color", "sku", "stock_quantity"]


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


class ProductListSerializer(serializers.ModelSerializer):
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


class ProductDetailSerializer(serializers.ModelSerializer):
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
            "is_active",
            "category",
            "drop",
            "variations",
            "images",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class ProductWriteSerializer(serializers.ModelSerializer):
    """Create/update de Product com variations opcionais aninhadas no create."""

    variations = ProductVariationInputSerializer(many=True, required=False)

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "description",
            "base_price",
            "is_active",
            "category",
            "drop",
            "variations",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def create(self, validated_data):
        variations = validated_data.pop("variations", [])
        product = Product.objects.create(**validated_data)
        for var in variations:
            ProductVariation.objects.create(product=product, **var)
        return product

    def update(self, instance, validated_data):
        validated_data.pop("variations", None)
        return super().update(instance, validated_data)


class StockMovementSerializer(serializers.ModelSerializer):
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
        ]
        read_only_fields = ["id", "created_by", "created_at", "new_stock"]

    def get_new_stock(self, obj) -> int:
        return obj.variation.stock_quantity

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantidade deve ser maior que zero.")
        return value

    def validate(self, attrs):
        variation = self.context.get("variation")
        if attrs.get("kind") == StockMovementKind.SAIDA and variation is not None:
            if variation.stock_quantity < attrs["quantity"]:
                raise serializers.ValidationError(
                    {"quantity": "Saída supera o estoque atual."}
                )
        return attrs
