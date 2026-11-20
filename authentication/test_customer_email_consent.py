"""#103: perfil do cliente no admin mostra se ele aceitou receber e-mails."""

import csv
import io
from datetime import timedelta

import openpyxl
from django.contrib.auth.models import Permission
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APITestCase

from authentication.models import NewsletterSubscriber, User, UserProfile, UserRole
from notifications.marketing import opt_in, opt_out

URL = "/api/auth/crm/customers/"


def make_customer(email, name="Cliente"):
    user = User.objects.create_user(email=email, name=name, password="Teste@123")
    UserProfile.objects.create(user=user, role=UserRole.CUSTOMER)
    return user


def make_admin(email="clientes@example.com"):
    user = User.objects.create_user(email=email, name="Admin", is_staff=True)
    UserProfile.objects.create(user=user, role=UserRole.ADMIN)
    user.user_permissions.set(
        Permission.objects.filter(
            content_type__app_label="authentication", codename="manage_customers"
        )
    )
    return user


class CustomerEmailConsentTests(APITestCase):
    def setUp(self):
        self.client.force_authenticate(make_admin())
        self.accepted = make_customer("aceitou@example.com")
        self.unsubscribed = make_customer("saiu@example.com")
        self.never = make_customer("nunca@example.com")
        opt_in(self.accepted.email)
        opt_in(self.unsubscribed.email)
        opt_out(self.unsubscribed.email)

    def detail(self, user):
        response = self.client.get(f"{URL}{user.id}/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_accepted_shows_yes_with_acceptance_date(self):
        sub = NewsletterSubscriber.objects.get(email=self.accepted.email)
        data = self.detail(self.accepted)
        self.assertIs(data["accepts_emails"], True)
        self.assertEqual(parse_datetime(data["email_consent_at"]), sub.subscribed_at)
        self.assertIsNone(data["email_unsubscribed_at"])

    def test_unsubscribed_shows_no_with_unsubscribe_date(self):
        sub = NewsletterSubscriber.objects.get(email=self.unsubscribed.email)
        data = self.detail(self.unsubscribed)
        self.assertIs(data["accepts_emails"], False)
        self.assertIsNone(data["email_consent_at"])
        self.assertEqual(
            parse_datetime(data["email_unsubscribed_at"]), sub.unsubscribed_at
        )

    def test_never_accepted_shows_no_without_dates(self):
        data = self.detail(self.never)
        self.assertIs(data["accepts_emails"], False)
        self.assertIsNone(data["email_consent_at"])
        self.assertIsNone(data["email_unsubscribed_at"])

    def test_record_without_consent_is_not_acceptance(self):
        # Linha criada sem consentimento (ex.: pelo admin do Django) não vale.
        other = make_customer("semconsent@example.com")
        NewsletterSubscriber.objects.create(email=other.email, consent_lgpd=False)
        self.assertIs(self.detail(other)["accepts_emails"], False)

    def test_email_match_ignores_case(self):
        other = make_customer("caixa@example.com")
        NewsletterSubscriber.objects.create(
            email="CAIXA@example.com", consent_lgpd=True
        )
        self.assertIs(self.detail(other)["accepts_emails"], True)

    def test_filter_returns_only_who_accepted(self):
        response = self.client.get(URL, {"accepts_emails": "true"})
        self.assertEqual(response.status_code, 200)
        emails = [c["email"] for c in response.data["results"]]
        self.assertEqual(emails, [self.accepted.email])

    def test_filter_false_returns_who_did_not_accept(self):
        response = self.client.get(URL, {"accepts_emails": "false"})
        emails = {c["email"] for c in response.data["results"]}
        self.assertEqual(emails, {self.unsubscribed.email, self.never.email})

    def test_invalid_filter_value_is_ignored(self):
        response = self.client.get(URL, {"accepts_emails": "talvez"})
        self.assertEqual(response.data["count"], 3)

    def test_export_follows_filter_and_same_rule(self):
        response = self.client.get(
            f"{URL}export/", {"file_format": "csv", "accepts_emails": "true"}
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8-sig")
        self.assertIn(self.accepted.email, body)
        self.assertNotIn(self.never.email, body)
        self.assertNotIn(self.unsubscribed.email, body)

    def _export_rows(self, file_format, **params):
        response = self.client.get(
            f"{URL}export/", {"file_format": file_format, **params}
        )
        self.assertEqual(response.status_code, 200)
        if file_format == "csv":
            content = response.content.decode("utf-8-sig")
            rows = list(csv.reader(io.StringIO(content), delimiter=";"))
        else:
            ws = openpyxl.load_workbook(io.BytesIO(response.content)).active
            rows = [[c.value for c in row] for row in ws.iter_rows()]
        col = rows[0].index("Inscrito na Newsletter")
        return {row[1]: row[col] for row in rows[1:]}

    def test_export_column_values_csv_and_xlsx(self):
        expected = {
            self.accepted.email: "Sim",
            self.unsubscribed.email: "Não",
            self.never.email: "Não",
        }
        self.assertEqual(self._export_rows("csv"), expected)
        self.assertEqual(self._export_rows("xlsx"), expected)

    def test_export_xlsx_respects_filter(self):
        self.assertEqual(
            self._export_rows("xlsx", accepts_emails="false"),
            {self.unsubscribed.email: "Não", self.never.email: "Não"},
        )

    def test_filter_combines_with_search(self):
        other = make_customer("aceitou-tambem@example.com", name="Outra")
        opt_in(other.email)
        response = self.client.get(URL, {"accepts_emails": "true", "search": "outra"})
        self.assertEqual([c["email"] for c in response.data["results"]], [other.email])
        response = self.client.get(URL, {"accepts_emails": "false", "search": "outra"})
        self.assertEqual(response.data["count"], 0)

    def test_filter_combines_with_ordering(self):
        other = make_customer("segundo@example.com")
        opt_in(other.email)
        User.objects.filter(email=other.email).update(
            created_at=timezone.now() - timedelta(days=10)
        )
        response = self.client.get(
            URL, {"accepts_emails": "true", "ordering": "created_at"}
        )
        self.assertEqual(
            [c["email"] for c in response.data["results"]],
            [other.email, self.accepted.email],
        )

    def test_export_combines_filter_and_search(self):
        rows = self._export_rows("csv", accepts_emails="false", search="nunca")
        self.assertEqual(rows, {self.never.email: "Não"})


class ReacceptUpdatesDateTests(APITestCase):
    """Quem se descadastra e aceita de novo tem a data do novo aceite."""

    def setUp(self):
        self.email = "volta@example.com"
        opt_in(self.email)
        opt_out(self.email)
        self.old = timezone.now() - timedelta(days=30)
        NewsletterSubscriber.objects.filter(email=self.email).update(
            subscribed_at=self.old
        )

    def test_opt_in_again_updates_subscribed_at(self):
        opt_in(self.email)
        sub = NewsletterSubscriber.objects.get(email=self.email)
        self.assertGreater(sub.subscribed_at, self.old)
        self.assertIsNone(sub.unsubscribed_at)

    def test_opt_in_while_active_keeps_original_date(self):
        opt_in(self.email)
        first = NewsletterSubscriber.objects.get(email=self.email).subscribed_at
        opt_in(self.email)
        sub = NewsletterSubscriber.objects.get(email=self.email)
        self.assertEqual(sub.subscribed_at, first)

    def test_newsletter_form_again_updates_subscribed_at(self):
        response = self.client.post(
            "/api/auth/newsletter/subscribe/",
            {"email": self.email, "consent_lgpd": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        sub = NewsletterSubscriber.objects.get(email=self.email)
        self.assertGreater(sub.subscribed_at, self.old)
