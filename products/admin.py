from django.contrib import admin

from .models import (
    Category,
    DropCampaign,
    Product,
    ProductImage,
    ProductVariation,
    StockMovement,
)


class ProductVariationInline(admin.TabularInline):
    model = ProductVariation
    extra = 0
    readonly_fields = ("sku", "stock_quantity")

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    fields = ("size", "color", "sku", "stock_quantity")


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 0
    fields = ("image", "display_order")
    readonly_fields = ("display_order",)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "drop", "base_price", "is_active", "created_at")
    list_filter = ("is_active", "category", "drop")
    search_fields = ("name", "description")
    inlines = [ProductVariationInline, ProductImageInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ProductVariation)
class ProductVariationAdmin(admin.ModelAdmin):
    list_display = ("product", "size", "color", "sku", "stock_quantity")
    search_fields = ("sku", "product__name")
    list_filter = ("size", "color")
    readonly_fields = ("sku", "stock_quantity")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ProductImage)
class ProductImageAdmin(admin.ModelAdmin):
    list_display = ("product", "display_order", "created_at")
    list_filter = ("product",)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    prepopulated_fields = {"slug": ("name",)}


@admin.register(DropCampaign)
class DropCampaignAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "slug",
        "launch_date",
        "end_date",
        "is_public",
        "is_active",
    )
    list_filter = ("is_active", "is_public")
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = (
        "variation",
        "kind",
        "reason",
        "quantity",
        "created_by",
        "created_at",
    )
    list_filter = ("kind", "reason", "created_at")
    search_fields = ("variation__sku", "variation__product__name", "note")
    readonly_fields = (
        "variation",
        "kind",
        "reason",
        "quantity",
        "note",
        "created_by",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
