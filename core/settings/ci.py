"""PostgreSQL settings used by GitHub Actions CI."""

import os

from .test import *  # noqa: F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("CI_DB_NAME", "shio_ci"),
        "USER": os.environ.get("CI_DB_USER", "postgres"),
        "PASSWORD": os.environ.get("CI_DB_PASSWORD", "postgres"),
        "HOST": os.environ.get("CI_DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("CI_DB_PORT", "5432"),
    }
}
