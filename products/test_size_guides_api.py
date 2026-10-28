from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from rest_framework import status
from rest_framework.test import APITestCase

from authentication.admin_permissions import ADMIN_PERMISSION_CODES
from authentication.models import UserProfile, UserRole
from products.models import Category, SizeGuide
from products.tests import auth_header, make_product, make_user

User = get_user_model()

COLUMNS = [{"key": "waist", "label": "Cintura"}]
ROWS = [
    {"size": "P", "measures": {"waist": [74, 80]}},
    {"size": "M", "measures": {"waist": [80, 86]}},
]


def make_admin(email, codename):
    user = User.objects.create_user(email=email, name="Admin", is_staff=True)
    UserProfile.objects.create(user=user, role=UserRole.ADMIN)
    user.user_permissions.set(
        Permission.objects.filter(
            content_type__app_label="authentication",
            codename=codename,
        )
    )
    assert codename in ADMIN_PERMISSION_CODES
    return user


class SizeGuideApiTests(APITestCase):
    url = "/api/catalog/size-guides/"

    def setUp(self):
        self.admin = make_admin("catalogo@x.com", "manage_catalog")
        self.camisetas = Category.objects.create(name="Camisetas", slug="camisetas")
        self.calcas = Category.objects.create(name="Calças", slug="calcas")

    def detail(self, guide):
        return f"{self.url}{guide.pk}/"

    def payload(self, **extra):
        return {"name": "Calças", "columns": COLUMNS, "rows": ROWS, **extra}

    def test_sem_login_ou_sem_permissao_de_catalogo_nao_acessa(self):
        guide = SizeGuide.objects.create(name="X", columns=COLUMNS, rows=ROWS)
        pedidos = make_admin("pedidos@x.com", "manage_orders")
        cliente = make_user("cliente@x.com")
        for headers, esperado in (
            ({}, status.HTTP_401_UNAUTHORIZED),
            (auth_header(pedidos), status.HTTP_403_FORBIDDEN),
            (auth_header(cliente), status.HTTP_403_FORBIDDEN),
        ):
            with self.subTest(esperado=esperado):
                self.assertEqual(
                    self.client.get(self.url, **headers).status_code, esperado
                )
                self.assertEqual(
                    self.client.post(
                        self.url, self.payload(), format="json", **headers
                    ).status_code,
                    esperado,
                )
                self.assertEqual(
                    self.client.delete(self.detail(guide), **headers).status_code,
                    esperado,
                )
        self.assertTrue(SizeGuide.objects.filter(pk=guide.pk).exists())

    def test_cria_guia_e_liga_categorias(self):
        response = self.client.post(
            self.url,
            self.payload(categories=[str(self.calcas.pk)]),
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertEqual(body["rows"], ROWS)
        self.assertEqual(body["categories"], [str(self.calcas.pk)])
        self.assertEqual(body["category_details"][0]["name"], "Calças")
        self.calcas.refresh_from_db()
        self.assertEqual(str(self.calcas.size_guide_id), body["id"])

    def test_guia_invalido_retorna_400_com_o_motivo(self):
        rows = [{"size": "P", "measures": {"waist": [80, 74]}}]
        response = self.client.post(
            self.url, self.payload(rows=rows), format="json", **auth_header(self.admin)
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("rows", response.json()["details"])
        self.assertFalse(SizeGuide.objects.filter(name="Calças").exists())

    def test_nome_repetido_retorna_400(self):
        SizeGuide.objects.create(name="Calças", columns=COLUMNS, rows=ROWS)
        response = self.client.post(
            self.url, self.payload(), format="json", **auth_header(self.admin)
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("name", response.json()["details"])

    def test_lista_guias_com_categorias(self):
        guide = SizeGuide.objects.create(name="Calças", columns=COLUMNS, rows=ROWS)
        self.calcas.size_guide = guide
        self.calcas.save()
        body = self.client.get(self.url, **auth_header(self.admin)).json()
        names = [g["name"] for g in body]
        self.assertEqual(names, sorted(names))
        listed = next(g for g in body if g["name"] == "Calças")
        self.assertEqual(listed["categories"], [str(self.calcas.pk)])

    def test_atualizar_troca_categorias_entre_guias(self):
        antigo = SizeGuide.objects.create(name="Antigo", columns=COLUMNS, rows=ROWS)
        novo = SizeGuide.objects.create(name="Novo", columns=COLUMNS, rows=ROWS)
        self.camisetas.size_guide = antigo
        self.camisetas.save()
        self.calcas.size_guide = novo
        self.calcas.save()

        response = self.client.put(
            self.detail(novo),
            {
                "name": "Novo",
                "columns": COLUMNS,
                "rows": ROWS,
                "categories": [str(self.camisetas.pk)],
            },
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.camisetas.refresh_from_db()
        self.calcas.refresh_from_db()
        # Camisetas saiu do antigo para o novo; Calças ficou de fora e foi desligada.
        self.assertEqual(self.camisetas.size_guide, novo)
        self.assertIsNone(self.calcas.size_guide)

    def test_atualizar_sem_categories_mantem_as_ligacoes(self):
        guide = SizeGuide.objects.create(name="Calças", columns=COLUMNS, rows=ROWS)
        self.calcas.size_guide = guide
        self.calcas.save()
        rows = [{"size": "P", "measures": {"waist": [70, 80]}}]
        response = self.client.put(
            self.detail(guide),
            self.payload(rows=rows),
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        guide.refresh_from_db()
        self.calcas.refresh_from_db()
        self.assertEqual(guide.rows, rows)
        self.assertEqual(self.calcas.size_guide, guide)

    def test_excluir_deixa_categoria_e_produto_sem_guia(self):
        guide = SizeGuide.objects.create(name="Calças", columns=COLUMNS, rows=ROWS)
        self.calcas.size_guide = guide
        self.calcas.save()
        product = make_product(name="Calça", category=self.calcas)
        response = self.client.delete(self.detail(guide), **auth_header(self.admin))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.calcas.refresh_from_db()
        self.assertIsNone(self.calcas.size_guide)
        detail = self.client.get(f"/api/catalog/products/{product.pk}/").json()
        self.assertIsNone(detail["size_guide"])

    def test_alteracao_aparece_no_detalhe_do_produto(self):
        guide = SizeGuide.objects.create(name="Calças", columns=COLUMNS, rows=ROWS)
        product = make_product(name="Calça", category=self.calcas)
        self.client.put(
            self.detail(guide),
            self.payload(categories=[str(self.calcas.pk)]),
            format="json",
            **auth_header(self.admin),
        )
        detail = self.client.get(f"/api/catalog/products/{product.pk}/").json()
        self.assertEqual(detail["size_guide"]["name"], "Calças")
