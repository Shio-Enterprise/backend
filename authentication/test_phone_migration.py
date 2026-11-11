from contextlib import redirect_stdout
from io import StringIO

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class NormalizePhoneMigrateTests(TransactionTestCase):
    def test_normaliza_validos_e_preserva_invalidos(self):
        executor = MigrationExecutor(connection)
        self.addCleanup(
            lambda: MigrationExecutor(connection).migrate(
                executor.loader.graph.leaf_nodes()
            )
        )
        previous = [
            (
                "authentication",
                "0009_merge_0006_normalize_profile_cpf_0008_manage_homepage",
            )
        ]
        target = [("authentication", "0010_normalize_profile_phone")]
        executor.migrate(previous)
        apps = executor.loader.project_state(previous).apps
        User = apps.get_model("authentication", "User")
        Profile = apps.get_model("authentication", "UserProfile")
        values = {
            "mascara@example.com": "(61) 99999-9999",
            "digitos@example.com": "61999999999",
            "ddi@example.com": "+55 11 3333-4444",
            "invalido@example.com": "99999-9999",
            "texto@example.com": "não tenho",
            "vazio@example.com": "",
            "branco@example.com": "   ",
            "nulo@example.com": None,
        }
        for email, phone in values.items():
            user = User.objects.create(email=email, name=email)
            Profile.objects.create(user=user, phone_number=phone)

        output = StringIO()
        with redirect_stdout(output):
            MigrationExecutor(connection).migrate(target)

        apps = MigrationExecutor(connection).loader.project_state(target).apps
        Profile = apps.get_model("authentication", "UserProfile")
        expected = {
            **values,
            "mascara@example.com": "61999999999",
            "ddi@example.com": "1133334444",
            "vazio@example.com": None,
            "branco@example.com": None,
        }
        self.assertEqual(
            dict(Profile.objects.values_list("user__email", "phone_number")),
            expected,
        )
        self.assertIn("invalido@example.com", output.getvalue())
        self.assertIn("texto@example.com", output.getvalue())
        self.assertNotIn("mascara@example.com", output.getvalue())
        self.assertNotIn("99999-9999", output.getvalue())
