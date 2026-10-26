from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase


class DatabaseStartupTests(SimpleTestCase):
    def test_command_is_registered_and_uses_configured_connection(self):
        connection = MagicMock()
        output = StringIO()
        with patch(
            "core.management.commands.wait_for_database.connections",
            {"default": connection},
        ):
            call_command("wait_for_database", timeout=1, stdout=output)

        connection.cursor.return_value.__enter__.return_value.execute.assert_called_once_with(
            "SELECT 1"
        )
        self.assertIn("Database ready.", output.getvalue())

    def test_retries_until_connection_is_available(self):
        connection = MagicMock()
        connection.cursor.side_effect = [RuntimeError("offline"), MagicMock()]
        with (
            patch(
                "core.management.commands.wait_for_database.connections",
                {"default": connection},
            ),
            patch("core.management.commands.wait_for_database.sleep") as sleep,
            patch(
                "core.management.commands.wait_for_database.monotonic", return_value=0
            ),
        ):
            call_command("wait_for_database", timeout=1, stdout=StringIO())

        self.assertEqual(connection.cursor.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_fails_after_timeout(self):
        connection = MagicMock()
        connection.cursor.side_effect = RuntimeError("offline")
        with (
            patch(
                "core.management.commands.wait_for_database.connections",
                {"default": connection},
            ),
            patch("core.management.commands.wait_for_database.sleep"),
            patch(
                "core.management.commands.wait_for_database.monotonic",
                side_effect=[0, 0, 0, 2],
            ),
            self.assertRaisesMessage(CommandError, "Database unavailable after 1s"),
        ):
            call_command("wait_for_database", timeout=1, stdout=StringIO())
