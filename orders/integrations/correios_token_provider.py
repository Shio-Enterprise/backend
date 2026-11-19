import threading
import time


class CorreiosTokenProvider:
    """Guarda o token dos Correios na memória do processo até ele expirar."""

    def __init__(self, token_loader, clock=time.monotonic):
        self.token_loader = token_loader
        self.clock = clock
        self._token = None
        self._expires_at = 0.0
        self._refresh_lock = threading.Lock()

    def _current_token(self):
        if self._token and self.clock() < self._expires_at:
            return self._token
        return None

    def get_token(self):
        token = self._current_token()
        if token:
            return token

        with self._refresh_lock:
            token = self._current_token()
            if token:
                return token
            return self._load_token()

    def refresh_token(self):
        with self._refresh_lock:
            return self._load_token()

    def _load_token(self):
        token, ttl = self.token_loader()
        if ttl > 0:
            self._token = token
            self._expires_at = self.clock() + ttl
        else:
            self._token = None
            self._expires_at = 0.0
        return token

    def invalidate_if_current(self, rejected_token):
        with self._refresh_lock:
            if self._token != rejected_token:
                return False
            # Só apaga se ainda for o token recusado: um token novo fica.
            self._token = None
            self._expires_at = 0.0
            return True
