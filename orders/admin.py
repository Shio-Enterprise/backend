from django.contrib import admin

from orders.models import CouponEmailSend


@admin.register(CouponEmailSend)
class CouponEmailSendAdmin(admin.ModelAdmin):
    list_display = ("coupon", "sent_by", "sent_at", "sent_count", "failed_count")
    readonly_fields = (
        "coupon",
        "sent_by",
        "sent_at",
        "message",
        "sent_count",
        "failed_count",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
