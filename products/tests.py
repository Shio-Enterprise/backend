"""
Testes para os endpoints de Category e DropCampaign.

Executar com:
    pytest products/tests.py -v
"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as ModelValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.urls import path
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.validation import validate_schema
from PIL import Image
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from authentication.models import UserProfile, UserRole
from orders import tests as order_fixtures
from orders.models import CustomerOrder, OrderItem, OrderStatus
from orders.services import restore_order_stock, update_status

from .catalog import filter_catalog
from .models import (
    Category,
    DropCampaign,
    Product,
    ProductImage,
    ProductVariation,
    StockMovement,
)
from .serializers import StockMovementSerializer
from .services import create_variation, move_stock
from .views import (
    CatalogFilterOptionsView,
    ProductListCreateView,
    ProductRecommendationsView,
)

User = get_user_model()


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


def make_stocked_product(**kwargs):
    product = make_product(**kwargs)
    ProductVariation.objects.create(
        product=product, sku=f"available-{product.id}", stock_quantity=1
    )
    return product


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
        self.ativo = make_stocked_product(
            name="Camisa Branca", category=self.category, drop=self.drop
        )
        self.preto = make_stocked_product(name="Camisa Preta", category=self.category)
        self.inativo = make_stocked_product(name="Removido", is_active=False)
        self.sem_variacoes = make_product(name="Sem variações")
        self.esgotado = make_product(name="Esgotado")
        ProductVariation.objects.create(
            product=self.esgotado, sku="esgotado", stock_quantity=0
        )

    def test_listagem_publica_so_retorna_ativos(self):
        """Sem token, só produtos com is_active=True são retornados."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["count"], 2)
        names = [p["name"] for p in body["results"]]
        self.assertNotIn("Removido", names)

    def test_admin_ve_inativos(self):
        """Admin vê produtos inativos e sem estoque por default."""
        response = self.client.get(self.url, **auth_header(self.admin))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["count"], 5)
        self.assertEqual(
            {item["id"] for item in body["results"]},
            {str(product.id) for product in Product.objects.all()},
        )

    def test_listagem_publica_exige_estoque_sem_duplicar_produtos(self):
        for size, stock in (("M", 0), ("G", 2)):
            ProductVariation.objects.create(
                product=self.ativo,
                size=size,
                sku=f"ativo-{stock}",
                stock_quantity=stock,
            )
        for user in (None, self.customer):
            headers = auth_header(user) if user else {}
            for params in ({}, {"is_active": "true"}, {"is_active": "false"}):
                with self.subTest(user=user, params=params):
                    response = self.client.get(self.url, params, **headers)
                    self.assertEqual(response.status_code, status.HTTP_200_OK)
                    body = response.json()
                    self.assertEqual(body["count"], 2)
                    self.assertCountEqual(
                        [item["id"] for item in body["results"]],
                        [str(self.ativo.id), str(self.preto.id)],
                    )

    def test_public_filters_require_stock_in_selected_variation(self):
        ProductVariation.objects.create(
            product=self.ativo,
            size="M",
            color="Azul",
            sku="SOLD-OUT-M",
            stock_quantity=0,
        )
        ProductVariation.objects.create(
            product=self.ativo,
            size="G",
            color="Preto",
            sku="AVAILABLE-G",
            stock_quantity=2,
        )
        for params in (
            {"size": "M"},
            {"color": "Azul"},
            {"size": "M", "color": "Azul"},
        ):
            for user in (None, self.customer, self.admin):
                with self.subTest(params=params, user=user):
                    response = self.client.get(
                        self.url, params, **(auth_header(user) if user else {})
                    )
                    self.assertEqual(response.status_code, 200)
                    ids = [item["id"] for item in response.data["results"]]
                    self.assertEqual(str(self.ativo.id) in ids, user == self.admin)
        response = self.client.get(self.url, {"size": "G", "color": ["Azul", "Preto"]})
        self.assertEqual(
            [item["id"] for item in response.data["results"]], [str(self.ativo.id)]
        )

    def test_is_active_invalido_retorna_400(self):
        for user in (None, self.customer, self.admin):
            headers = auth_header(user) if user else {}
            for value in ("invalid", "", "2", "null"):
                with self.subTest(user=user, value=value):
                    response = self.client.get(
                        self.url, {"is_active": value}, **headers
                    )
                    self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                    self.assertIn("is_active", response.json())

    def test_admin_filtra_booleano_validado_sem_exigir_estoque(self):
        headers = auth_header(self.admin)
        for value, active in (
            ("true", True),
            ("false", False),
            ("1", True),
            ("0", False),
        ):
            with self.subTest(value=value):
                response = self.client.get(self.url, {"is_active": value}, **headers)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                expected = {
                    str(product.id)
                    for product in Product.objects.filter(is_active=active)
                }
                body = response.json()
                self.assertEqual(body["count"], len(expected))
                self.assertEqual({item["id"] for item in body["results"]}, expected)

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

        self.assertEqual(
            ids_returned[0], str(Product.objects.get(name="Camisa Preta").id)
        )


# ─── Product — Detail ─────────────────────────────────────────────────────────


class ProductListContractTests(APITestCase):
    url = "/api/catalog/products/"

    def setUp(self):
        self.category = Category.objects.create(name="Camisetas", slug="camisetas")
        self.products = [
            make_stocked_product(
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
        products = Product.objects.bulk_create(
            [Product(name=f"Extra {i}", base_price=1) for i in range(80)]
        )
        ProductVariation.objects.bulk_create(
            [
                ProductVariation(
                    product=product, sku=f"available-{product.id}", stock_quantity=1
                )
                for product in products
            ]
        )
        body = self.client.get(self.url, {"page_size": 999}).json()
        self.assertEqual(body["count"], 105)
        self.assertEqual(len(body["results"]), 50)
        self.assertIsNotNone(body["next"])

    def test_category_slug_uuid_and_unknown(self):
        other = Category.objects.create(name="Bonés", slug="bones")
        make_stocked_product(category=other)
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
        make_stocked_product(category=category)
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
            product=self.products[0],
            size="M",
            color="Azul",
            sku="FILTER-M",
            stock_quantity=1,
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
        make_stocked_product()
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
                product=product,
                size=size,
                color=color,
                sku=str(uuid.uuid4()),
                stock_quantity=1,
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
                path(
                    "api/catalog/products/filter-options/",
                    CatalogFilterOptionsView.as_view(),
                ),
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
                "is_active",
            }.issubset(names)
        )
        is_active_parameter = next(
            parameter
            for parameter in operation["parameters"]
            if parameter["name"] == "is_active"
        )
        self.assertEqual(is_active_parameter["in"], "query")
        self.assertEqual(is_active_parameter["schema"]["type"], "boolean")
        self.assertFalse(is_active_parameter.get("required", False))
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
                    product=product,
                    size="M",
                    color=color,
                    sku=str(uuid.uuid4()),
                    stock_quantity=1,
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

    def test_sales_ranking_tracks_each_order_status_and_ignores_payment_status(self):
        from orders.models import Payment, PaymentMethod, PaymentStatus

        product = self.products[0]
        variation = ProductVariation.objects.create(
            product=product, size="M", sku="ranking-status"
        )
        order = CustomerOrder.objects.create(
            user=make_user("ranking-status@example.com"),
            subtotal=100,
            total_amount=100,
            shipping_zip_code="01001000",
            shipping_street="Rua Teste",
            shipping_number="1",
            shipping_neighborhood="Centro",
            shipping_city="São Paulo",
            shipping_state="SP",
        )
        OrderItem.objects.create(
            order=order,
            variation=variation,
            quantity=7,
            unit_price=100,
            product_name=product.name,
        )
        payment = Payment.objects.create(
            order=order,
            method=PaymentMethod.PIX,
            status=PaymentStatus.PENDING,
            total_amount=100,
        )
        for order_status, expected in (
            (OrderStatus.AWAITING_PAYMENT, 0),
            (OrderStatus.PAID, 7),
            (OrderStatus.PREPARING, 7),
            (OrderStatus.SHIPPED, 7),
            (OrderStatus.DELIVERED, 7),
            (OrderStatus.CANCELED, 0),
        ):
            for payment_status in (PaymentStatus.PENDING, PaymentStatus.PAID):
                with self.subTest(order=order_status, payment=payment_status):
                    order.status = order_status
                    order.save(update_fields=["status"])
                    payment.status = payment_status
                    payment.save(update_fields=["status"])
                    ranked = filter_catalog(
                        Product.objects.all(), {"ordering": "-sales_count"}
                    )
                    self.assertEqual(ranked.get(pk=product.pk).sales_count, expected)

    def test_sales_ranking_precedes_pagination_and_has_stable_ties(self):
        # O produto mais antigo fica fora da primeira página por recência.
        winner, tied_a, tied_b = self.products[:3]
        order = CustomerOrder.objects.create(
            user=make_user("ranking-page@example.com"),
            status=OrderStatus.PAID,
            subtotal=100,
            total_amount=100,
            shipping_zip_code="01001000",
            shipping_street="Rua Teste",
            shipping_number="1",
            shipping_neighborhood="Centro",
            shipping_city="São Paulo",
            shipping_state="SP",
        )
        inactive = make_product(name="Inativo", is_active=False)
        for product, quantity in (
            (winner, 10),
            (tied_a, 5),
            (tied_b, 5),
            (inactive, 100),
        ):
            variation = ProductVariation.objects.create(
                product=product, size="M", sku=str(product.id)
            )
            OrderItem.objects.create(
                order=order,
                variation=variation,
                quantity=quantity,
                unit_price=100,
                product_name=product.name,
            )
        expected_ties = [str(tied_b.id), str(tied_a.id)]
        for same_date in (False, True):
            if same_date:
                Product.objects.filter(pk__in=[tied_a.pk, tied_b.pk]).update(
                    created_at=timezone.now()
                )
                expected_ties = sorted(expected_ties)
            with self.subTest(same_date=same_date):
                with self.assertNumQueries(4):
                    response = self.client.get(
                        self.url, {"ordering": "-sales_count", "page_size": 4}
                    )
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["count"], 25)
                self.assertEqual(len(body["results"]), 4)
                ids = [item["id"] for item in body["results"]]
                self.assertEqual(ids[:3], [str(winner.id), *expected_ties])
                self.assertNotIn(str(inactive.id), ids)
                self.assertIsNotNone(body["next"])


class ProductRecommendationTests(APITestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Camisetas", slug="camisetas")
        self.other_category = Category.objects.create(name="Calças", slug="calcas")
        self.drop = DropCampaign.objects.create(name="Drop", slug="drop")
        self.product = self.make_available(category=self.category, drop=self.drop)
        self.url = f"/api/catalog/products/{self.product.id}/recommendations/"

    def make_available(self, stock=2, **kwargs):
        product = make_product(**kwargs)
        ProductVariation.objects.create(
            product=product, size="M", sku=str(uuid.uuid4()), stock_quantity=stock
        )
        return product

    def assert_recommendations(self, expected, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            [item["id"] for item in body["results"]],
            [str(product.id) for product in expected],
        )
        return body

    def test_public_recommendations_require_active_products_with_stock(self):
        available = self.make_available(category=self.category)
        ProductVariation.objects.create(
            product=available, size="G", sku="second-stock", stock_quantity=3
        )
        self.make_available(category=self.category, is_active=False)
        self.make_available(category=self.category, stock=0)
        make_product(category=self.category)
        with self.assertNumQueries(5):
            body = self.assert_recommendations([available])
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["category_details"]["slug"], "camisetas")
        self.assertEqual(len(body["results"][0]["variations"]), 2)
        self.assertIn("images", body["results"][0])

    def test_category_then_drop_priority_precedes_recency(self):
        both = self.make_available(category=self.category, drop=self.drop)
        category_only = self.make_available(category=self.category)
        drop_only = self.make_available(category=self.other_category, drop=self.drop)
        general = self.make_available(category=self.other_category)
        self.assert_recommendations([both, category_only, drop_only, general])

    def test_fallback_fills_page_from_same_category_then_general_catalog(self):
        category_only = self.make_available(category=self.category)
        general = self.make_available(category=self.other_category)
        body = self.assert_recommendations([category_only, general])
        self.assertEqual(body["count"], 2)
        self.assertIsNone(body["next"])

    def test_fallback_to_general_catalog_when_category_is_unavailable(self):
        self.make_available(category=self.category, stock=0)
        self.make_available(category=self.category, is_active=False)
        older = self.make_available(category=self.other_category)
        newer = self.make_available()
        self.assert_recommendations([newer, older])

    def test_no_drop_does_not_prioritize_other_products_without_drop(self):
        self.product.drop = None
        self.product.save(update_fields=["drop"])
        without_drop = self.make_available(category=self.category)
        with_drop = self.make_available(category=self.category, drop=self.drop)
        self.assert_recommendations([with_drop, without_drop])

    def test_no_category_uses_drop_then_general_catalog(self):
        self.product.category = None
        self.product.save(update_fields=["category"])
        same_drop = self.make_available(category=self.category, drop=self.drop)
        without_category = self.make_available()
        general = self.make_available(category=self.other_category)
        self.assert_recommendations([same_drop, general, without_category])

    def test_sales_sum_all_variations_and_only_valid_order_statuses(self):
        winner = self.make_available(category=self.category, drop=self.drop)
        second_variation = ProductVariation.objects.create(
            product=winner, size="G", sku="sold-out-sales", stock_quantity=0
        )
        runner_up = self.make_available(category=self.category, drop=self.drop)
        unpaid = self.make_available(category=self.category, drop=self.drop)
        general = self.make_available(category=self.other_category)
        user = make_user("recommendations@example.com")
        for order_status in OrderStatus.values:
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
            valid = order_status in (
                OrderStatus.PAID,
                OrderStatus.PREPARING,
                OrderStatus.SHIPPED,
                OrderStatus.DELIVERED,
            )
            for variation, quantity in (
                (winner.variations.get(size="M"), 2 if valid else 0),
                (second_variation, 2 if valid else 0),
                (runner_up.variations.get(), 3 if valid else 0),
                (unpaid.variations.get(), 0 if valid else 100),
                (general.variations.get(), 100 if valid else 0),
            ):
                if quantity:
                    OrderItem.objects.create(
                        order=order,
                        variation=variation,
                        quantity=quantity,
                        unit_price=1,
                        product_name=variation.product.name,
                    )
        self.assert_recommendations([winner, runner_up, unpaid, general])

    def test_ties_use_recency_then_id(self):
        older = self.make_available()
        newer = self.make_available()
        Product.objects.filter(pk=older.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        self.assert_recommendations([newer, older])
        Product.objects.filter(pk__in=[older.pk, newer.pk]).update(
            created_at=timezone.now()
        )
        self.assert_recommendations(
            sorted([older, newer], key=lambda product: product.id)
        )

    def test_priorities_apply_before_pagination_with_a_safe_page_size_limit(self):
        preferred = self.make_available(category=self.category, drop=self.drop)
        others = [self.make_available() for _ in range(54)]
        expected = [preferred, *reversed(others)]
        body = self.assert_recommendations(expected[:4])
        self.assertEqual(body["count"], 55)
        self.assertIsNotNone(body["next"])
        self.assertIsNone(body["previous"])
        body = self.assert_recommendations(expected[4:8], page=2, page_size=4)
        self.assertIsNotNone(body["previous"])
        body = self.assert_recommendations(expected[:50], page_size=1000)
        self.assertEqual(body["count"], 55)
        body = self.assert_recommendations(expected[50:], page_size=1000, page=2)
        self.assertIsNone(body["next"])

    def test_invalid_pagination_and_missing_page(self):
        for name in ("page", "page_size"):
            for value in ("zero", "0", "-1", "1.5"):
                with self.subTest(parameter=name, value=value):
                    self.assertEqual(
                        self.client.get(self.url, {name: value}).status_code, 400
                    )
        self.assertEqual(self.client.get(self.url, {"page": 2}).status_code, 404)

    def test_missing_or_inactive_source_returns_404_even_for_admin(self):
        admin = make_user(
            "recommendations-admin@example.com", role=UserRole.ADMIN, is_staff=True
        )
        inactive = make_product(is_active=False)
        for headers in ({}, auth_header(admin)):
            for product_id in (uuid.uuid4(), inactive.id):
                with self.subTest(authenticated=bool(headers), product_id=product_id):
                    response = self.client.get(
                        f"/api/catalog/products/{product_id}/recommendations/",
                        **headers,
                    )
                    self.assertEqual(response.status_code, 404)

    def test_sold_out_source_can_still_receive_recommendations(self):
        self.product.variations.update(stock_quantity=0)
        available = self.make_available()
        self.assert_recommendations([available])

    def test_empty_catalog_returns_empty_paginated_response(self):
        self.assertEqual(
            self.assert_recommendations([]),
            {"count": 0, "next": None, "previous": None, "results": []},
        )

    def test_openapi_documents_paginated_recommendations(self):
        schema = SchemaGenerator(
            patterns=[
                path(
                    "api/catalog/products/<uuid:pk>/recommendations/",
                    ProductRecommendationsView.as_view(),
                ),
            ]
        ).get_schema(public=True)
        validate_schema(schema)
        operation = next(iter(schema["paths"].values()))["get"]
        self.assertTrue(
            {"page", "page_size"}.issubset(
                parameter["name"] for parameter in operation["parameters"]
            )
        )
        response_schema = operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        component = response_schema["$ref"].rsplit("/", 1)[1]
        self.assertEqual(
            set(schema["components"]["schemas"][component]["properties"]),
            {"count", "next", "previous", "results"},
        )


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
            {
                "name": "Novo",
                "description": "x",
                "cost_price": "50.00",
                "base_price": "99.90",
            },
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
                "cost_price": "50.00",
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
            {
                "name": "X",
                "description": "x",
                "cost_price": "50.00",
                "base_price": "-1",
            },
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
                "cost_price": "50.00",
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
            {"name": "X", "description": "x", "cost_price": "50.00", "base_price": "1"},
            format="json",
            **auth_header(self.customer),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_sem_token_nao_pode_criar(self):
        response = self.client.post(
            self.url,
            {"name": "X", "description": "x", "cost_price": "50.00", "base_price": "1"},
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
            {
                "name": "New",
                "description": "y",
                "cost_price": "50.00",
                "base_price": "50",
            },
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["name"], "New")

    def test_customer_nao_pode_atualizar(self):
        response = self.client.put(
            self.url,
            {"name": "x", "description": "y", "cost_price": "50.00", "base_price": "1"},
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

    def test_color_normalization_create_update_and_catalog(self):
        created = self.client.post(
            self.create_url,
            {"size": "M", "color": "  aZuL  ", "stock_quantity": 2},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["color"], "Azul")
        updated = self.client.put(
            self.detail_url,
            {"color": "AZUL"},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.color, "Azul")
        for color in ("Azul", "azul", " AZUL "):
            response = self.client.get("/api/catalog/products/", {"color": color})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                [item["id"] for item in response.data["results"]],
                [str(self.product.id)],
            )
        options = self.client.get("/api/catalog/products/filter-options/").data
        self.assertEqual(options["colors"], ["Azul"])

    def test_normalized_color_still_rejects_duplicate_combination(self):
        create_variation(self.product, {"size": "M", "color": "Azul"})
        response = self.client.post(
            self.create_url,
            {"size": "M", "color": " azul "},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, 400)

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
            {"size": "G"},
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.size, "G")

    def test_admin_remove_variacao(self):
        ProductVariation.objects.create(product=self.product, size="GG", sku="KEEP-GG")
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
            {"size": "P", "color": "Preto"},
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
            {
                "kind": "ENTRADA",
                "reason": "COMPRA",
                "idempotency_key": str(uuid.uuid4()),
                "note": "Ajuste de teste",
                "quantity": 5,
            },
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
            {
                "kind": "SAIDA",
                "reason": "AJUSTE",
                "idempotency_key": str(uuid.uuid4()),
                "note": "Ajuste de teste",
                "quantity": 4,
            },
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 6)

    def test_saida_insuficiente_400(self):
        response = self.client.post(
            self.url,
            {
                "kind": "SAIDA",
                "reason": "AJUSTE",
                "idempotency_key": str(uuid.uuid4()),
                "note": "Ajuste de teste",
                "quantity": 100,
            },
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 10)

    def test_quantity_zero_400(self):
        response = self.client.post(
            self.url,
            {
                "kind": "ENTRADA",
                "reason": "COMPRA",
                "idempotency_key": str(uuid.uuid4()),
                "note": "Ajuste de teste",
                "quantity": 0,
            },
            format="json",
            **auth_header(self.admin),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_created_by_eh_setado(self):
        self.client.post(
            self.url,
            {
                "kind": "ENTRADA",
                "reason": "AJUSTE",
                "idempotency_key": str(uuid.uuid4()),
                "note": "Ajuste de teste",
                "quantity": 1,
            },
            format="json",
            **auth_header(self.admin),
        )
        movement = StockMovement.objects.latest("created_at")
        self.assertEqual(movement.created_by_id, self.admin.id)

    def test_historico_listado_admin(self):
        self.client.post(
            self.url,
            {
                "kind": "ENTRADA",
                "reason": "COMPRA",
                "idempotency_key": str(uuid.uuid4()),
                "note": "Ajuste de teste",
                "quantity": 1,
            },
            format="json",
            **auth_header(self.admin),
        )
        response = self.client.get(self.url, **auth_header(self.admin))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["count"], 1)

    def test_customer_403(self):
        response = self.client.post(
            self.url,
            {
                "kind": "ENTRADA",
                "reason": "COMPRA",
                "idempotency_key": str(uuid.uuid4()),
                "note": "Ajuste de teste",
                "quantity": 1,
            },
            format="json",
            **auth_header(self.customer),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_sem_token_401(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class CatalogDecisionTests(APITestCase):
    def setUp(self):
        self.admin = make_user(
            "df-admin@example.com", role=UserRole.ADMIN, name="Admin"
        )
        self.client.force_authenticate(self.admin)
        self.url = "/api/catalog/products/"
        self.payload = {
            "name": "Camisa",
            "description": "Algodão",
            "base_price": "100.00",
            "cost_price": "60.00",
        }

    def create(self, **overrides):
        response = self.client.post(
            self.url, {**self.payload, **overrides}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def test_cost_required_zero_explicit_and_public_privacy(self):
        payload = dict(self.payload)
        del payload["cost_price"]
        self.assertEqual(
            self.client.post(self.url, payload, format="json").status_code, 400
        )
        data = self.create(cost_price="0.00", base_price="0.00")
        self.assertIsNone(data["margin_percent"])
        self.assertEqual(len(data["variations"]), 1)
        self.assertEqual(data["variations"][0]["size"], "Único")
        self.assertEqual(StockMovement.objects.count(), 0)
        move_stock(
            variation=ProductVariation.objects.get(pk=data["variations"][0]["id"]),
            kind="ENTRADA",
            reason="AJUSTE",
            quantity=1,
            origin_type="MANUAL_ADJUSTMENT",
            origin_id=uuid.uuid4(),
            idempotency_key=str(uuid.uuid4()),
            created_by=self.admin,
            note="Disponibilizar produto para verificar privacidade no catálogo público",
        )
        self.client.force_authenticate(None)
        for url in (self.url, f"{self.url}{data['id']}/"):
            public = self.client.get(url).data
            product = public["results"][0] if "results" in public else public
            for field in ("cost_price", "margin_amount", "margin_percent"):
                self.assertNotIn(field, product)

    def test_promotion_boundaries_and_margin(self):
        at = timezone.now()
        data = self.create(
            promotional_price="80.00",
            promo_start=at.isoformat(),
            promo_end=(at + timedelta(hours=1)).isoformat(),
        )
        product = Product.objects.get(pk=data["id"])
        self.assertEqual(
            product.price_at(at - timedelta(microseconds=1)), Decimal("100.00")
        )
        self.assertEqual(product.price_at(at), Decimal("80.00"))
        self.assertEqual(product.price_at(at + timedelta(hours=1)), Decimal("100.00"))
        self.assertEqual(data["margin_amount"], "20.00")
        self.assertEqual(data["margin_percent"], "25.00")
        url = f"{self.url}{product.id}/"
        self.assertEqual(
            self.client.patch(url, {"base_price": "70.00"}, format="json").status_code,
            400,
        )
        self.assertEqual(
            self.client.patch(url, {"mystery": 1}, format="json").status_code, 400
        )
        cleared = self.client.patch(url, {"promotional_price": None}, format="json")
        self.assertEqual(cleared.status_code, 200)
        self.assertIsNone(cleared.data["promo_start"])
        self.assertIsNone(cleared.data["promo_end"])

    def test_invalid_promotions_and_naive_time_rejected(self):
        for fields in (
            {"promotional_price": "100.00"},
            {"promotional_price": "0.00"},
            {"promotional_price": "80.00"},
            {"promo_start": timezone.now().isoformat()},
            {
                "promotional_price": "80.00",
                "promo_start": "2026-09-13T12:00:00",
                "promo_end": "2026-09-14T12:00:00",
            },
        ):
            self.assertEqual(
                self.client.post(
                    self.url, {**self.payload, **fields}, format="json"
                ).status_code,
                400,
            )

    def test_combination_normalization_atomicity_and_manual_sku(self):
        rows = [
            {"size": " M ", "color": "#ff0000", "sku": " cam-m ", "stock_quantity": 10},
            {"size": "m", "color": "#FF0000", "sku": "second", "stock_quantity": 3},
        ]
        response = self.client.post(
            self.url, {**self.payload, "variations": rows}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Product.objects.exists())
        self.assertFalse(StockMovement.objects.exists())
        rows[1]["color"] = "#000000"
        data = self.create(variations=rows)
        self.assertEqual({v["sku"] for v in data["variations"]}, {"CAM-M", "SECOND"})
        self.assertEqual(
            {v["color"] for v in data["variations"]}, {"#FF0000", "#000000"}
        )
        self.assertEqual(StockMovement.objects.count(), 2)
        v = data["variations"][0]
        for payload in ({"stock_quantity": 999}, {"sku": "CHANGE"}):
            self.assertEqual(
                self.client.put(
                    f"/api/catalog/variations/{v['id']}/", payload, format="json"
                ).status_code,
                400,
            )
        self.assertEqual(
            self.client.delete(f"{self.url}{data['id']}/").status_code, 400
        )

    def test_default_conversion_preserves_identity(self):
        data = self.create(variations=[{"stock_quantity": 5}])
        v = data["variations"][0]
        self.assertEqual(
            self.client.post(
                f"{self.url}{data['id']}/variations/", {"size": "M"}, format="json"
            ).status_code,
            400,
        )
        response = self.client.put(
            f"/api/catalog/variations/{v['id']}/",
            {"size": "M", "color": "Azul"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["sku"], v["sku"])
        self.assertEqual(response.data["stock_quantity"], 5)
        self.assertEqual(
            self.client.post(
                f"{self.url}{data['id']}/variations/",
                {"size": "G", "color": "Azul"},
                format="json",
            ).status_code,
            201,
        )

    def test_duplicate_colors_manual_sku_and_repeated_generation(self):
        data = self.create(
            variations=[
                {"size": "M", "color": "#ff0000", "stock_quantity": 7},
                {"size": "G", "color": "Azul"},
            ]
        )
        rows = [
            {"source_id": v["id"], "sku": "", "stock_quantity": 0}
            for v in data["variations"]
        ]
        rows[0].update(sku="novo-m", stock_quantity=5)
        url = f"{self.url}{data['id']}/duplicate/"
        copy = self.client.post(url, {"variations": rows}, format="json")
        self.assertEqual(copy.status_code, 201, copy.data)
        self.assertFalse(copy.data["is_active"])
        self.assertIsNone(copy.data["promotional_price"])
        self.assertEqual(
            {v["color"] for v in copy.data["variations"]}, {"#FF0000", "Azul"}
        )
        self.assertIn("NOVO-M", {v["sku"] for v in copy.data["variations"]})
        self.assertEqual(
            self.client.post(url, {"variations": rows}, format="json").status_code, 400
        )
        rows[0]["sku"] = ""
        again = self.client.post(url, {"variations": rows}, format="json")
        self.assertEqual(again.status_code, 201)
        self.assertFalse(
            {v["sku"] for v in again.data["variations"]}
            & {v["sku"] for v in copy.data["variations"]}
        )
        self.assertEqual(
            sum(
                v.stock_quantity
                for v in Product.objects.get(pk=data["id"]).variations.all()
            ),
            7,
        )

    def test_duplicate_images_are_independent(self):
        data = self.create()
        original = ProductImage.objects.create(
            product_id=data["id"], image=make_product_image_file(), display_order=1
        )
        rows = [{"source_id": v["id"]} for v in data["variations"]]
        response = self.client.post(
            f"{self.url}{data['id']}/duplicate/", {"variations": rows}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        copy = ProductImage.objects.get(product_id=response.data["id"])
        self.assertNotEqual(original.image.name, copy.image.name)
        with original.image.open("rb") as source, copy.image.open("rb") as target:
            self.assertEqual(source.read(), target.read())
        copy.delete()
        self.assertTrue(original.image.storage.exists(original.image.name))

    def test_ledger_balances_retry_conflict_and_compensation(self):
        data = self.create(variations=[{"stock_quantity": 10}])
        v = data["variations"][0]
        url = f"/api/catalog/variations/{v['id']}/stock-movements/"
        payload = {
            "kind": "SAIDA",
            "reason": "AJUSTE",
            "quantity": 3,
            "note": "Contagem",
            "idempotency_key": str(uuid.uuid4()),
        }
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.client.post(url, payload, format="json").status_code, 201)
        self.assertEqual(
            self.client.post(
                url, {**payload, "quantity": 2}, format="json"
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(
                url,
                {
                    **payload,
                    "kind": "ENTRADA",
                    "quantity": 2,
                    "idempotency_key": str(uuid.uuid4()),
                },
                format="json",
            ).status_code,
            201,
        )
        history = self.client.get(url).data["results"]
        self.assertEqual([m["balance_after"] for m in reversed(history)], [10, 7, 9])
        self.assertEqual([m["new_stock"] for m in reversed(history)], [10, 7, 9])
        movement = StockMovement.objects.first()
        movement.balance_after = 99
        with self.assertRaises(ModelValidationError):
            movement.save()
        with self.assertRaises(ModelValidationError):
            StockMovement.objects.filter(pk=movement.pk).update(balance_after=99)
        compensation = {
            **payload,
            "kind": "ENTRADA",
            "reverses_movement": response.data["id"],
            "idempotency_key": str(uuid.uuid4()),
        }
        self.assertEqual(
            self.client.post(url, compensation, format="json").status_code, 201
        )
        self.assertEqual(
            self.client.post(url, compensation, format="json").status_code, 201
        )

    def test_legacy_balance_is_unknown_and_next_movement_uses_actual_stock(self):
        product = Product.objects.create(**self.payload)
        variation = ProductVariation.objects.create(
            product=product, size="M", sku="LEGACY", stock_quantity=9
        )
        movement = StockMovement.objects.create(
            variation=variation,
            kind="ENTRADA",
            quantity=3,
            reason="COMPRA",
            is_legacy=True,
            origin_type="LEGACY",
            origin_id=uuid.uuid4(),
            idempotency_key="legacy-test",
        )
        self.assertIsNone(StockMovementSerializer(movement).data["new_stock"])
        result = move_stock(
            variation=variation,
            kind="SAIDA",
            quantity=2,
            reason="AJUSTE",
            origin_type="MANUAL_ADJUSTMENT",
            origin_id=uuid.uuid4(),
            idempotency_key="after-legacy",
            created_by=self.admin,
            note="Contagem",
        )
        self.assertEqual(result.balance_after, 7)


class CheckoutDecisionTests(APITestCase):
    setUp = order_fixtures.CheckoutAPITests.setUp

    @patch(
        "orders.views.create_infinitepay_checkout",
        return_value="https://example.test/pay",
    )
    def test_expiry_reconfirmation_snapshot_and_cancel_once(self, gateway):
        now = timezone.now()
        Product.objects.filter(pk=self.product.pk).update(
            cost_price=60,
            promotional_price=80,
            promo_start=now - timedelta(hours=2),
            promo_end=now - timedelta(hours=1),
        )
        payload = {"address_id": str(self.address.pk), "confirmed_subtotal": "160.00"}
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["subtotal"], "200.00")
        self.assertFalse(CustomerOrder.objects.exists())
        self.assertFalse(StockMovement.objects.exists())
        gateway.assert_not_called()
        response = self.client.post(
            self.url, {**payload, "confirmed_subtotal": "200.00"}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        order = CustomerOrder.objects.get()
        sale = StockMovement.objects.get(reason="VENDA")
        self.assertEqual(sale.balance_after, 8)
        self.assertEqual(sale.origin_id, order.pk)
        self.assertEqual(sale.order_item.unit_price, Decimal("100.00"))
        Product.objects.filter(pk=self.product.pk).update(base_price=200)
        self.assertEqual(order.items.get().unit_price, Decimal("100.00"))
        update_status(order, OrderStatus.CANCELED, changed_by=self.user)
        update_status(order, OrderStatus.CANCELED, changed_by=self.user)
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 10)
        self.assertEqual(StockMovement.objects.filter(reason="DEVOLUCAO").count(), 1)

    @patch(
        "orders.views.create_infinitepay_checkout",
        return_value="https://example.test/pay",
    )
    def test_shipped_cancel_waits_for_physical_return(self, gateway):
        self.client.post(
            self.url,
            {"address_id": str(self.address.pk), "confirmed_subtotal": "200.00"},
            format="json",
        )
        order = CustomerOrder.objects.get()
        update_status(order, OrderStatus.SHIPPED, tracking_code="TRACK")
        update_status(order, OrderStatus.CANCELED)
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 8)
        restore_order_stock(order, changed_by=self.user, physical_return=True)
        restore_order_stock(order, changed_by=self.user, physical_return=True)
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 10)

    @patch(
        "orders.views.create_infinitepay_checkout",
        side_effect=RuntimeError("Gateway offline"),
    )
    def test_gateway_failure_rolls_back_the_ledger(self, gateway):
        response = self.client.post(
            self.url,
            {"address_id": str(self.address.pk), "confirmed_subtotal": "200.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 500)
        self.assertFalse(StockMovement.objects.exists())
        self.assertFalse(CustomerOrder.objects.exists())
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 10)


class CatalogMigrationTests(TransactionTestCase):
    migrate_from = [
        ("products", "0004_productvariation_color"),
        ("orders", "0002_orderstatuslog"),
    ]
    migrate_to = [
        ("products", "0005_stockopeningbalance_alter_stockmovement_options_and_more")
    ]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        self.old_apps = executor.loader.project_state(self.migrate_from).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        super().tearDown()

    def test_migration_preserves_unknown_history_and_records_opening(self):
        Product = self.old_apps.get_model("products", "Product")
        Variation = self.old_apps.get_model("products", "ProductVariation")
        Movement = self.old_apps.get_model("products", "StockMovement")
        product = Product.objects.create(name="Legacy", base_price=100)
        variation = Variation.objects.create(
            product=product, size="Unico", sku="legacy-m", stock_quantity=9
        )
        movement = Movement.objects.create(
            variation=variation, kind="ENTRADA", reason="COMPRA", quantity=3
        )
        empty = Product.objects.create(name="Empty", base_price=50)
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps
        migrated = apps.get_model("products", "StockMovement").objects.get(
            pk=movement.pk
        )
        self.assertTrue(migrated.is_legacy)
        self.assertIsNone(migrated.balance_after)
        self.assertEqual(migrated.origin_id, movement.pk)
        opening = apps.get_model("products", "StockOpeningBalance").objects.get(
            variation_id=variation.pk
        )
        self.assertEqual(opening.balance, 9)
        self.assertEqual(
            apps.get_model("products", "ProductVariation")
            .objects.get(pk=variation.pk)
            .size,
            "Único",
        )
        self.assertEqual(
            apps.get_model("products", "ProductVariation")
            .objects.filter(product_id=empty.pk)
            .count(),
            1,
        )
        self.assertIsNone(
            apps.get_model("products", "Product").objects.get(pk=product.pk).cost_price
        )

    def test_collision_aborts_without_merging_stock(self):
        Product = self.old_apps.get_model("products", "Product")
        Variation = self.old_apps.get_model("products", "ProductVariation")
        product = Product.objects.create(name="Collision", base_price=100)
        first = Variation.objects.create(
            product=product, size="M", color="Azul", sku="FIRST", stock_quantity=4
        )
        second = Variation.objects.create(
            product=product, size=" m ", color="azul", sku="SECOND", stock_quantity=7
        )
        with self.assertRaisesRegex(RuntimeError, "Corrija o catálogo"):
            MigrationExecutor(connection).migrate(self.migrate_to)
        self.assertEqual(Variation.objects.get(pk=first.pk).stock_quantity, 4)
        self.assertEqual(Variation.objects.get(pk=second.pk).stock_quantity, 7)
        Variation.objects.filter(pk=second.pk).update(size="G")


class InventoryConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_two_concurrent_withdrawals_cannot_oversell(self):
        actor = make_user("concurrent@example.com", role=UserRole.ADMIN)
        product = Product.objects.create(
            name="Concurrent", base_price=100, cost_price=60
        )
        variation = create_variation(product, {"size": "M", "stock_quantity": 5}, actor)
        barrier = Barrier(2)

        def withdraw():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    move_stock(
                        variation=variation,
                        kind="SAIDA",
                        reason="AJUSTE",
                        quantity=4,
                        origin_type="MANUAL_ADJUSTMENT",
                        origin_id=uuid.uuid4(),
                        idempotency_key=str(uuid.uuid4()),
                        created_by=actor,
                        note="Concorrência",
                    )
                    return "ok"
                except ValidationError:
                    return "insufficient"
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: withdraw(), range(2)))
        self.assertCountEqual(outcomes, ["ok", "insufficient"])
        variation.refresh_from_db()
        self.assertEqual(variation.stock_quantity, 1)
        self.assertEqual(
            list(
                StockMovement.objects.filter(variation=variation)
                .order_by("sequence")
                .values_list("balance_after", flat=True)
            ),
            [5, 1],
        )
