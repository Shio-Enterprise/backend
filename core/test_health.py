from unittest.mock import patch

from django.db.utils import OperationalError
from django.test import SimpleTestCase, override_settings


class HealthCheckTests(SimpleTestCase):
    def test_health_returns_200_without_authentication(self):
        with patch("core.views.connection") as mock_connection:
            mock_cursor = mock_connection.cursor.return_value.__enter__.return_value
            mock_cursor.fetchone.return_value = (1,)

            response = self.client.get("/health/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "database": "ok"})
        mock_connection.ensure_connection.assert_called_once()

    def test_health_returns_503_when_database_unavailable(self):
        with patch("core.views.connection") as mock_connection:
            mock_connection.ensure_connection.side_effect = OperationalError(
                "database down"
            )

            response = self.client.get("/health/")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(), {"status": "error", "database": "unavailable"}
        )

    @override_settings(
        SECURE_SSL_REDIRECT=True,
        SECURE_REDIRECT_EXEMPT=[r"^health/?$"],
        ALLOWED_HOSTS=["testserver", "healthcheck.railway.app"],
    )
    def test_health_is_exempt_from_ssl_redirect_for_railway_probe(self):
        with patch("core.views.connection") as mock_connection:
            mock_cursor = mock_connection.cursor.return_value.__enter__.return_value
            mock_cursor.fetchone.return_value = (1,)

            response = self.client.get(
                "/health/",
                HTTP_HOST="healthcheck.railway.app",
            )

        self.assertEqual(response.status_code, 200)
