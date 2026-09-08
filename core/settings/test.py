import tempfile

from .base import *  # noqa: F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Isolar uploads de teste do mediafiles/ real
MEDIA_ROOT = tempfile.mkdtemp(prefix="test-media-")

# Remove warning de chave muito curta do JWT nos testes
SECRET_KEY = "uma-chave-secreta-muito-longa-apenas-para-testes-jwt-validar"
SIMPLE_JWT["SIGNING_KEY"] = SECRET_KEY
