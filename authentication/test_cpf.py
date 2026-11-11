from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from .cpf import normalize_cpf


class NormalizeCPFTests(SimpleTestCase):
    def test_valid_cpf_with_and_without_punctuation(self):
        for value in ("09675623330", "096.756.233-30", " 096.756.233-30 "):
            with self.subTest(value=value):
                self.assertEqual(normalize_cpf(value), "09675623330")

    def test_invalid_cpf(self):
        values = [
            "123.456.789-00",
            "09675623340",
            "09675623331",
            "0967562333",
            "096756233300",
            "abc",
            "...-",
            *[str(digit) * 11 for digit in range(10)],
        ]
        for value in values:
            with (
                self.subTest(value=value),
                self.assertRaisesMessage(ValidationError, "CPF inválido."),
            ):
                normalize_cpf(value)

    def test_empty_cpf(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertIsNone(normalize_cpf(value))
