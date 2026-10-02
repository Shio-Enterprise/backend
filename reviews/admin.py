from django.contrib import admin

from .models import ProductReview


@admin.register(ProductReview)
class ProductReviewAdmin(admin.ModelAdmin):
    """Somente leitura: escrever por aqui pularia o recálculo da média."""

    list_display = ("product", "user", "rating", "fit", "status", "created_at")
    list_filter = ("status", "rating", "fit")
    search_fields = ("product__name", "user__email", "user__name", "comment")
    list_select_related = ("product", "user")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
