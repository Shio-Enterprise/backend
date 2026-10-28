from django.contrib import admin

from .models import SiteEvent


@admin.register(SiteEvent)
class SiteEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "user", "anonymous_id", "path", "occurred_at")
    list_filter = ("event_type",)
    search_fields = ("anonymous_id", "path", "user__email")
    readonly_fields = ("occurred_at",)
