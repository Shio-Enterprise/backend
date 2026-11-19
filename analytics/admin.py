from django.contrib import admin

from .models import AnalyticsConsent, SiteEvent


@admin.register(SiteEvent)
class SiteEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "user", "anonymous_id", "path", "occurred_at")
    list_filter = ("event_type",)
    search_fields = ("anonymous_id", "path", "user__email")
    readonly_fields = ("occurred_at",)


@admin.register(AnalyticsConsent)
class AnalyticsConsentAdmin(admin.ModelAdmin):
    """Histórico de consentimento, somente leitura."""

    list_display = ("choice", "version", "user", "anonymous_id", "created_at")
    list_filter = ("choice", "version")
    search_fields = ("anonymous_id", "user__email")
    readonly_fields = ("user", "anonymous_id", "choice", "version", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
