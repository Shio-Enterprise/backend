import dj_database_url
from decouple import Csv, config

from .base import *

ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="", cast=Csv())


# django-cors-headers
# https://github.com/adamchainz/django-cors-headers

CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", default="", cast=Csv())
# CORS_ALLOW_ALL_ORIGINS = True

# SSL Redirect

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True

# Session cookie — SameSite=None permite enviar o cookie em requisições cross-origin
# (frontend Vercel → backend Railway). Secure=True é obrigatório quando SameSite=None.
SESSION_COOKIE_SAMESITE = "None"
SESSION_COOKIE_SECURE = True

# Database
# https://docs.djangoproject.com/en/4.2/ref/settings/#databases

DATABASES = {
    "default": dj_database_url.config(
        conn_max_age=600,
        conn_health_checks=True,
        ssl_require=False,
    ),
}

# Static files finders — garante que o admin (contrib) e apps instaladas
# sejam descobertos pelo collectstatic + WhiteNoise em produção.
# AppDirectoriesFinder: varre <app>/static/ de cada INSTALLED_APP
# FileSystemFinder:     varre os caminhos declarados em STATICFILES_DIRS
STATICFILES_FINDERS = [
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
]

# Cloudinary — usa armazenamento na nuvem apenas se credenciais estiverem preenchidas.
# Caso contrário cai no FileSystemStorage (útil em deploys sem Cloudinary ainda configurado).
_cloudinary_cloud_name = config("CLOUDINARY_CLOUD_NAME", default="")
_cloudinary_api_key = config("CLOUDINARY_API_KEY", default="")
_cloudinary_api_secret = config("CLOUDINARY_API_SECRET", default="")

_use_cloudinary = all([_cloudinary_cloud_name, _cloudinary_api_key, _cloudinary_api_secret])

if _use_cloudinary:
    _media_backend = "cloudinary_storage.storage.MediaCloudinaryStorage"
else:
    import warnings
    warnings.warn(
        "Cloudinary credentials not set. Falling back to FileSystemStorage for media. "
        "Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET in .env.",
        stacklevel=1,
    )
    _media_backend = "django.core.files.storage.FileSystemStorage"

STORAGES = {
    # TolerantManifestStaticFilesStorage resolve o bug do WhiteNoise com
    # @import url() relativo no CSS do Django Admin (forms.css → widgets.css).
    # Ver: core/storage.py
    "staticfiles": {
        "BACKEND": "core.storage.TolerantManifestStaticFilesStorage",
    },
    "default": {
        "BACKEND": _media_backend,
    },
}

# Don't store the original (un-hashed filename) version of static files, to reduce slug size:
# https://whitenoise.readthedocs.io/en/latest/django.html#WHITENOISE_KEEP_ONLY_HASHED_FILES
WHITENOISE_KEEP_ONLY_HASHED_FILES = True