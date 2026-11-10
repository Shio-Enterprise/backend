from pathlib import Path

from PIL import Image, UnidentifiedImageError
from rest_framework import serializers

from .models import HomepageBanner, HomepageSettings


def validate_homepage_image(value):
    if value.size > 5 * 1024 * 1024:
        raise serializers.ValidationError("A imagem não pode passar de 5 MB.")
    if Path(value.name).suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
        raise serializers.ValidationError("Use uma imagem JPEG, PNG ou WebP.")
    try:
        image = Image.open(value)
        image.verify()
        if image.format not in ("JPEG", "PNG", "WEBP"):
            raise serializers.ValidationError("Use uma imagem JPEG, PNG ou WebP.")
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise serializers.ValidationError("O arquivo não é uma imagem válida.") from exc
    finally:
        value.seek(0)
    return value


class HomepageImageField(serializers.ImageField):
    def to_internal_value(self, data):
        if getattr(data, "size", 0) > 5 * 1024 * 1024:
            raise serializers.ValidationError("A imagem não pode passar de 5 MB.")
        return super().to_internal_value(data)


class HomepageBannerSerializer(serializers.ModelSerializer):
    image_desktop = HomepageImageField()
    image_mobile = HomepageImageField(required=False, allow_null=True)
    remove_image_mobile = serializers.BooleanField(
        write_only=True, required=False, default=False
    )

    class Meta:
        model = HomepageBanner
        fields = (
            "id",
            "image_desktop",
            "image_mobile",
            "alt_text",
            "link",
            "display_order",
            "remove_image_mobile",
        )
        read_only_fields = ("id", "display_order")
        extra_kwargs = {"image_mobile": {"required": False, "allow_null": True}}

    def validate_image_desktop(self, value):
        return validate_homepage_image(value)

    def validate_image_mobile(self, value):
        return validate_homepage_image(value) if value else value

    def validate_alt_text(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Informe o texto alternativo.")
        return value

    def validate_link(self, value):
        value = value.strip()
        if value and (
            not value.startswith("/")
            or value.startswith("//")
            or "\\" in value
            or any(ord(char) < 32 for char in value)
        ):
            raise serializers.ValidationError(
                "Use um caminho interno da loja começando por /."
            )
        return value

    def validate(self, attrs):
        if attrs.get("remove_image_mobile") and attrs.get("image_mobile"):
            raise serializers.ValidationError(
                {"image_mobile": "Escolha uma nova imagem ou remova a atual."}
            )
        return attrs

    def create(self, validated_data):
        validated_data.pop("remove_image_mobile", None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        remove_mobile = validated_data.pop("remove_image_mobile", False)
        if remove_mobile:
            validated_data["image_mobile"] = None
        return super().update(instance, validated_data)


class HomepageTaglineSerializer(serializers.ModelSerializer):
    class Meta:
        model = HomepageSettings
        fields = ("tagline",)

    def validate_tagline(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Informe a frase da faixa.")
        return value


class HomepageOrderSerializer(serializers.Serializer):
    ids = serializers.ListField(child=serializers.UUIDField(), allow_empty=True)
