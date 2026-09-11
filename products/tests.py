"""
Testes para os endpoints de Category e DropCampaign.

Executar com:
    pytest products/tests.py -v
"""

import os
import uuid
from datetime import timedelta
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import path
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.validation import validate_schema
from PIL import Image
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from authentication.models import UserProfile, UserRole
from orders.models import CustomerOrder, OrderItem, OrderStatus

from .catalog import filter_catalog
from .models import (
    Category,
    DropCampaign,
    Product,
    ProductImage,
    ProductVariation,
    StockMovement,
)
from .views import CatalogFilterOptionsView, ProductListCreateView

User = get_user_model()


# ─── Helpers ──────────────────────────────────────────────────────────────────


def make_user(email, role=UserRole.CUSTOMER, name="User", is_staff=False):
    """Cria um utilizador com perfil associado para uso nos testes."""
    user = User.objects.create_user(email=email, name=name, is_staff=is_staff)
    UserProfile.objects.create(user=user, role=role)
    return user


def auth_header(user):
    """Devolve o header Authorization Bearer para o utilizador."""
    token = str(RefreshToken.for_user(user).access_token)
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def make_banner(name="banner.jpg"):
    """Gera um JPEG 1x1 válido como SimpleUploadedFile para testes de upload."""
    buf = BytesIO()
    Image.new("RGB", (1, 1), color="red").save(buf, format="JPEG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


def make_product(name="Camiseta", **kwargs):
    """Cria um Product com defaults razoáveis."""
    defaults = {"description": "desc", "base_price": 100, "is_active": True}
    defaults.update(kwargs)
    return Product.objects.create(name=name, **defaults)


# ─── List & Create ────────────────────────────────────────────────────────────


class CategoryListCreateTests(APITestCase):
    """Testes para GET/POST /api/catalog/categories/."""

    url = "/api/catalog/categories/"

    def setUp(self):
        self.admin = make_user(
            "admin@x.com", role=UserRole.ADMIN, name="Admin", is_staff=True
        )
        self.customer = make_user("c@x.com", role=UserRole.CUSTOMER, name="Cliente")
        Category.objects.create(name="Camisetas", slug="camisetas")
        Category.objects.create(name="Bonés", slug="bones")

    def test_listagem_publica_sem_autenticacao(self):
        """Deve listar categorias paginadas sem token."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["count"], 2)
        self.assertIn("results", data)

    def test_admin_cria_categoria_com_slug_auto(self):
        """POST de admin sem slug deve auto-gerar a partir do name."""
        response = self.client.post(
            self.url,
            {"name": "Calças Cargo"},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["slug"], "calcas-cargo")

    def test_admin_cria_com_slug_explicito(self):
        """POST com slug enviado deve usar o slug fornecido."""
        response = self.client.post(
            self.url,
            {"name": "Acessórios", "slug": "acess"},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["slug"], "acess")

    def test_cliente_nao_pode_criar(self):
        """POST de utilizador não-admin deve retornar 403."""
        response = self.client.post(
            self.url,
            {"name": "X"},
            format="json",
            **auth_header(self.customer),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_sem_token_nao_pode_criar(self):
        """POST sem Authorization header deve retornar 401."""
        response = self.client.post(self.url, {"name": "X"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_nome_duplicado_retorna_400(self):
        """POST com name já existente deve retornar 400."""
        response = self.client.post(
            self.url,
            {"name": "Camisetas"},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# ─── Detail / Update / Delete ─────────────────────────────────────────────────


class CategoryDetailTests(APITestCase):
    """Testes para GET/PUT/DELETE /api/catalog/categories/{id}/."""

    def setUp(self):
        self.admin = make_user(
            "admin@x.com", role=UserRole.ADMIN, name="Admin", is_staff=True
        )
        self.customer = make_user("c@x.com", role=UserRole.CUSTOMER, name="Cliente")
        self.category = Category.objects.create(name="Camisetas", slug="camisetas")
        self.url = f"/api/catalog/categories/{self.category.id}/"

    def test_detalhe_publico(self):
        """GET deve retornar a categoria sem exigir autenticação."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["name"], "Camisetas")

    def test_detalhe_404_quando_id_inexistente(self):
        """GET com UUID inexistente deve retornar 404."""
        response = self.client.get(f"/api/catalog/categories/{uuid.uuid4()}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_put_admin_regenera_slug_quando_name_muda(self):
        """PUT mudando o name e sem enviar slug deve regenerar o slug."""
        response = self.client.put(
            self.url,
            {"name": "Camisas Polo"},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["slug"], "camisas-polo")

    def test_put_admin_mantem_slug_explicito(self):
        """PUT com slug explícito deve usar o slug enviado mesmo que name mude."""
        response = self.client.put(
            self.url,
            {"name": "Camisas Polo", "slug": "polo-custom"},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["slug"], "polo-custom")

    def test_put_admin_mantem_slug_se_name_nao_muda(self):
        """PUT sem alterar name não deve regenerar o slug."""
        response = self.client.put(
            self.url,
            {"name": "Camisetas"},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["slug"], "camisetas")

    def test_cliente_nao_pode_atualizar(self):
        """PUT de utilizador não-admin deve retornar 403."""
        response = self.client.put(
            self.url,
            {"name": "X"},
            format="json",
            **auth_header(self.customer),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_pode_remover(self):
        """DELETE de admin deve apagar a categoria e retornar 204."""
        response = self.client.delete(self.url, **auth_header(self.admin))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Category.objects.filter(pk=self.category.id).exists())

    def test_cliente_nao_pode_remover(self):
        """DELETE de utilizador não-admin deve retornar 403."""
        response = self.client.delete(self.url, **auth_header(self.customer))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


# ─── DropCampaign — List & Create ─────────────────────────────────────────────


class DropCampaignListCreateTests(APITestCase):
    """Testes para GET/POST /api/catalog/drops/."""

    url = "/api/catalog/drops/"

    def setUp(self):
        self.admin = make_user(
            "admin@x.com", role=UserRole.ADMIN, name="Admin", is_staff=True
        )
        self.customer = make_user("c@x.com", role=UserRole.CUSTOMER, name="Cliente")
        now = timezone.now()
        DropCampaign.objects.create(
            name="Verão 2026",
            slug="verao-2026",
            is_active=True,
            launch_date=now - timedelta(days=1),
            end_date=now + timedelta(days=30),
        )
        DropCampaign.objects.create(name="Inativo", slug="inativo", is_active=False)
        DropCampaign.objects.create(
            name="Já terminou",
            slug="ja-terminou",
            is_active=True,
            launch_date=now - timedelta(days=60),
            end_date=now - timedelta(days=10),
        )

    def test_listagem_publica_sem_autenticacao(self):
        """Deve listar drops paginados sem token."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["count"], 3)

    def test_filtro_active_true_retorna_so_dentro_do_periodo(self):
        """?active=true retorna apenas drops ativos dentro de [launch_date, end_date]."""
        response = self.client.get(f"{self.url}?active=true")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["name"], "Verão 2026")

    def test_admin_cria_drop_sem_banner(self):
        """POST de admin com payload JSON simples deve criar drop."""
        response = self.client.post(
            self.url,
            {"name": "Outono 2026", "is_active": True},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["name"], "Outono 2026")
        self.assertIsNone(response.json()["banner"])

    def test_admin_cria_drop_com_banner_multipart(self):
        """POST multipart/form-data com banner deve salvar arquivo."""
        response = self.client.post(
            self.url,
            {"name": "Com Banner", "banner": make_banner()},
            format="multipart",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("banner", response.json())
        self.assertIsNotNone(response.json()["banner"])

    def test_banner_extensao_invalida_retorna_400(self):
        """Upload .gif (fora de jpg/jpeg/png/webp) deve retornar 400."""
        buf = BytesIO()
        Image.new("RGB", (1, 1), color="red").save(buf, format="GIF")
        gif = SimpleUploadedFile("banner.gif", buf.getvalue(), content_type="image/gif")
        response = self.client.post(
            self.url,
            {"name": "Drop", "banner": gif},
            format="multipart",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("banner", response.json()["details"])

    def test_banner_acima_de_5mb_retorna_400(self):
        """Upload de banner com tamanho > 5MB deve retornar 400."""
        buf = BytesIO()
        Image.new("RGB", (1, 1), color="red").save(buf, format="JPEG")
        content = buf.getvalue() + b"\x00" * (6 * 1024 * 1024)
        big = SimpleUploadedFile("grande.jpg", content, content_type="image/jpeg")
        response = self.client.post(
            self.url,
            {"name": "Drop", "banner": big},
            format="multipart",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("banner", response.json()["details"])

    def test_admin_datas_invalidas_retorna_400(self):
        """end_date <= launch_date deve retornar 400 com mensagem clara."""
        now = timezone.now()
        response = self.client.post(
            self.url,
            {
                "name": "Datas erradas",
                "launch_date": (now + timedelta(days=10)).isoformat(),
                "end_date": (now + timedelta(days=5)).isoformat(),
            },
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("end_date", response.json()["details"])

    def test_cliente_nao_pode_criar(self):
        """POST de utilizador não-admin deve retornar 403."""
        response = self.client.post(
            self.url,
            {"name": "X"},
            format="json",
            **auth_header(self.customer),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_sem_token_nao_pode_criar(self):
        """POST sem Authorization header deve retornar 401."""
        response = self.client.post(self.url, {"name": "X"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_admin_cria_drop_com_todos_campos(self):
        """Cria drop com description, is_public e slug auto-gerado."""
        response = self.client.post(
            self.url,
            {
                "name": "Drop Verão",
                "description": "Coleção exclusiva",
                "is_public": True,
            },
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertEqual(body["slug"], "drop-verao")
        self.assertTrue(body["is_public"])
        self.assertEqual(body["description"], "Coleção exclusiva")

    def test_admin_cria_drop_com_slug_explicito(self):
        """POST com slug enviado deve usar o slug fornecido."""
        response = self.client.post(
            self.url,
            {"name": "Drop X", "slug": "meu-slug-custom"},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["slug"], "meu-slug-custom")

    def test_slug_duplicado_retorna_400(self):
        """Slug já existente deve retornar 400."""
        response = self.client.post(
            self.url,
            {"name": "Outro", "slug": "verao-2026"},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# ─── DropCampaign — Detail / Update / Delete ──────────────────────────────────


class DropCampaignDetailTests(APITestCase):
    """Testes para GET/PUT/DELETE /api/catalog/drops/{id}/."""

    def setUp(self):
        self.admin = make_user(
            "admin@x.com", role=UserRole.ADMIN, name="Admin", is_staff=True
        )
        self.customer = make_user("c@x.com", role=UserRole.CUSTOMER, name="Cliente")
        self.drop = DropCampaign.objects.create(
            name="Verão 2026", slug="verao-2026", is_active=True
        )
        self.url = f"/api/catalog/drops/{self.drop.id}/"

    def test_detalhe_publico_inclui_produtos(self):
        """GET deve retornar o drop com a lista de produtos aninhada."""
        Product.objects.create(
            drop=self.drop, name="Camiseta", description="x", base_price=100
        )
        Product.objects.create(
            drop=self.drop, name="Boné", description="x", base_price=50
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()["products"]), 2)

    def test_detalhe_404_quando_id_inexistente(self):
        response = self.client.get(f"/api/catalog/drops/{uuid.uuid4()}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_admin_put_atualiza_dados_basicos(self):
        """PUT deve atualizar campos sem precisar do banner."""
        response = self.client.put(
            self.url,
            {"name": "Verão 2026 — Atualizado", "is_active": False},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["name"], "Verão 2026 — Atualizado")
        self.assertFalse(response.json()["is_active"])

    def test_put_trocar_banner_apaga_antigo(self):
        """PUT com novo banner deve remover o arquivo antigo do storage."""
        self.drop.banner = make_banner("antigo.jpg")
        self.drop.save()
        old_path = self.drop.banner.path
        self.assertTrue(os.path.exists(old_path))

        response = self.client.put(
            self.url,
            {"name": self.drop.name, "banner": make_banner("novo.jpg")},
            format="multipart",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(os.path.exists(old_path))

    def test_delete_remove_banner_do_disco(self):
        """DELETE do drop deve apagar o banner do storage."""
        self.drop.banner = make_banner("para-apagar.jpg")
        self.drop.save()
        banner_path = self.drop.banner.path
        self.assertTrue(os.path.exists(banner_path))

        response = self.client.delete(self.url, **auth_header(self.admin))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(os.path.exists(banner_path))
        self.assertFalse(DropCampaign.objects.filter(pk=self.drop.id).exists())

    def test_cliente_nao_pode_atualizar(self):
        response = self.client.put(
            self.url,
            {"name": "X"},
            format="json",
            **auth_header(self.customer),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cliente_nao_pode_remover(self):
        response = self.client.delete(self.url, **auth_header(self.customer))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_put_admin_regenera_slug_quando_name_muda(self):
        """PUT mudando o name sem enviar slug deve regenerar o slug."""
        response = self.client.put(
            self.url,
            {"name": "Outono 2026"},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["slug"], "outono-2026")


# ─── DropProductManage — POST/DELETE ──────────────────────────────────────────


class DropProductManageTests(APITestCase):
    """Testes para POST/DELETE /api/catalog/drops/{drop_id}/products/{product_id}/."""

    def setUp(self):
        self.admin = make_user(
            "admin@x.com", role=UserRole.ADMIN, name="Admin", is_staff=True
        )
        self.customer = make_user("c@x.com", role=UserRole.CUSTOMER, name="Cliente")
        self.drop = DropCampaign.objects.create(
            name="Verão", slug="verao", is_active=True
        )
        self.outro_drop = DropCampaign.objects.create(name="Outro", slug="outro")
        self.product = Product.objects.create(
            name="Camiseta",
            description="x",
            base_price=100,
        )
        self.url = f"/api/catalog/drops/{self.drop.id}/products/{self.product.id}/"

    def test_admin_associa_produto_ao_drop(self):
        """POST deve vincular o produto ao drop."""
        response = self.client.post(self.url, **auth_header(self.admin))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.product.refresh_from_db()
        self.assertEqual(self.product.drop_id, self.drop.id)
        self.assertEqual(len(response.json()["products"]), 1)

    def test_associar_move_produto_de_outro_drop(self):
        """Se produto já está em outro drop, deve ser movido para este."""
        self.product.drop = self.outro_drop
        self.product.save()
        self.client.post(self.url, **auth_header(self.admin))
        self.product.refresh_from_db()
        self.assertEqual(self.product.drop_id, self.drop.id)

    def test_admin_desassocia_produto(self):
        """DELETE deve setar Product.drop=None."""
        self.product.drop = self.drop
        self.product.save()
        response = self.client.delete(self.url, **auth_header(self.admin))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.product.refresh_from_db()
        self.assertIsNone(self.product.drop_id)

    def test_delete_produto_de_outro_drop_retorna_404(self):
        """DELETE de produto que está em outro drop deve retornar 404."""
        self.product.drop = self.outro_drop
        self.product.save()
        response = self.client.delete(self.url, **auth_header(self.admin))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_cliente_nao_pode_associar(self):
        response = self.client.post(self.url, **auth_header(self.customer))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cliente_nao_pode_desassociar(self):
        self.product.drop = self.drop
        self.product.save()
        response = self.client.delete(self.url, **auth_header(self.customer))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_sem_token_retorna_401(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_drop_inexistente_404(self):
        url = f"/api/catalog/drops/{uuid.uuid4()}/products/{self.product.id}/"
        response = self.client.post(url, **auth_header(self.admin))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_produto_inexistente_404(self):
        url = f"/api/catalog/drops/{self.drop.id}/products/{uuid.uuid4()}/"
        response = self.client.post(url, **auth_header(self.admin))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


# ─── Product — List ───────────────────────────────────────────────────────────


class ProductListTests(APITestCase):
    """Testes para GET/POST /api/catalog/products/."""

    url = "/api/catalog/products/"

    def setUp(self):
        self.admin = make_user(
            "admin@x.com", role=UserRole.ADMIN, name="Admin", is_staff=True
        )
        self.customer = make_user("c@x.com", role=UserRole.CUSTOMER, name="Cliente")
        self.category = Category.objects.create(name="Camisetas", slug="camisetas")
        self.drop = DropCampaign.objects.create(
            name="Verão", slug="verao", is_active=True
        )
        self.ativo = make_product(
            name="Camisa Branca", category=self.category, drop=self.drop
        )
        make_product(name="Camisa Preta", category=self.category)
        self.inativo = make_product(name="Removido", is_active=False)

    def test_listagem_publica_so_retorna_ativos(self):
        """Sem token, só produtos com is_active=True são retornados."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["count"], 2)
        names = [p["name"] for p in body["results"]]
        self.assertNotIn("Removido", names)

    def test_admin_ve_inativos(self):
        """Admin vê produtos inativos por default."""
        response = self.client.get(self.url, **auth_header(self.admin))
        self.assertEqual(response.json()["count"], 3)

    def test_admin_filtra_is_active_false(self):
        """Admin pode passar ?is_active=false e ver só inativos."""
        response = self.client.get(
            f"{self.url}?is_active=false", **auth_header(self.admin)
        )
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["name"], "Removido")

    def test_filtro_por_category(self):
        response = self.client.get(f"{self.url}?category={self.category.id}")
        self.assertEqual(response.json()["count"], 2)

    def test_filtro_por_drop(self):
        response = self.client.get(f"{self.url}?drop={self.drop.id}")
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["name"], "Camisa Branca")

    def test_busca_por_name(self):
        response = self.client.get(f"{self.url}?search=branca")
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["name"], "Camisa Branca")

    def test_ordering_por_created_at_desc(self):
        """Mais recente primeiro."""
        response = self.client.get(self.url)
        results = response.json()["results"]
        ids_returned = [r["id"] for r in results]
        # Última criada (Camisa Preta — feita depois) vem antes da Camisa Branca
        self.assertEqual(
            ids_returned[0], str(Product.objects.get(name="Camisa Preta").id)
        )


# ─── Product — Detail ─────────────────────────────────────────────────────────


class ProductListContractTests(APITestCase):
    url = "/api/catalog/products/"

    def setUp(self):
        self.category = Category.objects.create(name="Camisetas", slug="camisetas")
        self.products = [
            make_product(
                name=f"Produto {index}",
                category=self.category,
                base_price=index,
                description="Algodão exclusivo" if index == 0 else "Descrição",
            )
            for index in range(25)
        ]

    def test_pagination_defaults_links_and_custom_size(self):
        first = self.client.get(self.url).json()
        self.assertEqual(set(first), {"count", "next", "previous", "results"})
        self.assertEqual(first["count"], 25)
        self.assertEqual(len(first["results"]), 20)
        self.assertIsNone(first["previous"])
        second = self.client.get(first["next"]).json()
        self.assertEqual(len(second["results"]), 5)
        self.assertIsNone(second["next"])
        self.assertIsNotNone(second["previous"])
        self.assertFalse(
            {item["id"] for item in first["results"]}
            & {item["id"] for item in second["results"]}
        )
        page = self.client.get(self.url, {"page": 2, "page_size": 7}).json()
        self.assertEqual(page["count"], 25)
        self.assertEqual(len(page["results"]), 7)
        self.assertIn("page_size=7", page["next"])
        self.assertEqual(page["results"][0]["id"], str(self.products[17].id))

    def test_page_size_is_capped(self):
        Product.objects.bulk_create(
            [Product(name=f"Extra {i}", base_price=1) for i in range(80)]
        )
        body = self.client.get(self.url, {"page_size": 999}).json()
        self.assertEqual(body["count"], 105)
        self.assertEqual(len(body["results"]), 50)
        self.assertIsNotNone(body["next"])

    def test_category_slug_uuid_and_unknown(self):
        other = Category.objects.create(name="Bonés", slug="bones")
        make_product(category=other)
        for category, count in (
            ("camisetas", 25),
            (str(self.category.id), 25),
            ("bones", 1),
            ("inexistente", 0),
        ):
            with self.subTest(category=category):
                body = self.client.get(self.url, {"category": category}).json()
                self.assertEqual(body["count"], count)

    def test_uuid_shaped_category_is_always_an_id(self):
        category = Category.objects.create(name="Slug UUID", slug=str(self.category.id))
        make_product(category=category)
        body = self.client.get(self.url, {"category": category.slug}).json()
        self.assertEqual(body["count"], 25)

    def test_search_finds_description_outside_first_page(self):
        body = self.client.get(self.url, {"search": "EXCLUSIVO"}).json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["id"], str(self.products[0].id))

    def test_search_name_and_inclusive_price_boundaries(self):
        body = self.client.get(
            self.url, {"search": "pRoDuTo 0", "min_price": "0", "max_price": "0"}
        ).json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["id"], str(self.products[0].id))

    def test_each_variation_filter_and_unknown_values(self):
        ProductVariation.objects.create(
            product=self.products[0], size="M", color="Azul", sku="FILTER-M"
        )
        for params, expected in (
            ({"size": "M"}, 1),
            ({"color": "Azul"}, 1),
            ({"size": "Inexistente"}, 0),
            ({"color": "Inexistente"}, 0),
        ):
            with self.subTest(params=params):
                body = self.client.get(self.url, params).json()
                self.assertEqual(body["count"], expected)

    def test_list_metadata_preserves_relation_ids(self):
        drop = DropCampaign.objects.create(name="Coleção", slug="colecao")
        product = self.products[0]
        product.drop = drop
        product.save()
        with self.assertNumQueries(4):
            body = self.client.get(self.url, {"search": "EXCLUSIVO"}).json()
        item = body["results"][0]
        self.assertEqual(item["category"], str(self.category.id))
        self.assertEqual(item["drop"], str(drop.id))
        self.assertEqual(item["category_details"]["slug"], "camisetas")
        self.assertEqual(item["drop_details"]["slug"], "colecao")

    def test_filter_options_cover_all_active_products_without_duplicates(self):
        inactive = make_product(is_active=False, base_price=9999)
        for product, size, color in (
            (self.products[0], "M", "Azul"),
            (self.products[-1], "M", "Azul"),
            (self.products[1], "G", ""),
            (inactive, "EXCLUSIVO", "Oculta"),
        ):
            ProductVariation.objects.create(
                product=product, size=size, color=color, sku=str(uuid.uuid4())
            )
        with self.assertNumQueries(3):
            response = self.client.get(self.url + "filter-options/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "min_price": "0.00",
                "max_price": "24.00",
                "sizes": ["G", "M"],
                "colors": ["Azul"],
            },
        )

    def test_empty_filter_options_and_nullable_list_metadata(self):
        Product.objects.all().delete()
        self.assertEqual(
            self.client.get(self.url + "filter-options/").json(),
            {
                "min_price": None,
                "max_price": None,
                "sizes": [],
                "colors": [],
            },
        )
        make_product()
        item = self.client.get(self.url).json()["results"][0]
        self.assertIsNone(item["category_details"])
        self.assertIsNone(item["drop_details"])

    def test_combined_filters_require_same_variation_without_duplicates(self):
        matching = self.products[10]
        for product, size, color in (
            (matching, "M", "Azul"),
            (matching, "M", "Preto"),
            (self.products[11], "M", "Branco"),
            (self.products[11], "G", "Azul"),
            (self.products[9], "M", "Azul"),
        ):
            ProductVariation.objects.create(
                product=product, size=size, color=color, sku=str(uuid.uuid4())
            )
        body = self.client.get(
            self.url + "?size=M&color=Azul&color=Preto&min_price=10&max_price=11"
        ).json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["id"], str(matching.id))

    def test_invalid_parameters_return_400(self):
        for params in (
            {"page": 0},
            {"page": "abc"},
            {"page": -1},
            {"page": 1.5},
            {"page_size": 0},
            {"page_size": "abc"},
            {"page_size": -2},
            {"min_price": "abc"},
            {"min_price": "NaN"},
            {"max_price": "Infinity"},
            {"min_price": -1},
            {"max_price": "1.234"},
            {"min_price": 10, "max_price": 9},
            {"ordering": "name"},
            {"ordering": "?"},
            {"drop": "invalid"},
            {"color": ""},
        ):
            with self.subTest(params=params):
                response = self.client.get(self.url, params)
                self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(self.client.get(self.url, {"page": 999}).status_code, 404)

    def test_price_ordering_and_stable_ties(self):
        for ordering, expected in (
            ("base_price", self.products[0]),
            ("-base_price", self.products[-1]),
        ):
            body = self.client.get(self.url, {"ordering": ordering}).json()
            self.assertEqual(body["results"][0]["id"], str(expected.id))
        Product.objects.update(created_at=timezone.now(), base_price=1)
        expected = sorted(str(product.id) for product in self.products)
        for ordering in ("-created_at", "base_price", "-base_price", "-sales_count"):
            body = self.client.get(
                self.url, {"ordering": ordering, "page_size": 100}
            ).json()
            self.assertEqual([item["id"] for item in body["results"]], expected)

    def test_related_data_is_loaded_in_constant_queries(self):
        with self.assertNumQueries(4):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_openapi_documents_query_and_paginated_response(self):
        schema = SchemaGenerator(
            patterns=[
                path("api/catalog/products/", ProductListCreateView.as_view()),
                path("api/catalog/products/filter-options/", CatalogFilterOptionsView.as_view()),
            ]
        ).get_schema(public=True)
        validate_schema(schema)
        filter_options_operation = schema["paths"][self.url + "filter-options/"]["get"]
        self.assertIn("200", filter_options_operation["responses"])
        operation = schema["paths"][self.url]["get"]
        names = {parameter["name"] for parameter in operation["parameters"]}
        self.assertTrue(
            {
                "page",
                "page_size",
                "category",
                "drop",
                "search",
                "size",
                "color",
                "min_price",
                "max_price",
                "ordering",
            }.issubset(names)
        )
        response_schema = operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        component = response_schema["$ref"].rsplit("/", 1)[1]
        self.assertEqual(
            set(schema["components"]["schemas"][component]["properties"]),
            {"count", "next", "previous", "results"},
        )

    def test_sales_sum_quantities_and_ignore_invalid_orders_with_filters(self):
        user = make_user("sales@example.com")
        winner, runner_up, invalid = self.products[:3]
        variations = {}
        for product in (winner, runner_up, invalid):
            variations[product.id] = [
                ProductVariation.objects.create(
                    product=product, size="M", color=color, sku=str(uuid.uuid4())
                )
                for color in ("Azul", "Preto")
            ]
        for order_status in OrderStatus.values:
            valid = order_status not in (
                OrderStatus.AWAITING_PAYMENT,
                OrderStatus.CANCELED,
            )
            order = CustomerOrder.objects.create(
                user=user,
                status=order_status,
                subtotal=100,
                total_amount=100,
                shipping_zip_code="01001000",
                shipping_street="Rua Teste",
                shipping_number="1",
                shipping_neighborhood="Centro",
                shipping_city="São Paulo",
                shipping_state="SP",
            )
            for product, quantity in (
                ((winner, 2), (runner_up, 1)) if valid else ((invalid, 100),)
            ):
                for variation in variations[product.id]:
                    OrderItem.objects.create(
                        order=order,
                        variation=variation,
                        quantity=quantity,
                        unit_price=1,
                        product_name=product.name,
                    )
        body = self.client.get(
            self.url + "?ordering=-sales_count&size=M&color=Azul&color=Preto"
        ).json()
        self.assertEqual(body["count"], 3)
        self.assertEqual(
            [item["id"] for item in body["results"]],
            [str(product.id) for product in (winner, runner_up, invalid)],
        )
        ranked = filter_catalog(Product.objects.all(), {"ordering": "-sales_count"})
        totals = dict(ranked.values_list("id", "sales_count"))
        self.assertEqual(totals[winner.id], 16)
        self.assertEqual(totals[runner_up.id], 8)
        self.assertEqual(totals[invalid.id], 0)
        self.assertEqual(totals[self.products[-1].id], 0)


class ProductDetailTests(APITestCase):
    """Testes para GET /api/catalog/products/{id}/."""

    def setUp(self):
        self.admin = make_user(
            "admin@x.com", role=UserRole.ADMIN, name="Admin", is_staff=True
        )
        self.customer = make_user("c@x.com", role=UserRole.CUSTOMER, name="Cliente")
        self.product = make_product(name="Camiseta")
        self.inativo = make_product(name="Inativo", is_active=False)

    def test_detalhe_publico_de_ativo(self):
        response = self.client.get(f"/api/catalog/products/{self.product.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["name"], "Camiseta")
        self.assertIn("variations", body)
        self.assertIn("images", body)

    def test_inativo_retorna_404_para_publico(self):
        response = self.client.get(f"/api/catalog/products/{self.inativo.id}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_inativo_retorna_404_para_customer(self):
        response = self.client.get(
            f"/api/catalog/products/{self.inativo.id}/",
            **auth_header(self.customer),
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_admin_ve_inativo(self):
        response = self.client.get(
            f"/api/catalog/products/{self.inativo.id}/",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_uuid_inexistente_404(self):
        response = self.client.get(f"/api/catalog/products/{uuid.uuid4()}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


# ─── Product — Create / Update / Delete ───────────────────────────────────────


class ProductCreateTests(APITestCase):
    url = "/api/catalog/products/"

    def setUp(self):
        self.admin = make_user(
            "admin@x.com", role=UserRole.ADMIN, name="Admin", is_staff=True
        )
        self.customer = make_user("c@x.com", role=UserRole.CUSTOMER, name="Cliente")

    def test_admin_cria_produto_simples(self):
        response = self.client.post(
            self.url,
            {"name": "Novo", "description": "x", "base_price": "99.90"},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["name"], "Novo")

    def test_admin_cria_com_variations_aninhadas(self):
        response = self.client.post(
            self.url,
            {
                "name": "Camisa",
                "description": "Algodão",
                "base_price": "120.00",
                "variations": [
                    {
                        "size": "P",
                        "color": "Azul",
                        "sku": "CAM-P",
                        "stock_quantity": 10,
                    },
                    {
                        "size": "M",
                        "color": "Vermelho",
                        "sku": "CAM-M",
                        "stock_quantity": 5,
                    },
                ],
            },
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(response.json()["variations"]), 2)
        variations = response.json()["variations"]
        self.assertEqual(variations[0]["color"], "Azul")
        self.assertEqual(variations[1]["color"], "Vermelho")

    def test_base_price_negativo_400(self):
        response = self.client.post(
            self.url,
            {"name": "X", "description": "x", "base_price": "-1"},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_sku_duplicado_400(self):
        ProductVariation.objects.create(
            product=make_product(), size="P", sku="DUP-1", stock_quantity=1
        )
        response = self.client.post(
            self.url,
            {
                "name": "Outro",
                "description": "x",
                "base_price": "10",
                "variations": [{"size": "P", "sku": "DUP-1", "stock_quantity": 1}],
            },
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_customer_nao_pode_criar(self):
        response = self.client.post(
            self.url,
            {"name": "X", "description": "x", "base_price": "1"},
            format="json",
            **auth_header(self.customer),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_sem_token_nao_pode_criar(self):
        response = self.client.post(
            self.url,
            {"name": "X", "description": "x", "base_price": "1"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class ProductUpdateTests(APITestCase):
    def setUp(self):
        self.admin = make_user(
            "admin@x.com", role=UserRole.ADMIN, name="Admin", is_staff=True
        )
        self.customer = make_user("c@x.com", role=UserRole.CUSTOMER, name="Cliente")
        self.product = make_product(name="Old")
        self.url = f"/api/catalog/products/{self.product.id}/"

    def test_admin_put_atualiza(self):
        response = self.client.put(
            self.url,
            {"name": "New", "description": "y", "base_price": "50"},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["name"], "New")

    def test_customer_nao_pode_atualizar(self):
        response = self.client.put(
            self.url,
            {"name": "x", "description": "y", "base_price": "1"},
            format="json",
            **auth_header(self.customer),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ProductDeleteTests(APITestCase):
    def setUp(self):
        self.admin = make_user(
            "admin@x.com", role=UserRole.ADMIN, name="Admin", is_staff=True
        )
        self.customer = make_user("c@x.com", role=UserRole.CUSTOMER, name="Cliente")
        self.product = make_product()
        self.url = f"/api/catalog/products/{self.product.id}/"

    def test_admin_remove(self):
        response = self.client.delete(self.url, **auth_header(self.admin))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Product.objects.filter(pk=self.product.id).exists())

    def test_customer_nao_pode_remover(self):
        response = self.client.delete(self.url, **auth_header(self.customer))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


# ─── Variation — CRUD ─────────────────────────────────────────────────────────


class VariationCRUDTests(APITestCase):
    def setUp(self):
        self.admin = make_user(
            "admin@x.com", role=UserRole.ADMIN, name="Admin", is_staff=True
        )
        self.customer = make_user("c@x.com", role=UserRole.CUSTOMER, name="Cliente")
        self.product = make_product()
        self.variation = ProductVariation.objects.create(
            product=self.product, size="P", sku="V-P", stock_quantity=10
        )
        self.create_url = f"/api/catalog/products/{self.product.id}/variations/"
        self.detail_url = f"/api/catalog/variations/{self.variation.id}/"

    def test_admin_cria_variacao(self):
        response = self.client.post(
            self.create_url,
            {"size": "M", "sku": "V-M", "stock_quantity": 5},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.product.variations.count(), 2)

    def test_sku_duplicado_400(self):
        ProductVariation.objects.create(
            product=make_product(name="Outro"), size="G", sku="DUP-X", stock_quantity=1
        )
        response = self.client.post(
            self.create_url,
            {"size": "M", "sku": "DUP-X", "stock_quantity": 1},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_admin_atualiza_variacao(self):
        response = self.client.put(
            self.detail_url,
            {"size": "G", "sku": "V-G", "stock_quantity": 20},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.size, "G")

    def test_admin_remove_variacao(self):
        response = self.client.delete(self.detail_url, **auth_header(self.admin))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ProductVariation.objects.filter(pk=self.variation.id).exists())

    def test_customer_nao_pode_criar(self):
        response = self.client.post(
            self.create_url,
            {"size": "M", "sku": "V-X", "stock_quantity": 1},
            format="json",
            **auth_header(self.customer),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_cria_variacao_com_cor(self):
        response = self.client.post(
            self.create_url,
            {"size": "M", "color": "Azul", "sku": "V-M-AZUL", "stock_quantity": 5},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.product.variations.count(), 2)
        v = ProductVariation.objects.get(sku="V-M-AZUL")
        self.assertEqual(v.color, "Azul")
        self.assertEqual(response.json()["color"], "Azul")

    def test_admin_atualiza_cor_variacao(self):
        response = self.client.put(
            self.detail_url,
            {"size": "P", "color": "Preto", "sku": "V-P-PRETO", "stock_quantity": 10},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.color, "Preto")
        self.assertEqual(response.json()["color"], "Preto")

    def test_variation_str_representation(self):
        self.assertEqual(str(self.variation), f"{self.product.name} - P")
        self.variation.color = "Verde"
        self.variation.save()
        self.assertEqual(str(self.variation), f"{self.product.name} - P / Verde")


# ─── Image — Persist / Delete ─────────────────────────────────────────────────


def make_product_image_file(name="img.jpg"):
    """JPEG válido 1x1 pra upload de ProductImage."""
    buf = BytesIO()
    Image.new("RGB", (1, 1), color="red").save(buf, format="JPEG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


class ImagePersistTests(APITestCase):
    def setUp(self):
        self.admin = make_user(
            "admin@x.com", role=UserRole.ADMIN, name="Admin", is_staff=True
        )
        self.customer = make_user("c@x.com", role=UserRole.CUSTOMER, name="Cliente")
        self.product = make_product()
        self.create_url = f"/api/catalog/products/{self.product.id}/images/"

    def test_primeira_imagem_recebe_display_order_1(self):
        response = self.client.post(
            self.create_url,
            {"image": make_product_image_file()},
            format="multipart",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["display_order"], 1)

    def test_segunda_imagem_recebe_display_order_2(self):
        self.client.post(
            self.create_url,
            {"image": make_product_image_file("a.jpg")},
            format="multipart",
            **auth_header(self.admin),
        )
        response = self.client.post(
            self.create_url,
            {"image": make_product_image_file("b.jpg")},
            format="multipart",
            **auth_header(self.admin),
        )
        self.assertEqual(response.json()["display_order"], 2)

    def test_extensao_invalida_400(self):
        buf = BytesIO()
        Image.new("RGB", (1, 1), color="red").save(buf, format="GIF")
        gif = SimpleUploadedFile("x.gif", buf.getvalue(), content_type="image/gif")
        response = self.client.post(
            self.create_url,
            {"image": gif},
            format="multipart",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("image", response.json()["details"])

    def test_tamanho_acima_de_5mb_400(self):
        buf = BytesIO()
        Image.new("RGB", (1, 1), color="red").save(buf, format="JPEG")
        content = buf.getvalue() + b"\x00" * (6 * 1024 * 1024)
        big = SimpleUploadedFile("big.jpg", content, content_type="image/jpeg")
        response = self.client.post(
            self.create_url,
            {"image": big},
            format="multipart",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_substitui_binario_e_mantem_ordem(self):
        post = self.client.post(
            self.create_url,
            {"image": make_product_image_file("antigo.jpg")},
            format="multipart",
            **auth_header(self.admin),
        )
        image_id = post.json()["id"]
        old_path = ProductImage.objects.get(pk=image_id).image.path
        self.assertTrue(os.path.exists(old_path))

        update_url = f"/api/catalog/products/{self.product.id}/images/{image_id}/"
        response = self.client.put(
            update_url,
            {"image": make_product_image_file("novo.jpg")},
            format="multipart",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(os.path.exists(old_path))
        self.assertEqual(response.json()["display_order"], 1)

    def test_delete_apaga_do_disco(self):
        post = self.client.post(
            self.create_url,
            {"image": make_product_image_file()},
            format="multipart",
            **auth_header(self.admin),
        )
        image_id = post.json()["id"]
        path = ProductImage.objects.get(pk=image_id).image.path
        self.assertTrue(os.path.exists(path))

        response = self.client.delete(
            f"/api/catalog/images/{image_id}/", **auth_header(self.admin)
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(os.path.exists(path))

    def test_customer_nao_pode_subir(self):
        response = self.client.post(
            self.create_url,
            {"image": make_product_image_file()},
            format="multipart",
            **auth_header(self.customer),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


# ─── Stock — Movement ─────────────────────────────────────────────────────────


class StockMovementTests(APITestCase):
    def setUp(self):
        self.admin = make_user(
            "admin@x.com", role=UserRole.ADMIN, name="Admin", is_staff=True
        )
        self.customer = make_user("c@x.com", role=UserRole.CUSTOMER, name="Cliente")
        self.product = make_product()
        self.variation = ProductVariation.objects.create(
            product=self.product, size="P", sku="ST-P", stock_quantity=10
        )
        self.url = f"/api/catalog/variations/{self.variation.id}/stock-movements/"

    def test_entrada_aumenta_estoque(self):
        response = self.client.post(
            self.url,
            {"kind": "ENTRADA", "reason": "COMPRA", "quantity": 5, "note": "NF 123"},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 15)
        self.assertEqual(response.json()["new_stock"], 15)

    def test_saida_reduz_estoque(self):
        response = self.client.post(
            self.url,
            {"kind": "SAIDA", "reason": "VENDA", "quantity": 4},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 6)

    def test_saida_insuficiente_400(self):
        response = self.client.post(
            self.url,
            {"kind": "SAIDA", "reason": "VENDA", "quantity": 100},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 10)

    def test_quantity_zero_400(self):
        response = self.client.post(
            self.url,
            {"kind": "ENTRADA", "reason": "COMPRA", "quantity": 0},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_created_by_eh_setado(self):
        self.client.post(
            self.url,
            {"kind": "ENTRADA", "reason": "AJUSTE", "quantity": 1},
            format="json",
            **auth_header(self.admin),
        )
        movement = StockMovement.objects.latest("created_at")
        self.assertEqual(movement.created_by_id, self.admin.id)

    def test_historico_listado_admin(self):
        self.client.post(
            self.url,
            {"kind": "ENTRADA", "reason": "COMPRA", "quantity": 1},
            format="json",
            **auth_header(self.admin),
        )
        response = self.client.get(self.url, **auth_header(self.admin))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["count"], 1)

    def test_customer_403(self):
        response = self.client.post(
            self.url,
            {"kind": "ENTRADA", "reason": "COMPRA", "quantity": 1},
            format="json",
            **auth_header(self.customer),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_sem_token_401(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
