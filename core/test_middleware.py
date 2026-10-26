from django.test import SimpleTestCase, override_settings


class CorsCheckoutHeadersTests(SimpleTestCase):
    @override_settings(CORS_ALLOWED_ORIGINS=["https://store.example.test"])
    def test_browser_can_read_retry_delay_and_error_reference(self):
        response = self.client.get(
            "/api/auth/me/", HTTP_ORIGIN="https://store.example.test"
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response["Access-Control-Allow-Origin"], "https://store.example.test"
        )
        exposed = {
            header.strip().lower()
            for header in response["Access-Control-Expose-Headers"].split(",")
        }
        self.assertTrue({"retry-after", "x-request-id"}.issubset(exposed))
        self.assertIn("X-Request-ID", response)
