import os
import subprocess
import sys

from django.test import SimpleTestCase


class ProductionCacheConfigurationTests(SimpleTestCase):
    def load_production_settings(self, redis_url):
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "from core.settings import prod; "
                "assert prod.CACHES['correios']['BACKEND'] == "
                "'django.core.cache.backends.redis.RedisCache'; "
                "assert prod.CACHES['correios']['OPTIONS']['max_connections'] == 5",
            ],
            env={
                **os.environ,
                "REDIS_URL": redis_url,
                "DATABASE_URL": "postgresql://test:test@localhost/test",
            },
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_production_requires_shared_redis(self):
        result = self.load_production_settings("")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Production requires REDIS_URL", result.stderr)

    def test_production_accepts_redis_and_tls_redis_urls(self):
        for url in ("redis://localhost:6379", "rediss://localhost:6379"):
            with self.subTest(url=url):
                result = self.load_production_settings(url)
                self.assertEqual(result.returncode, 0, result.stderr)
