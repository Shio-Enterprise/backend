"""Local-only wishlist browser validation; never use for deployment."""

from .test_postgres import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
CORS_ALLOWED_ORIGINS = ["http://127.0.0.1:5137", "http://localhost:5137"]
CORREIOS_MOCK_ENABLED = True
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
