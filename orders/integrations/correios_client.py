import requests
from django.conf import settings


class CorreiosClient:
    """Small HTTP boundary for Correios URLs, authentication headers and transport."""

    def __init__(self, transport=requests):
        self.transport = transport

    def build_correios_url(self, path: str) -> str:
        base_url = settings.CORREIOS_API_BASE_URL.rstrip("/")
        return f"{base_url}/{path.lstrip('/')}"

    def request(self, method: str, path: str, *, token=None, headers=None, **kwargs):
        request_headers = {"Accept": "application/json", **(headers or {})}
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        request_method = getattr(self.transport, method.lower())
        return request_method(
            self.build_correios_url(path), headers=request_headers, **kwargs
        )
