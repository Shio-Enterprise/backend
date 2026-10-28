"""
Testes do detalhe público do drop por slug (`/drop/:slug` no frontend).

Executar com:
    pytest products/test_public_drop.py -v
"""

from rest_framework import status
from rest_framework.test import APITestCase

from orders.models import CustomerOrder, OrderItem, OrderStatus

from .models import DropCampaign, Product, ProductVariation
from .tests import auth_header, make_user

PUBLIC_URL = "/api/catalog/drops/slug/"


def make_order(user, status_=OrderStatus.PAID):
    return CustomerOrder.objects.create(
        user=user,
        subtotal=10,
        total_amount=10,
        status=status_,
        shipping_zip_code="71000000",
        shipping_street="R",
        shipping_number="1",
        shipping_neighborhood="B",
        shipping_city="C",
        shipping_state="DF",
    )


class PublicDropCampaignDetailTests(APITestCase):
    def setUp(self):
        self.admin = make_user("admin-drop-publico@shio.com", is_staff=True)
        self.cliente = make_user("cliente-drop-publico@shio.com")
        self.drop = DropCampaign.objects.create(
            name="Drop Público",
            slug="drop-publico",
            is_public=True,
            is_active=True,
        )

    def test_visitante_anonimo_ve_o_drop_pelo_slug(self):
        response = self.client.get(f"{PUBLIC_URL}{self.drop.slug}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["slug"], "drop-publico")
        self.assertIn("sold_quantity", data)
        self.assertIn("remaining_quantity", data)

    def test_slug_inexistente_retorna_404(self):
        response = self.client.get(f"{PUBLIC_URL}nao-existe/")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_drop_privado_retorna_404_mesmo_para_visitante_anonimo(self):
        privado = DropCampaign.objects.create(
            name="Privado", slug="privado", is_public=False
        )

        response = self.client.get(f"{PUBLIC_URL}{privado.slug}/")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_drop_privado_retorna_404_mesmo_para_admin(self):
        """A rota pública sempre aplica a visibilidade pública, mesmo para admin —
        quem quiser pré-visualizar um drop privado usa o detalhe por UUID."""
        privado = DropCampaign.objects.create(
            name="Privado", slug="privado-admin", is_public=False
        )

        response = self.client.get(
            f"{PUBLIC_URL}{privado.slug}/", **auth_header(self.admin)
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_so_lista_produtos_ativos_do_drop(self):
        Product.objects.create(
            name="Ativo", description="x", base_price=10, drop=self.drop, is_active=True
        )
        Product.objects.create(
            name="Inativo",
            description="x",
            base_price=10,
            drop=self.drop,
            is_active=False,
        )

        data = self.client.get(f"{PUBLIC_URL}{self.drop.slug}/").json()

        self.assertEqual([p["name"] for p in data["products"]], ["Ativo"])

    def test_produto_do_drop_vem_no_formato_de_card_do_catalogo(self):
        product = Product.objects.create(
            name="Camiseta", description="x", base_price=10, drop=self.drop
        )
        ProductVariation.objects.create(
            product=product, size="M", sku="DROP-M", stock_quantity=5
        )

        data = self.client.get(f"{PUBLIC_URL}{self.drop.slug}/").json()

        card = data["products"][0]
        self.assertIn("images", card)
        self.assertIn("variations", card)
        self.assertIn("is_sellable", card)

    def test_sem_max_quantity_remaining_quantity_e_nulo(self):
        data = self.client.get(f"{PUBLIC_URL}{self.drop.slug}/").json()

        self.assertIsNone(data["remaining_quantity"])
        self.assertEqual(data["sold_quantity"], 0)

    def test_remaining_quantity_desconta_unidades_vendidas(self):
        drop = DropCampaign.objects.create(
            name="Limitado", slug="limitado", is_public=True, max_quantity=10
        )
        product = Product.objects.create(
            name="Item", description="x", base_price=10, drop=drop
        )
        variation = ProductVariation.objects.create(
            product=product, size="U", sku="LIM-1", stock_quantity=20
        )
        order = make_order(self.cliente)
        OrderItem.objects.create(
            order=order,
            variation=variation,
            quantity=3,
            unit_price=10,
            product_name="Item - U",
        )

        data = self.client.get(f"{PUBLIC_URL}{drop.slug}/").json()

        self.assertEqual(data["sold_quantity"], 3)
        self.assertEqual(data["remaining_quantity"], 7)

    def test_pedido_cancelado_nao_conta_como_vendido(self):
        drop = DropCampaign.objects.create(
            name="Cancelado", slug="cancelado", is_public=True, max_quantity=5
        )
        product = Product.objects.create(
            name="Item2", description="x", base_price=10, drop=drop
        )
        variation = ProductVariation.objects.create(
            product=product, size="U", sku="CANC-1", stock_quantity=20
        )
        order = make_order(self.cliente, status_=OrderStatus.CANCELED)
        OrderItem.objects.create(
            order=order,
            variation=variation,
            quantity=4,
            unit_price=10,
            product_name="Item2 - U",
        )

        data = self.client.get(f"{PUBLIC_URL}{drop.slug}/").json()

        self.assertEqual(data["sold_quantity"], 0)
        self.assertEqual(data["remaining_quantity"], 5)

    def test_remaining_quantity_nao_fica_negativo_quando_ultrapassa_o_limite(self):
        drop = DropCampaign.objects.create(
            name="Superado", slug="superado", is_public=True, max_quantity=2
        )
        product = Product.objects.create(
            name="Item3", description="x", base_price=10, drop=drop
        )
        variation = ProductVariation.objects.create(
            product=product, size="U", sku="SUP-1", stock_quantity=20
        )
        order = make_order(self.cliente)
        OrderItem.objects.create(
            order=order,
            variation=variation,
            quantity=5,
            unit_price=10,
            product_name="Item3 - U",
        )

        data = self.client.get(f"{PUBLIC_URL}{drop.slug}/").json()

        self.assertEqual(data["remaining_quantity"], 0)
