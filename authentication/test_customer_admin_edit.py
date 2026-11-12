"""#100: admin edita nome, telefone e CPF do cliente pelo CRM."""

from django.contrib.auth.models import Permission
from rest_framework.test import APITestCase

from authentication.models import User, UserProfile, UserRole


def make_customer(email, name="Maria Silva", phone=None, cpf=None):
    user = User.objects.create_user(email=email, name=name, password="Teste@123")
    UserProfile.objects.create(
        user=user, role=UserRole.CUSTOMER, phone_number=phone, cpf=cpf
    )
    return user


def make_admin(email, *codenames):
    user = User.objects.create_user(email=email, name="Admin", is_staff=True)
    UserProfile.objects.create(user=user, role=UserRole.ADMIN)
    user.user_permissions.set(
        Permission.objects.filter(
            content_type__app_label="authentication", codename__in=codenames
        )
    )
    return user


class CustomerAdminEditTests(APITestCase):
    def setUp(self):
        self.customer = make_customer(
            "maria@example.com", phone="61999998888", cpf="52998224725"
        )
        self.admin = make_admin("clientes@example.com", "manage_customers")
        self.url = f"/api/auth/crm/customers/{self.customer.id}/"
        self.client.force_authenticate(self.admin)

    def test_detail_brings_formatted_phone_and_cpf_for_the_form(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["phone_number"], "(61) 99999-8888")
        self.assertEqual(response.data["cpf"], "529.982.247-25")
        self.assertIn("order_history", response.data)

    def test_detail_without_phone_and_cpf_returns_null(self):
        other = make_customer("semdados@example.com")
        response = self.client.get(f"/api/auth/crm/customers/{other.id}/")
        self.assertIsNone(response.data["phone_number"])
        self.assertIsNone(response.data["cpf"])

    def test_edit_name_phone_and_cpf(self):
        response = self.client.patch(
            self.url,
            {
                "name": "Maria Souza",
                "phone_number": "(11) 98765-4321",
                "cpf": "111.444.777-35",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["name"], "Maria Souza")
        self.assertEqual(response.data["phone_number"], "(11) 98765-4321")
        self.assertEqual(response.data["cpf"], "111.444.777-35")
        self.customer.refresh_from_db()
        self.customer.profile.refresh_from_db()
        self.assertEqual(self.customer.name, "Maria Souza")
        self.assertEqual(self.customer.profile.phone_number, "11987654321")
        self.assertEqual(self.customer.profile.cpf, "11144477735")
        # A lista mostra o nome novo.
        listing = self.client.get("/api/auth/crm/customers/")
        names = [c["name"] for c in listing.data["results"]]
        self.assertIn("Maria Souza", names)

    def test_phone_and_cpf_can_be_cleared(self):
        response = self.client.patch(
            self.url, {"phone_number": "", "cpf": ""}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.customer.profile.refresh_from_db()
        self.assertIsNone(self.customer.profile.phone_number)
        self.assertIsNone(self.customer.profile.cpf)

    def test_email_and_password_are_not_changed(self):
        response = self.client.patch(
            self.url,
            {"email": "outro@example.com", "password": "Nova@1234", "name": "Maria"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.email, "maria@example.com")
        self.assertTrue(self.customer.check_password("Teste@123"))

    def test_invalid_cpf_phone_and_blank_name_are_refused(self):
        response = self.client.patch(
            self.url,
            {"name": "  ", "phone_number": "123", "cpf": "111.111.111-11"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["name"], ["Informe o nome."])
        self.assertEqual(response.data["cpf"], ["CPF inválido."])
        self.assertIn("phone_number", response.data)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.name, "Maria Silva")

    def test_cpf_of_another_account_is_refused(self):
        make_customer("joao@example.com", name="João", cpf="11144477735")
        response = self.client.patch(self.url, {"cpf": "111.444.777-35"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data["cpf"], ["Este CPF já está cadastrado em outra conta."]
        )

    def test_keeping_own_cpf_is_allowed(self):
        response = self.client.patch(
            self.url, {"name": "Maria", "cpf": "529.982.247-25"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_admin_without_customers_permission_gets_403(self):
        self.client.force_authenticate(
            make_admin("pedidos@example.com", "manage_orders")
        )
        response = self.client.patch(self.url, {"name": "Hacker"}, format="json")
        self.assertEqual(response.status_code, 403)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.name, "Maria Silva")

    def test_customer_and_anonymous_cannot_edit(self):
        other = make_customer("cliente2@example.com", name="Cliente 2")
        self.client.force_authenticate(other)
        self.assertEqual(
            self.client.patch(self.url, {"name": "X"}, format="json").status_code, 403
        )
        self.client.force_authenticate(None)
        self.assertEqual(
            self.client.patch(self.url, {"name": "X"}, format="json").status_code, 401
        )

    def test_admin_accounts_cannot_be_edited_here(self):
        response = self.client.patch(
            f"/api/auth/crm/customers/{self.admin.id}/", {"name": "X"}, format="json"
        )
        self.assertEqual(response.status_code, 404)

    def test_put_is_not_allowed(self):
        response = self.client.put(self.url, {"name": "Maria"}, format="json")
        self.assertEqual(response.status_code, 405)
