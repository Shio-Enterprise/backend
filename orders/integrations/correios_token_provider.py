import threading
import time

_EMPTY = (None, 0.0)


class CorreiosTokenProvider:
    """Guarda o token dos Correios na memória do processo até ele expirar."""

    def __init__(self, token_loader, clock=time.monotonic):
        self.token_loader = token_loader
        self.clock = clock
        # Token e validade numa tupla só: leitura sem lock nunca vê par misturado.
        self._state = _EMPTY
        self._refresh_lock = threading.Lock()

    def _current_token(self):
        token, expires_at = self._state
        if token and self.clock() < expires_at:
            return token
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
        self._state = (token, self.clock() + ttl) if ttl > 0 else _EMPTY
        return token

    def invalidate_if_current(self, rejected_token):
        with self._refresh_lock:
            if self._state[0] != rejected_token:
                return False
            # Só apaga se ainda for o token recusado: um token novo fica.
            self._state = _EMPTY
            return True
