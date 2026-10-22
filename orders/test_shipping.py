import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import Mock

from django.core.cache.backends.locmem import LocMemCache
from django.test import SimpleTestCase, override_settings

from orders.integrations.correios_client import CorreiosClient
from orders.integrations.correios_token_provider import CorreiosTokenProvider
from orders.shipping import resolve_shipping_parameters


@override_settings(
    CORREIOS_API_BASE_URL="https://correios.example.test/",
    CORREIOS_REMETENTE_CEP="70000-000",
    CORREIOS_CODIGO_SERVICO="03220",
    CORREIOS_PESO_PADRAO_GRAMAS="300",
    CORREIOS_COMPRIMENTO_CM="20",
    CORREIOS_LARGURA_CM="18",
    CORREIOS_ALTURA_CM="5",
    SHIPPING_POLICY_VERSION="policy-2",
    CORREIOS_MOCK_ENABLED=False,
)
class ShippingFoundationTests(SimpleTestCase):
    def test_parameters_are_server_resolved_immutable_and_snapshot_safe(self):
        parameters = resolve_shipping_parameters("71000-000", declared_value="129.90")

        self.assertEqual(parameters.origin, "70000000")
        self.assertEqual(parameters.destination, "71000000")
        self.assertEqual(parameters.service, "03220")
        self.assertEqual(parameters.weight_grams, "300")
        self.assertEqual(parameters.declared_value, "129.90")
        self.assertEqual(parameters.as_snapshot()["policy_version"], "policy-2")
        with self.assertRaises(FrozenInstanceError):
            parameters.weight_grams = "1"

    def test_rejects_invalid_server_logistics_configuration(self):
        with override_settings(CORREIOS_REMETENTE_CEP="invalid"):
            with self.assertRaises(ValueError):
                resolve_shipping_parameters("71000000")

    def test_client_owns_url_construction_and_bearer_transport(self):
        get = Mock(return_value=SimpleNamespace(status_code=200))
        client = CorreiosClient(transport=SimpleNamespace(get=get))

        response = client.request("GET", "/cep/v1/enderecos/71000000", token="abc")

        self.assertEqual(response.status_code, 200)
        get.assert_called_once_with(
            "https://correios.example.test/cep/v1/enderecos/71000000",
            headers={"Accept": "application/json", "Authorization": "Bearer abc"},
        )

    def test_rejected_old_token_cannot_evict_a_refreshed_token(self):
        tokens = iter((("old-token", 60), ("new-token", 60)))
        cache = LocMemCache("test-correios-token-provider", {})
        provider = CorreiosTokenProvider(lambda: next(tokens), cache_backend=cache)

        self.assertEqual(provider.get_token(), "old-token")
        self.assertEqual(provider.refresh_token(), "new-token")
        self.assertFalse(provider.invalidate_if_current("old-token"))
        self.assertEqual(provider.get_token(), "new-token")

    def test_concurrent_reads_share_one_refresh_in_this_process(self):
        loader = Mock(side_effect=lambda: (time.sleep(0.02) or "shared-token", 60))
        cache = LocMemCache("test-correios-token-refresh", {})
        provider = CorreiosTokenProvider(loader, cache_backend=cache)
        start = threading.Barrier(8)

        def read_token(_):
            start.wait()
            return provider.get_token()

        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(read_token, range(8)))

        self.assertEqual(tokens, ["shared-token"] * 8)
        loader.assert_called_once_with()
