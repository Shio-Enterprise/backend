from contextlib import redirect_stdout
from io import StringIO

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class NormalizeCPFMigrateTests(TransactionTestCase):
    def test_normalizes_only_safe_values_and_reports_conflicts_without_data_loss(self):
        executor = MigrationExecutor(connection)
        self.addCleanup(
            lambda: MigrationExecutor(connection).migrate(
                executor.loader.graph.leaf_nodes()
            )
        )
        previous = [("authentication", "0005_newsletter_email_lowercase")]
        target = [("authentication", "0006_normalize_profile_cpf")]
        executor.migrate(previous)
        apps = executor.loader.project_state(previous).apps
        User = apps.get_model("authentication", "User")
        Profile = apps.get_model("authentication", "UserProfile")
        values = {
            "safe@example.com": "529.982.247-25",
            "digits@example.com": "16899535009",
            "duplicate1@example.com": "096.756.233-30",
            "duplicate2@example.com": "09675623330",
            "invalid@example.com": "123.456.789-00",
            "repeated@example.com": "111.111.111-11",
            "empty@example.com": "",
            "blank@example.com": "   ",
            "null@example.com": None,
        }
        for email, cpf in values.items():
            user = User.objects.create(email=email, name=email)
            Profile.objects.create(user=user, cpf=cpf)

        output = StringIO()
        with redirect_stdout(output):
            MigrationExecutor(connection).migrate(target)

        apps = MigrationExecutor(connection).loader.project_state(target).apps
        Profile = apps.get_model("authentication", "UserProfile")
        expected = {
            **values,
            "safe@example.com": "52998224725",
            "empty@example.com": None,
            "blank@example.com": None,
        }
        self.assertEqual(
            dict(Profile.objects.values_list("user__email", "cpf")), expected
        )
        self.assertEqual(User.objects.count(), len(values))
        for email in (
            "duplicate1@example.com",
            "duplicate2@example.com",
            "invalid@example.com",
            "repeated@example.com",
        ):
            self.assertIn(email, output.getvalue())
        self.assertIn("CPF duplicado preservado", output.getvalue())
        self.assertIn("CPF inválido preservado", output.getvalue())
        self.assertNotIn("safe@example.com", output.getvalue())
        self.assertNotIn("09675623330", output.getvalue())
