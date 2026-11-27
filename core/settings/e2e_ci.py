"""Isolated Django settings for CI browser E2E runs against PostgreSQL."""

from .ci import *  # noqa: F403

DEBUG = False
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
CORS_ALLOWED_ORIGINS = [
    "http://127.0.0.1:4173",
    "http://localhost:4173",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
]
CORREIOS_MOCK_ENABLED = True
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
STORAGES = {
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
}
