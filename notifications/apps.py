from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "notifications"
    verbose_name = "Notificações"

    def ready(self):
        from .backends import warn_if_email_disabled

        warn_if_email_disabled()
