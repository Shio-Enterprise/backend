from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase

from authentication.models import UserProfile

from .correios import build_destinatario_data_from_order


class ShippingCPFTests(TestCase):
    def test_correios_receives_digits_for_canonical_and_legacy_cpf(self):
        for value in ("09675623330", "096.756.233-30", None):
            with self.subTest(value=value):
                user = get_user_model().objects.create_user(
                    email=f"{value}@example.com"
                )
                UserProfile.objects.create(user=user, cpf=value)
                self.assert_cpf(user, "09675623330" if value else "")

    def test_correios_handles_missing_profile(self):
        user = get_user_model().objects.create_user(email="no-profile@example.com")
        self.assert_cpf(user, "")

    def assert_cpf(self, user, expected):
        order = SimpleNamespace(
            user=user,
            shipping_zip_code="71000-000",
            shipping_street="Rua Teste",
            shipping_number="1",
            shipping_complement=None,
            shipping_neighborhood="Centro",
            shipping_city="Brasília",
            shipping_state="DF",
        )
        self.assertEqual(build_destinatario_data_from_order(order)["cpfCnpj"], expected)
