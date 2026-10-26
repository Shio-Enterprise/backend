import os
import threading
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from django.core.cache.backends.redis import RedisCache
from django.test import SimpleTestCase, override_settings
from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import (
    ConnectionError,
    LockError,
    LockNotOwnedError,
    OutOfMemoryError,
)
from redis.retry import Retry

from orders.correios import (
    CorreiosAuthenticationError,
    get_valid_correios_access_token,
    request_new_correios_access_token,
)
from orders.integrations.correios_token_provider import CorreiosTokenProvider


def make_provider(url, namespace, loader):
    options = {
        "socket_connect_timeout": 1,
        "socket_timeout": 1,
        "max_connections": 5,
        "retry": Retry(NoBackoff(), 0),
    }
    cache = RedisCache(url, {"KEY_PREFIX": namespace, "OPTIONS": options})
    client = Redis.from_url(url, **options)
    return CorreiosTokenProvider(loader, cache_backend=cache, redis_client=client)


def read_token_in_worker(url, namespace):
    def load():
        client = Redis.from_url(url)
        number = client.incr(f"{namespace}:loads")
        client.close()
        time.sleep(0.1)
        return f"shared-token-{number}", 60

    provider = make_provider(url, namespace, load)
    try:
        return provider.get_token()
    finally:
        provider.redis.close()


@pytest.fixture
def redis_scope():
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL must point to a disposable Redis instance")
    namespace = f"o10-test:{uuid.uuid4().hex}"
    client = Redis.from_url(url)
    client.ping()
    try:
        yield url, namespace, client
    finally:
        keys = list(client.scan_iter(match=f"{namespace}:*"))
        if keys:
            client.delete(*keys)
        client.close()


def test_workers_share_one_refresh_and_cache_entries_expire(redis_scope):
    url, namespace, client = redis_scope
    with ProcessPoolExecutor(max_workers=4, mp_context=get_context("spawn")) as pool:
        futures = [pool.submit(read_token_in_worker, url, namespace) for _ in range(8)]
        tokens = [future.result(timeout=20) for future in futures]

    assert tokens == ["shared-token-1"] * 8
    assert client.get(f"{namespace}:loads") == b"1"
    keys = list(client.scan_iter(match=f"{namespace}:1:*"))
    assert len(keys) == 2
    assert all(0 < client.ttl(key) <= 60 for key in keys)


def test_stale_invalidation_cannot_delete_another_workers_token(redis_scope):
    url, namespace, _ = redis_scope
    old = make_provider(url, namespace, lambda: ("old-token", 60))
    new = make_provider(url, namespace, lambda: ("new-token", 60))
    try:
        assert old.get_token() == "old-token"
        assert new.refresh_token() == "new-token"
        assert not old.invalidate_if_current("old-token")
        assert old.get_token() == "new-token"
        assert new.invalidate_if_current("new-token")
        assert old.get_token() == "old-token"
    finally:
        old.redis.close()
        new.redis.close()


def test_cache_loss_regenerates_token_without_persisting_business_data(redis_scope):
    url, namespace, client = redis_scope
    loader = Mock(side_effect=[("first-token", 60), ("after-restart", 60)])
    provider = make_provider(url, namespace, loader)
    try:
        assert provider.get_token() == "first-token"
        client.delete(*client.scan_iter(match=f"{namespace}:*"))
        assert provider.get_token() == "after-restart"
        assert loader.call_count == 2
    finally:
        provider.redis.close()


def test_busy_refresh_lock_fails_without_calling_token_endpoint(redis_scope):
    url, namespace, _ = redis_scope
    loader = Mock()
    provider = make_provider(url, namespace, loader)
    held = provider.redis.lock(provider.cache.make_key("token_refresh_lock"), timeout=5)
    try:
        assert held.acquire(blocking=False)
        provider._refresh_lock.blocking_timeout = 0.05
        with pytest.raises(LockError):
            provider.get_token()
        loader.assert_not_called()
        assert held.owned()
    finally:
        if held.owned():
            held.release()
        provider.redis.close()


class RedisFailureTests(SimpleTestCase):
    @override_settings(
        CORREIOS_MOCK_ENABLED=False,
        CORREIOS_REMETENTE_CEP="70000000",
    )
    def test_redis_failure_returns_503_without_calling_correios(self):
        from rest_framework.test import APIRequestFactory, force_authenticate

        from orders.correios_views import ShippingOptionsView

        request = APIRequestFactory().get(
            "/api/orders/correios/frete/", {"cep_destino": "71000000"}
        )
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
        with (
            patch(
                "orders.correios.correios_token_provider.get_token",
                side_effect=ConnectionError("Redis unavailable"),
            ),
            patch("orders.correios.correios_client.request") as transport,
        ):
            response = ShippingOptionsView.as_view()(request)

        self.assertEqual(response.status_code, 503)
        transport.assert_not_called()

    def test_memory_limit_failure_releases_lock_and_rejects_refresh(self):
        cache = Mock()
        cache.set.side_effect = OutOfMemoryError("Cache memory limit reached")
        provider = CorreiosTokenProvider(lambda: ("token", 60), cache_backend=cache)
        with patch("orders.correios.correios_token_provider", provider):
            with self.assertRaises(CorreiosAuthenticationError):
                request_new_correios_access_token()
        self.assertFalse(provider._refresh_lock.locked())

    def test_cache_failure_does_not_fall_back_to_an_external_refresh(self):
        loader = Mock()
        cache = Mock()
        cache.get.side_effect = ConnectionError("Redis unavailable")
        provider = CorreiosTokenProvider(loader, cache_backend=cache)

        with patch("orders.correios.correios_token_provider", provider):
            with self.assertRaises(CorreiosAuthenticationError):
                get_valid_correios_access_token()
        loader.assert_not_called()

    def test_lost_lock_cannot_publish_the_loaded_token(self):
        cache = Mock()
        redis = MagicMock()
        lock = redis.lock.return_value
        lock.owned.return_value = False
        provider = CorreiosTokenProvider(
            lambda: ("stale-token", 60), cache_backend=cache, redis_client=redis
        )

        with self.assertRaises(LockNotOwnedError):
            provider.refresh_token()
        cache.set.assert_not_called()

    def test_loader_failure_releases_local_lock(self):
        loader = Mock(side_effect=RuntimeError("Token endpoint unavailable"))
        provider = CorreiosTokenProvider(loader, cache_backend=Mock())
        provider._refresh_lock = threading.Lock()

        with self.assertRaises(RuntimeError):
            provider.refresh_token()
        self.assertFalse(provider._refresh_lock.locked())
