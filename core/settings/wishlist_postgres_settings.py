"""Explicit isolated PostgreSQL settings for wishlist integration tests.

Every connection value is supplied through WISHLIST_TEST_DB_* variables so
this module cannot accidentally use the development or production database.
"""

import os

from .test import *  # noqa: F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("WISHLIST_TEST_DB_NAME", "wishlist_validation"),
        "USER": os.environ.get("WISHLIST_TEST_DB_USER", "wishlist_test"),
        "PASSWORD": os.environ.get("WISHLIST_TEST_DB_PASSWORD", "wishlist_local_only"),
        "HOST": os.environ.get("WISHLIST_TEST_DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("WISHLIST_TEST_DB_PORT", "55437"),
        "TEST": {
            "NAME": os.environ.get(
                "WISHLIST_TEST_DB_TEST_NAME", "test_wishlist_validation"
            )
        },
    }
}
