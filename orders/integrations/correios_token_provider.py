import hashlib
import threading

from django.conf import settings
from django.core.cache import caches
from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import LockNotOwnedError
from redis.retry import Retry


class CorreiosTokenProvider:
    """Cache access tokens without allowing a rejected old token to evict a new one."""

    CURRENT_KEY = "correios_access_token_current"
    TOKEN_KEY_PREFIX = "correios_access_token:"

    def __init__(self, token_loader, cache_backend=None, redis_client=None):
        self.token_loader = token_loader
        self.cache = cache_backend if cache_backend is not None else caches["correios"]
        self.redis = redis_client
        if self.redis is None and cache_backend is None and settings.REDIS_URL:
            self.redis = Redis.from_url(
                settings.REDIS_URL,
                socket_connect_timeout=2,
                socket_timeout=2,
                max_connections=5,
                retry=Retry(NoBackoff(), 0),
            )
        self._refresh_lock = (
            self.redis.lock(
                self.cache.make_key("token_refresh_lock"),
                timeout=30,
                blocking_timeout=12,
            )
            if self.redis is not None
            else threading.Lock()
        )

    @classmethod
    def _digest(cls, token):
        return hashlib.sha256(token.encode()).hexdigest()

    @classmethod
    def _token_key(cls, digest):
        return f"{cls.TOKEN_KEY_PREFIX}{digest}"

    def get_token(self):
        digest = self.cache.get(self.CURRENT_KEY)
        token = self.cache.get(self._token_key(digest)) if digest else None
        if token:
            return token

        with self._refresh_lock:
            digest = self.cache.get(self.CURRENT_KEY)
            token = self.cache.get(self._token_key(digest)) if digest else None
            if token:
                return token

            return self._load_and_cache_token()

    def refresh_token(self):
        with self._refresh_lock:
            return self._load_and_cache_token()

    def _load_and_cache_token(self):
        token, ttl = self.token_loader()
        if self.redis is not None and not self._refresh_lock.owned():
            raise LockNotOwnedError("Correios token refresh lock expired")
        if ttl > 0:
            digest = self._digest(token)
            self.cache.set(self._token_key(digest), token, timeout=ttl)
            self.cache.set(self.CURRENT_KEY, digest, timeout=ttl)
        return token

    def invalidate_if_current(self, rejected_token):
        rejected_digest = self._digest(rejected_token)
        if self.cache.get(self.CURRENT_KEY) != rejected_digest:
            return False
        # Token values are immutable cache entries, so a concurrent refresh writes
        # a different key and cannot be deleted by this invalidation.
        self.cache.delete(self._token_key(rejected_digest))
        return True
