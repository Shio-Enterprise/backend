from django.utils.text import slugify
from rest_framework import serializers

from .availability import is_drop_sellable, is_drop_visible, is_product_sellable
from .models import (
    Category,
    DropCampaign,
    Product,
    ProductImage,
    ProductVariation,
    StockMovement,
    StockMovementKind,
)


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
    """Serializer de DropCampaign — list, create e update (PUT).

    `is_visible` e `is_sellable` são computados pela política única da issue
    #6 (products.availability) — o frontend deve ler esses campos em vez de
    reimplementar a lógica de datas/limite em JS.
    """

    is_visible = serializers.SerializerMethodField()
    is_sellable = serializers.SerializerMethodField()

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
            "is_visible",
            "is_sellable",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
        extra_kwargs = {
            "slug": {"required": False, "allow_blank": True},
        }

    def get_is_visible(self, obj) -> bool:
        return is_drop_visible(obj)

    def get_is_sellable(self, obj) -> bool:
        return is_drop_sellable(obj)

    def validate_banner(self, value):
        max_mb = 5
        if value and value.size > max_mb * 1024 * 1024:
            raise serializers.ValidationError(f"Banner não pode passar de {max_mb}MB.")
        return value

    def validate_max_quantity(self, value):
        if value is not None and value <= 0:
            raise serializers.ValidationError(
                "max_quantity deve ser um inteiro maior que zero, ou nulo (sem limite)."
            )
        return value

    def validate(self, attrs):
        # Em atualizações parciais, um campo de data ausente no payload deve
        # cair para o valor já persistido (não para None) antes de comparar.
        if self.instance is not None:
            launch = attrs.get("launch_date", self.instance.launch_date)
            end = attrs.get("end_date", self.instance.end_date)
        else:
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
        fields = ["id", "size", "color", "sku", "stock_quantity", "created_at", "updated_at"]
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
    """Versão enxuta de Product para listagem pública.

    `is_sellable` reflete a política da issue #6: um produto pode aparecer
    aqui (o backend já só lista produtos visíveis) e ainda assim não ser
    vendável — drop Rascunho, Programado, Encerrado ou Esgotado. O frontend
    deve usar este campo para desabilitar o botão de compra, não `is_active`.
    """

    variations = ProductVariationSerializer(many=True, read_only=True)
    images = ProductImageSerializer(many=True, read_only=True)
    is_sellable = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "base_price",
            "is_active",
            "is_sellable",
            "category",
            "drop",
            "variations",
            "images",
            "created_at",
        ]
        read_only_fields = fields

    def get_is_sellable(self, obj) -> bool:
        return is_product_sellable(obj)


class ProductDetailSerializer(serializers.ModelSerializer):
    """Detalhe completo com variations, images e relações expandidas."""

    variations = ProductVariationSerializer(many=True, read_only=True)
    images = ProductImageSerializer(many=True, read_only=True)
    category = CategoryNestedSerializer(read_only=True)
    drop = DropNestedSerializer(read_only=True)
    is_sellable = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "description",
            "base_price",
            "is_active",
            "is_sellable",
            "category",
            "drop",
            "variations",
            "images",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_is_sellable(self, obj) -> bool:
        return is_product_sellable(obj)


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
