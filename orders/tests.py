import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.exceptions import ValidationError

from authentication.models import Address, UserProfile, UserRole
from orders.models import (
    Cart,
    CartItem,
    Coupon,
    CustomerOrder,
    OrderItem,
    OrderStatus,
    OrderStatusLog,
    Payment,
    PaymentStatus,
)
from orders.services import (
    create_infinitepay_checkout,
    get_welcome_discount,
    get_welcome_discount_preview,
    update_status,
)
from products.models import Category, Product, ProductVariation

User = get_user_model()


class CheckoutAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="testador@shio.com", name="Testador", password="senha_forte_123"
        )
        self.client.force_authenticate(user=self.user)

        self.address = Address.objects.create(
            user=self.user,
            zip_code="71000000",
            street="Rua Teste",
            address_number="123",
            neighborhood="Centro",
            city="Brasília",
            state="DF",
        )

        self.category = Category.objects.create(name="Roupas", slug="roupas")
        self.product = Product.objects.create(
            category=self.category, name="Camiseta Teste", base_price=100.00
        )
        self.variation = ProductVariation.objects.create(
            product=self.product, size="M", sku="TESTE-M", stock_quantity=10
        )

        self.cart = Cart.objects.create(user=self.user, status="ACTIVE")
        self.cart_item = CartItem.objects.create(
            cart=self.cart, variation=self.variation, quantity=2, unit_price=100.00
        )

        self.url = "/api/orders/checkout/"

    @patch("orders.views.create_infinitepay_checkout")
    def test_checkout_sucesso_gera_pedido_e_reduz_estoque(self, mock_create_checkout):
        """Deve retornar 201, criar o pedido, finalizar o carrinho e deduzir estoque."""
        mock_create_checkout.return_value = "https://pay.infinitepay.io/mock-url"

        payload = {
            "address_id": str(self.address.id),
            "shipping_cost": 15.00,
            "confirmed_subtotal": "200.00",
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            response.json()["checkout_url"], "https://pay.infinitepay.io/mock-url"
        )

        self.cart.refresh_from_db()
        self.assertEqual(self.cart.status, "FINISHED")

        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 8)

        order = CustomerOrder.objects.get(user=self.user)
        self.assertEqual(order.status, OrderStatus.AWAITING_PAYMENT)
        self.assertEqual(order.total_amount, 195.00)
        self.assertEqual(order.discount_amount, 20.00)

    def test_checkout_com_carrinho_vazio_retorna_400(self):
        """Deve retornar 400 se o usuário não tiver itens no carrinho ativo."""
        self.cart.items.all().delete()

        payload = {
            "address_id": str(self.address.id),
            "shipping_cost": 15.00,
            "confirmed_subtotal": "200.00",
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(CustomerOrder.objects.count(), 0)

    def test_checkout_sem_estoque_faz_rollback_e_retorna_400(self):
        """Deve barrar a compra e garantir que o carrinho continua ativo e o estoque intacto."""
        self.cart_item.quantity = 20
        self.cart_item.save()

        payload = {
            "address_id": str(self.address.id),
            "shipping_cost": 15.00,
            "confirmed_subtotal": "2000.00",
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.cart.refresh_from_db()
        self.assertEqual(self.cart.status, "ACTIVE")

        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 10)

    @patch("orders.views.create_infinitepay_checkout")
    def test_checkout_falha_gateway_faz_rollback(self, mock_create_checkout):
        """Deve proteger o banco de dados se a API da InfinitePay cair."""
        mock_create_checkout.side_effect = Exception("InfinitePay Timeout")

        payload = {
            "address_id": str(self.address.id),
            "shipping_cost": 15.00,
            "confirmed_subtotal": "200.00",
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(CustomerOrder.objects.count(), 0)

        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 10)

    @patch("orders.views.create_infinitepay_checkout")
    def test_primeira_compra_aplica_desconto_de_boas_vindas(self, mock_create_checkout):
        mock_create_checkout.return_value = "https://pay.infinitepay.io/mock-url"

        payload = {
            "address_id": str(self.address.id),
            "shipping_cost": 15.00,
            "confirmed_subtotal": "200.00",
        }
        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = CustomerOrder.objects.get(user=self.user)
        self.assertEqual(order.discount_amount, 20.00)  # 10% de 200.00
        self.assertEqual(order.total_amount, 195.00)  # 200 - 20 + 15
        self.assertEqual(order.coupon.code, "BEMVINDO10")

    @patch("orders.views.create_infinitepay_checkout")
    def test_segunda_compra_nao_aplica_desconto(self, mock_create_checkout):
        mock_create_checkout.return_value = "https://pay.infinitepay.io/mock-url"

        CustomerOrder.objects.create(
            user=self.user,
            subtotal=50.00,
            total_amount=50.00,
            status=OrderStatus.PAID,
        )

        payload = {
            "address_id": str(self.address.id),
            "shipping_cost": 15.00,
            "confirmed_subtotal": "200.00",
        }
        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        new_order = (
            CustomerOrder.objects.filter(user=self.user).exclude(subtotal=50.00).first()
        )
        self.assertEqual(new_order.discount_amount, 0)
        self.assertIsNone(new_order.coupon)


class InfinitePayCardSimulationTests(APITestCase):
    """Simula o gateway InfinitePay (POST /links e /payment_check) via mock de
    requests.post, sem bater na rede nem exigir cartão real. Cobre o fluxo
    completo: checkout -> pagamento aprovado / recusado."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="cartao_teste@shio.com",
            name="Testador Cartão",
            password="senha_forte_123",
        )
        self.client.force_authenticate(user=self.user)

        self.address = Address.objects.create(
            user=self.user,
            zip_code="71000000",
            street="Rua Teste",
            address_number="123",
            neighborhood="Centro",
            city="Brasília",
            state="DF",
        )

        self.category = Category.objects.create(name="Roupas", slug="roupas")
        self.product = Product.objects.create(
            category=self.category, name="Camiseta Teste", base_price=100.00
        )
        self.variation = ProductVariation.objects.create(
            product=self.product, size="M", sku="TESTE-M", stock_quantity=10
        )

        self.cart = Cart.objects.create(user=self.user, status="ACTIVE")
        CartItem.objects.create(
            cart=self.cart, variation=self.variation, quantity=1, unit_price=100.00
        )

        self.checkout_url = "/api/orders/checkout/"
        self.success_url = "/api/orders/pagamento-sucesso/"

    @staticmethod
    def _fake_gateway_post(links_response, payment_check_response):
        """Roteia o mock de requests.post pra /links ou /payment_check
        conforme a URL chamada, como o gateway real faria."""

        def _post(url, json=None, headers=None, timeout=None):
            fake = type(
                "FakeResponse",
                (),
                {"raise_for_status": lambda self: None, "status_code": 200},
            )()
            if "payment_check" in url:
                fake.json = lambda: payment_check_response
            else:
                fake.json = lambda: links_response
            return fake

        return _post

    def _fazer_checkout(
        self, mock_post, checkout_link_url="https://checkout.infinitepay.io/mock"
    ):
        mock_post.side_effect = self._fake_gateway_post(
            links_response={"url": checkout_link_url}, payment_check_response={}
        )

        response = self.client.post(
            self.checkout_url,
            {
                "address_id": str(self.address.id),
                "shipping_cost": 15.00,
                "confirmed_subtotal": "100.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["checkout_url"], checkout_link_url)

        order = CustomerOrder.objects.get(user=self.user)
        self.assertEqual(order.status, OrderStatus.AWAITING_PAYMENT)
        self.assertEqual(order.payment.status, PaymentStatus.PROCESSING)
        return order

    @patch("orders.services.requests.post")
    def test_cartao_de_teste_aprovado_confirma_pagamento(self, mock_post):
        """Simula cartão aprovado: /payment_check retorna paid=True e o
        pedido deve virar PAID."""
        order = self._fazer_checkout(mock_post)

        mock_post.side_effect = self._fake_gateway_post(
            links_response={}, payment_check_response={"paid": True}
        )

        response = self.client.get(
            self.success_url,
            {
                "order_nsu": str(order.id),
                "transaction_nsu": "CARTAO_TESTE_APROVADO",
                "slug": "FATURA_TESTE",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAID)
        self.assertEqual(order.payment.status, PaymentStatus.PAID)
        self.assertEqual(order.payment.gateway_transaction_id, "CARTAO_TESTE_APROVADO")

    @patch("orders.services.requests.post")
    def test_cartao_de_teste_recusado_mantem_pedido_pendente(self, mock_post):
        """Simula cartão recusado: /payment_check retorna paid=False e o
        pedido deve continuar AWAITING_PAYMENT."""
        order = self._fazer_checkout(mock_post)

        mock_post.side_effect = self._fake_gateway_post(
            links_response={}, payment_check_response={"paid": False}
        )

        response = self.client.get(
            self.success_url,
            {
                "order_nsu": str(order.id),
                "transaction_nsu": "CARTAO_TESTE_RECUSADO",
                "slug": "FATURA_TESTE",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.AWAITING_PAYMENT)
        self.assertEqual(order.payment.status, PaymentStatus.PROCESSING)


class CreateInfinitePayCheckoutPayloadTests(APITestCase):
    """Garante que o payload enviado à InfinitePay cobra exatamente
    order.total_amount — em especial que o desconto de boas-vindas vira uma
    linha negativa no payload, em vez de o cliente ser cobrado o valor cheio.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            email="payload@shio.com", name="Payload", password="senha_forte_123"
        )
        self.category = Category.objects.create(name="Calçados", slug="calcados")
        self.product = Product.objects.create(
            category=self.category, name="Tênis Teste", base_price=Decimal("100.00")
        )
        self.variation = ProductVariation.objects.create(
            product=self.product, size="40", sku="TENIS-40", stock_quantity=10
        )
        self.factory = RequestFactory()

    def make_order(self, discount_amount):
        subtotal = Decimal("200.00")
        shipping_cost = Decimal("15.00")
        order = CustomerOrder.objects.create(
            user=self.user,
            subtotal=subtotal,
            shipping_cost=shipping_cost,
            discount_amount=discount_amount,
            total_amount=subtotal - discount_amount + shipping_cost,
            shipping_zip_code="71000000",
            shipping_street="Rua Teste",
            shipping_number="123",
            shipping_neighborhood="Centro",
            shipping_city="Brasília",
            shipping_state="DF",
        )
        OrderItem.objects.create(
            order=order,
            variation=self.variation,
            quantity=2,
            unit_price=Decimal("100.00"),
            product_name=self.product.name,
            sku_snapshot=self.variation.sku,
        )
        return order

    def call_service(self, order):
        """Chama create_infinitepay_checkout mockando apenas a chamada HTTP de
        saída, e devolve o payload realmente enviado ao gateway."""
        with patch("orders.services.requests.post") as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            mock_post.return_value.json.return_value = {
                "url": "https://pay.infinitepay.io/mock-url"
            }
            request = self.factory.get("/")
            url = create_infinitepay_checkout(order, request)

        self.assertEqual(url, "https://pay.infinitepay.io/mock-url")
        return mock_post.call_args.kwargs["json"]

    def test_payload_com_desconto_cobra_o_total_do_pedido(self):
        order = self.make_order(Decimal("20.00"))

        payload = self.call_service(order)

        total_cobrado = sum(
            item["price"] * item["quantity"] for item in payload["items"]
        )
        self.assertEqual(total_cobrado, int(order.total_amount * 100))

    def test_payload_com_desconto_adiciona_linha_negativa_apos_o_frete(self):
        order = self.make_order(Decimal("20.00"))

        payload = self.call_service(order)

        descriptions = [item["description"] for item in payload["items"]]
        self.assertEqual(
            descriptions, ["Tênis Teste", "Frete", "Desconto de boas-vindas"]
        )

        discount_line = payload["items"][-1]
        self.assertEqual(discount_line["quantity"], 1)
        self.assertEqual(discount_line["price"], -2000)

        # Os preços por produto continuam íntegros (itemização correta no recibo).
        self.assertEqual(payload["items"][0]["price"], 10000)
        self.assertEqual(payload["items"][0]["quantity"], 2)

    def test_payload_sem_desconto_nao_ganha_linha_de_desconto(self):
        order = self.make_order(Decimal("0.00"))

        payload = self.call_service(order)

        descriptions = [item["description"] for item in payload["items"]]
        self.assertEqual(descriptions, ["Tênis Teste", "Frete"])
        self.assertTrue(all(item["price"] > 0 for item in payload["items"]))

        total_cobrado = sum(
            item["price"] * item["quantity"] for item in payload["items"]
        )
        self.assertEqual(total_cobrado, int(order.total_amount * 100))


class PaymentSuccessRedirectTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="testador2@shio.com", password="123")

        self.order = CustomerOrder.objects.create(
            user=self.user,
            subtotal=100.00,
            total_amount=100.00,
            status=OrderStatus.AWAITING_PAYMENT,
            shipping_zip_code="000",
            shipping_street="X",
            shipping_number="1",
            shipping_neighborhood="Y",
            shipping_city="Z",
            shipping_state="DF",
        )

        self.payment = Payment.objects.create(
            order=self.order,
            method="CREDIT_CARD",
            status=PaymentStatus.PROCESSING,
            total_amount=100.00,
        )

        self.url = "/api/orders/pagamento-sucesso/"

    @patch("orders.views.check_payment_status")
    def test_pagamento_confirmado_pela_infinitepay(self, mock_check_payment):
        """Deve atualizar o pedido para PAID se o gateway confirmar."""
        mock_check_payment.return_value = {"paid": True}

        response = self.client.get(
            self.url,
            {
                "order_nsu": str(self.order.id),
                "transaction_nsu": "TRANS123",
                "slug": "FATURA123",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.order.refresh_from_db()
        self.payment.refresh_from_db()

        self.assertEqual(self.order.status, OrderStatus.PAID)
        self.assertEqual(self.payment.status, PaymentStatus.PAID)
        self.assertEqual(self.payment.gateway_transaction_id, "TRANS123")

    @patch("orders.views.check_payment_status")
    def test_pagamento_nao_confirmado_mantem_pendente(self, mock_check_payment):
        """Deve ignorar fraude se o gateway informar que não foi pago."""
        mock_check_payment.return_value = {"paid": False}

        response = self.client.get(
            self.url,
            {
                "order_nsu": str(self.order.id),
                "transaction_nsu": "FRAUDE123",
                "slug": "FATURA123",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.AWAITING_PAYMENT)

    def test_parametros_faltando_retorna_400(self):
        """Deve retornar erro se a query string estiver incompleta."""
        response = self.client.get(self.url, {"order_nsu": str(self.order.id)})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_pedido_nao_encontrado_retorna_404(self):
        """Deve retornar 404 para um UUID inexistente."""
        fake_uuid = str(uuid.uuid4())
        response = self.client.get(
            self.url,
            {
                "order_nsu": fake_uuid,
                "transaction_nsu": "TRANS123",
                "slug": "FATURA123",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class OrderTrackingViewTests(APITestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            email="cliente@shio.com", name="Cliente", password="senha_forte_123"
        )
        self.other_customer = User.objects.create_user(
            email="outro@shio.com", name="Outro Cliente", password="senha_forte_123"
        )
        self.admin = User.objects.create_user(
            email="admin@shio.com", name="Admin", is_staff=True
        )

        self.order_with_tracking = CustomerOrder.objects.create(
            user=self.customer,
            subtotal=100.00,
            total_amount=115.00,
            shipping_cost=15.00,
            status=OrderStatus.SHIPPED,
            tracking_code="BR123456789BR",
            shipping_zip_code="71000000",
            shipping_street="Rua Teste",
            shipping_number="10",
            shipping_neighborhood="Centro",
            shipping_city="Brasília",
            shipping_state="DF",
        )

        self.order_without_tracking = CustomerOrder.objects.create(
            user=self.customer,
            subtotal=100.00,
            total_amount=115.00,
            shipping_cost=15.00,
            status=OrderStatus.PREPARING,
            tracking_code=None,
            shipping_zip_code="71000000",
            shipping_street="Rua Teste",
            shipping_number="10",
            shipping_neighborhood="Centro",
            shipping_city="Brasília",
            shipping_state="DF",
        )

    def tracking_url(self, order_id):
        return f"/api/orders/correios/{order_id}/tracking/"

    @patch("orders.views.get_order_tracking_data")
    def test_cliente_consulta_rastreio_do_proprio_pedido_com_sucesso(
        self, mock_get_tracking
    ):
        """Cliente autenticado deve receber o histórico de rastreio do seu pedido."""
        mock_get_tracking.return_value = {
            "tracking_code": "BR123456789BR",
            "status_atual": "Objeto em trânsito",
            "previsao_entrega": "2024-06-10T18:00:00",
            "eventos": [
                {
                    "data": "2024-06-08T10:00:00",
                    "descricao": "Objeto postado",
                    "detalhe": "",
                    "local": "BRASILIA - DF",
                }
            ],
        }

        self.client.force_authenticate(user=self.customer)
        response = self.client.get(self.tracking_url(self.order_with_tracking.id))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["tracking_code"], "BR123456789BR")
        mock_get_tracking.assert_called_once_with("BR123456789BR")

    @patch("orders.views.get_order_tracking_data")
    def test_pedido_sem_codigo_de_rastreio_retorna_status_not_shipped(
        self, mock_get_tracking
    ):
        """Pedido ainda não despachado deve retornar status not_shipped sem erro."""
        mock_get_tracking.return_value = {
            "tracking_code": None,
            "status": "not_shipped",
            "eventos": [],
        }

        self.client.force_authenticate(user=self.customer)
        response = self.client.get(self.tracking_url(self.order_without_tracking.id))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["status"], "not_shipped")

    def test_cliente_nao_pode_consultar_rastreio_de_pedido_de_outro_cliente(self):
        """Cliente não deve conseguir acessar pedidos que não são seus."""
        self.client.force_authenticate(user=self.other_customer)
        response = self.client.get(self.tracking_url(self.order_with_tracking.id))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch("orders.views.get_order_tracking_data")
    def test_admin_pode_consultar_rastreio_de_qualquer_pedido(self, mock_get_tracking):
        """Admin deve conseguir consultar o rastreio de qualquer pedido."""
        mock_get_tracking.return_value = {
            "tracking_code": "BR123456789BR",
            "status_atual": "Objeto entregue ao destinatário",
            "previsao_entrega": None,
            "eventos": [],
        }

        self.client.force_authenticate(user=self.admin)
        response = self.client.get(self.tracking_url(self.order_with_tracking.id))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_usuario_nao_autenticado_recebe_401(self):
        """Requisição sem token JWT deve ser rejeitada."""
        response = self.client.get(self.tracking_url(self.order_with_tracking.id))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pedido_inexistente_retorna_404(self):
        """UUID que não existe no banco deve retornar 404."""
        self.client.force_authenticate(user=self.customer)
        response = self.client.get(self.tracking_url(uuid.uuid4()))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch("orders.views.get_order_tracking_data")
    def test_falha_na_api_dos_correios_retorna_503(self, mock_get_tracking):
        """Qualquer falha na integração com os Correios deve retornar 503."""
        mock_get_tracking.side_effect = Exception("Timeout conectando aos Correios")

        self.client.force_authenticate(user=self.customer)
        response = self.client.get(self.tracking_url(self.order_with_tracking.id))

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)


class OrderTrackingCodeAssignmentTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="admin@shio.com", name="Admin", is_staff=True
        )
        self.customer = User.objects.create_user(
            email="cliente@shio.com", name="Cliente", password="senha_forte_123"
        )
        self.order = CustomerOrder.objects.create(
            user=self.customer,
            subtotal=100.00,
            total_amount=115.00,
            shipping_cost=15.00,
            status=OrderStatus.PREPARING,
            tracking_code=None,
            shipping_zip_code="71000000",
            shipping_street="Rua Teste",
            shipping_number="10",
            shipping_neighborhood="Centro",
            shipping_city="Brasília",
            shipping_state="DF",
        )

    def tracking_url(self, order_id):
        return f"/api/orders/correios/{order_id}/tracking/"

    def test_admin_registra_codigo_de_rastreio_e_status_muda_para_shipped(self):
        """Admin deve conseguir vincular o código e o status deve mudar para SHIPPED."""
        self.client.force_authenticate(user=self.admin)

        response = self.client.patch(
            self.tracking_url(self.order.id),
            {"tracking_code": "BR123456789BR"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["tracking_code"], "BR123456789BR")

        self.order.refresh_from_db()
        self.assertEqual(self.order.tracking_code, "BR123456789BR")
        self.assertEqual(self.order.status, OrderStatus.SHIPPED)

    def test_admin_pode_corrigir_codigo_de_rastreio_ja_existente(self):
        """Admin deve conseguir sobrescrever um código de rastreio incorreto."""
        self.order.tracking_code = "BR000000000BR"
        self.order.status = OrderStatus.SHIPPED
        self.order.save()

        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(
            self.tracking_url(self.order.id),
            {"tracking_code": "BR123456789BR"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.tracking_code, "BR123456789BR")

    def test_cliente_nao_pode_registrar_codigo_de_rastreio(self):
        """Cliente não deve ter permissão para registrar código de rastreio."""
        self.client.force_authenticate(user=self.customer)

        response = self.client.patch(
            self.tracking_url(self.order.id),
            {"tracking_code": "BR123456789BR"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_tracking_code_vazio_retorna_400(self):
        """Enviar tracking_code em branco deve ser rejeitado."""
        self.client.force_authenticate(user=self.admin)

        response = self.client.patch(
            self.tracking_url(self.order.id),
            {"tracking_code": ""},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_nao_pode_registrar_rastreio_em_pedido_cancelado(self):
        """Pedido cancelado não deve aceitar código de rastreio."""
        self.order.status = OrderStatus.CANCELED
        self.order.save()

        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(
            self.tracking_url(self.order.id),
            {"tracking_code": "BR123456789BR"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_nao_pode_registrar_rastreio_em_pedido_entregue(self):
        """Pedido já entregue não deve aceitar código de rastreio."""
        self.order.status = OrderStatus.DELIVERED
        self.order.save()

        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(
            self.tracking_url(self.order.id),
            {"tracking_code": "BR123456789BR"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_pedido_inexistente_retorna_404(self):
        """UUID inválido deve retornar 404."""
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(
            self.tracking_url(uuid.uuid4()),
            {"tracking_code": "BR123456789BR"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class AdminDashboardViewTests(APITestCase):
    """Testes para o endpoint GET /api/orders/dashboard/summary/."""

    url = "/api/orders/dashboard/summary/"

    def setUp(self):
        self.admin = User.objects.create_user(
            email="admin@x.com", name="Admin", is_staff=True
        )
        UserProfile.objects.create(user=self.admin, role=UserRole.ADMIN)

        self.customer = User.objects.create_user(email="c@x.com", name="Cliente")
        UserProfile.objects.create(user=self.customer, role=UserRole.CUSTOMER)

    def auth_header(self, user):
        token = str(RefreshToken.for_user(user).access_token)
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def test_admin_acessa_dashboard_com_sucesso(self):
        """Admin deve poder aceder e receber o formato correto do dashboard."""
        response = self.client.get(self.url, **self.auth_header(self.admin))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("sales_summary", data)
        self.assertIn("customers_summary", data)
        self.assertIn("recent_orders", data)
        self.assertIn("low_stock_alerts", data)

    def test_cliente_nao_acessa_dashboard(self):
        """Utilizador sem permissão is_staff recebe 403."""
        response = self.client.get(self.url, **self.auth_header(self.customer))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class AdminOrderManagementTests(APITestCase):
    def setUp(self):
        # admin
        self.admin = User.objects.create_user(
            email="admin2@x.com", name="Admin2", is_staff=True
        )
        # customer
        self.user = User.objects.create_user(email="cliente@x.com", name="Cliente")

        # product/variation
        self.cat = Category.objects.create(name="Roupas2", slug="roupas2")
        self.prod = Product.objects.create(
            category=self.cat, name="Camiseta", base_price=50.00
        )
        self.variation = ProductVariation.objects.create(
            product=self.prod, size="M", sku="CAM-M", stock_quantity=5
        )

        # order with non-paid payment
        self.order = CustomerOrder.objects.create(
            user=self.user,
            subtotal=100.00,
            total_amount=100.00,
            status=OrderStatus.AWAITING_PAYMENT,
            shipping_zip_code="000",
            shipping_street="X",
            shipping_number="1",
            shipping_neighborhood="Y",
            shipping_city="Z",
            shipping_state="DF",
        )
        OrderItem.objects.create(
            order=self.order,
            variation=self.variation,
            quantity=2,
            unit_price=50.00,
            product_name="Camiseta M",
        )
        self.payment = Payment.objects.create(
            order=self.order,
            method="CREDIT_CARD",
            status=PaymentStatus.PROCESSING,
            total_amount=100.00,
        )

        # paid order
        self.paid_order = CustomerOrder.objects.create(
            user=self.user,
            subtotal=50.00,
            total_amount=50.00,
            status=OrderStatus.PAID,
            shipping_zip_code="111",
            shipping_street="Y",
            shipping_number="2",
            shipping_neighborhood="B",
            shipping_city="C",
            shipping_state="DF",
        )
        OrderItem.objects.create(
            order=self.paid_order,
            variation=self.variation,
            quantity=1,
            unit_price=50.00,
            product_name="Camiseta M",
        )
        Payment.objects.create(
            order=self.paid_order,
            method="PIX",
            status=PaymentStatus.PAID,
            total_amount=50.00,
        )

        self.admin_auth = {
            "HTTP_AUTHORIZATION": f"Bearer {str(RefreshToken.for_user(self.admin).access_token)}"
        }
        self.user_auth = {
            "HTTP_AUTHORIZATION": f"Bearer {str(RefreshToken.for_user(self.user).access_token)}"
        }

    def test_admin_list_filter_by_status(self):
        url = "/api/orders/admin/?status=PAID"
        response = self.client.get(url, **self.admin_auth)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        # only paid_order should be present
        ids = [o["id"] for o in data]
        self.assertIn(str(self.paid_order.id), ids)
        self.assertNotIn(str(self.order.id), ids)

    def test_non_admin_forbidden(self):
        url = "/api/orders/admin/"
        response = self.client.get(url, **self.user_auth)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_get_order_detail(self):
        url = f"/api/orders/admin/{self.order.id}/"
        response = self.client.get(url, **self.admin_auth)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["id"], str(self.order.id))
        self.assertIn("items", data)
        self.assertIn("payment", data)
        self.assertIn("status_logs", data)

    def test_admin_update_status_to_preparing(self):
        url = f"/api/orders/admin/{self.paid_order.id}/"
        payload = {"status": "PREPARING"}

        response = self.client.patch(url, payload, format="json", **self.admin_auth)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.paid_order.refresh_from_db()
        self.assertEqual(self.paid_order.status, OrderStatus.PREPARING)

    def test_admin_update_status_creates_audit_log(self):
        url = f"/api/orders/admin/{self.paid_order.id}/"
        payload = {"status": "PREPARING", "comment": "Iniciando separação"}
        response = self.client.patch(url, payload, format="json", **self.admin_auth)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(url, **self.admin_auth)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        self.assertEqual(len(data["status_logs"]), 1)
        log = data["status_logs"][0]
        self.assertEqual(log["previous_status"], OrderStatus.PAID)
        self.assertEqual(log["new_status"], OrderStatus.PREPARING)
        self.assertEqual(log["comment"], "Iniciando separação")
        self.assertEqual(log["changed_by"]["email"], self.admin.email)

    def test_admin_ship_stores_tracking_audit_log(self):
        self.paid_order.status = OrderStatus.PREPARING
        self.paid_order.save(update_fields=["status"])

        url = f"/api/orders/admin/{self.paid_order.id}/"
        payload = {
            "status": "SHIPPED",
            "tracking_code": "TRACK123",
            "comment": "Envio para correios",
        }

        response = self.client.patch(
            url,
            payload,
            format="json",
            **self.admin_auth,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.paid_order.refresh_from_db()
        self.assertEqual(self.paid_order.status, OrderStatus.SHIPPED)
        self.assertEqual(self.paid_order.tracking_code, "TRACK123")

        response = self.client.get(url, **self.admin_auth)
        log = response.json()["status_logs"][0]

        self.assertEqual(log["previous_status"], OrderStatus.PREPARING)
        self.assertEqual(log["new_status"], OrderStatus.SHIPPED)
        self.assertEqual(log["tracking_code"], "TRACK123")
        self.assertEqual(log["comment"], "Envio para correios")

    def test_admin_ship_requires_tracking_code(self):
        self.paid_order.status = OrderStatus.PREPARING
        self.paid_order.save(update_fields=["status"])

        url = f"/api/orders/admin/{self.paid_order.id}/"

        payload = {"status": "SHIPPED"}

        response = self.client.patch(
            url,
            payload,
            format="json",
            **self.admin_auth,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        payload = {
            "status": "SHIPPED",
            "tracking_code": "TRACK123",
        }

        response = self.client.patch(
            url,
            payload,
            format="json",
            **self.admin_auth,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.paid_order.refresh_from_db()

        self.assertEqual(self.paid_order.status, OrderStatus.SHIPPED)
        self.assertEqual(self.paid_order.tracking_code, "TRACK123")

    def test_admin_cancel_order_payment_not_confirmed(self):
        url = f"/api/orders/admin/{self.order.id}/"
        payload = {"status": "CANCELED"}
        response = self.client.patch(url, payload, format="json", **self.admin_auth)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELED)
        self.assertEqual(self.payment.status, PaymentStatus.FAILED)


class OrderDispatchViewTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="dispatch_admin@shio.com", name="Admin Dispatch", is_staff=True
        )
        self.customer = User.objects.create_user(
            email="dispatch_cliente@shio.com",
            name="Cliente Dispatch",
            password="senha_forte_123",
        )
        self.order = CustomerOrder.objects.create(
            user=self.customer,
            subtotal=100.00,
            total_amount=115.00,
            shipping_cost=15.00,
            status=OrderStatus.PREPARING,
            tracking_code=None,
            shipping_zip_code="71000000",
            shipping_street="Rua Teste",
            shipping_number="10",
            shipping_neighborhood="Centro",
            shipping_city="Brasília",
            shipping_state="DF",
        )
        self.payment = Payment.objects.create(
            order=self.order,
            method="PIX",
            status=PaymentStatus.PAID,
            total_amount=115.00,
        )

    def dispatch_url(self, order_id):
        return f"/api/orders/correios/{order_id}/despachar/"

    @patch("orders.views.dispatch_order_and_get_tracking_code")
    def test_admin_despacha_pedido_e_recebe_tracking_code(self, mock_dispatch):
        """Admin deve conseguir despachar o pedido e receber o código de rastreio gerado."""
        mock_dispatch.return_value = "BR123456789BR"

        self.client.force_authenticate(user=self.admin)
        response = self.client.post(self.dispatch_url(self.order.id))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["tracking_code"], "BR123456789BR")

        self.order.refresh_from_db()
        self.assertEqual(self.order.tracking_code, "BR123456789BR")
        self.assertEqual(self.order.status, OrderStatus.SHIPPED)

        from orders.models import OrderStatusLog

        log = OrderStatusLog.objects.filter(order=self.order).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.new_status, OrderStatus.SHIPPED)
        self.assertEqual(log.changed_by, self.admin)

    def test_cliente_nao_pode_despachar_pedido(self):
        """Cliente não deve ter permissão para despachar pedidos."""
        self.client.force_authenticate(user=self.customer)
        response = self.client.post(self.dispatch_url(self.order.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_pedido_inexistente_retorna_404(self):
        """UUID que não existe deve retornar 404."""
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(self.dispatch_url(uuid.uuid4()))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_pedido_already_shipped_retorna_400(self):
        """Pedido já em SHIPPED não deve ser despachado novamente."""
        self.order.status = OrderStatus.SHIPPED
        self.order.tracking_code = "BR000000000BR"
        self.order.save()

        self.client.force_authenticate(user=self.admin)
        response = self.client.post(self.dispatch_url(self.order.id))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_pedido_delivered_retorna_400(self):
        """Pedido já entregue não deve ser despachado."""
        self.order.status = OrderStatus.DELIVERED
        self.order.save()

        self.client.force_authenticate(user=self.admin)
        response = self.client.post(self.dispatch_url(self.order.id))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_pedido_cancelado_retorna_400(self):
        """Pedido cancelado não deve ser despachado."""
        self.order.status = OrderStatus.CANCELED
        self.order.save()

        self.client.force_authenticate(user=self.admin)
        response = self.client.post(self.dispatch_url(self.order.id))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("orders.views.dispatch_order_and_get_tracking_code")
    def test_falha_na_api_dos_correios_retorna_503(self, mock_dispatch):
        """Qualquer falha na chamada à API dos Correios deve retornar 503."""
        mock_dispatch.side_effect = Exception("Timeout nos Correios")

        self.client.force_authenticate(user=self.admin)
        response = self.client.post(self.dispatch_url(self.order.id))
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)


class OrderStateMachineAcceptanceTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="o6_admin@shio.com",
            name="Admin O6",
            is_staff=True,
        )

        self.customer = User.objects.create_user(
            email="o6_cliente@shio.com",
            name="Cliente O6",
            password="senha_forte_123",
        )

        self.admin_auth = {
            "HTTP_AUTHORIZATION": (
                f"Bearer "
                f"{str(RefreshToken.for_user(self.admin).access_token)}"
            )
        }

        self.customer_auth = {
            "HTTP_AUTHORIZATION": (
                f"Bearer "
                f"{str(RefreshToken.for_user(self.customer).access_token)}"
            )
        }

    def create_order(self, order_status):
        return CustomerOrder.objects.create(
            user=self.customer,
            subtotal=100.00,
            total_amount=115.00,
            shipping_cost=15.00,
            status=order_status,
            tracking_code=None,
            shipping_zip_code="71000000",
            shipping_street="Rua Teste",
            shipping_number="10",
            shipping_neighborhood="Centro",
            shipping_city="Brasília",
            shipping_state="DF",
        )

    def admin_url(self, order):
        return f"/api/orders/admin/{order.id}/"

    def dispatch_url(self, order):
        return f"/api/orders/correios/{order.id}/despachar/"

    def customer_url(self, order):
        return f"/api/orders/my-orders/{order.id}/"

    def test_todas_as_transicoes_permitidas(self):
        transitions = [
            (OrderStatus.AWAITING_PAYMENT, OrderStatus.PAID),
            (OrderStatus.AWAITING_PAYMENT, OrderStatus.CANCELED),
            (OrderStatus.PAID, OrderStatus.PREPARING),
            (OrderStatus.PREPARING, OrderStatus.SHIPPED),
            (OrderStatus.SHIPPED, OrderStatus.DELIVERED),
        ]

        for previous_status, new_status in transitions:
            with self.subTest(previous_status=previous_status, new_status=new_status):
                order = self.create_order(previous_status)

                tracking_code = (
                    "BR123456789BR"
                    if new_status == OrderStatus.SHIPPED
                    else None
                )

                update_status(
                    order=order,
                    new_status=new_status,
                    tracking_code=tracking_code,
                    changed_by=self.admin,
                )

                order.refresh_from_db()
                self.assertEqual(order.status, new_status)

                logs = OrderStatusLog.objects.filter(order=order)
                self.assertEqual(logs.count(), 1)

                log = logs.first()
                self.assertEqual(log.previous_status, previous_status)
                self.assertEqual(log.new_status, new_status)

    def test_cada_estado_rejeita_transicao_invalida(self):
        invalid_transitions = [
            (OrderStatus.AWAITING_PAYMENT, OrderStatus.SHIPPED),
            (OrderStatus.PAID, OrderStatus.DELIVERED),
            (OrderStatus.PREPARING, OrderStatus.DELIVERED),
            (OrderStatus.SHIPPED, OrderStatus.PREPARING),
            (OrderStatus.DELIVERED, OrderStatus.SHIPPED),
            (OrderStatus.CANCELED, OrderStatus.PAID),
        ]

        for previous_status, new_status in invalid_transitions:
            with self.subTest(previous_status=previous_status, new_status=new_status):
                order = self.create_order(previous_status)

                with self.assertRaises(ValidationError):
                    update_status(
                        order=order,
                        new_status=new_status,
                        changed_by=self.admin,
                    )

                order.refresh_from_db()

                self.assertEqual(order.status, previous_status)
                self.assertEqual(
                    OrderStatusLog.objects.filter(
                        order=order
                    ).count(),
                    0,
                )

    @patch("orders.views.dispatch_order_and_get_tracking_code")
    def test_pedido_aguardando_pagamento_nao_pode_ser_despachado(
        self,
        mock_dispatch,
    ):
        order = self.create_order(OrderStatus.AWAITING_PAYMENT)

        Payment.objects.create(
            order=order,
            method="PIX",
            status=PaymentStatus.PROCESSING,
            total_amount=115.00,
        )

        response = self.client.post(self.dispatch_url(order), **self.admin_auth)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        mock_dispatch.assert_not_called()

        order.refresh_from_db()

        self.assertEqual(order.status, OrderStatus.AWAITING_PAYMENT)
        self.assertIsNone(order.tracking_code)
        self.assertEqual(
            OrderStatusLog.objects.filter(
                order=order
            ).count(),
            0,
        )

    @patch("orders.views.dispatch_order_and_get_tracking_code")
    def test_pedido_com_pagamento_recusado_nao_pode_ser_despachado(
        self,
        mock_dispatch,
    ):
        order = self.create_order(OrderStatus.PREPARING)

        Payment.objects.create(
            order=order,
            method="PIX",
            status=PaymentStatus.FAILED,
            total_amount=115.00,
        )

        response = self.client.post(self.dispatch_url(order), **self.admin_auth)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        mock_dispatch.assert_not_called()

        order.refresh_from_db()

        self.assertEqual(order.status, OrderStatus.PREPARING)
        self.assertIsNone(order.tracking_code)
        self.assertEqual(
            OrderStatusLog.objects.filter(
                order=order
            ).count(),
            0,
        )

    def test_cada_mudanca_de_status_cria_somente_um_log(self):
        order = self.create_order(OrderStatus.PAID)
        update_status(
            order=order,
            new_status=OrderStatus.PREPARING,
            changed_by=self.admin,
        )

        self.assertEqual(
            OrderStatusLog.objects.filter(
                order=order
            ).count(),
            1,
        )

        update_status(
            order=order,
            new_status=OrderStatus.SHIPPED,
            tracking_code="BR123456789BR",
            changed_by=self.admin,
        )

        self.assertEqual(
            OrderStatusLog.objects.filter(
                order=order
            ).count(),
            2,
        )

        update_status(
            order=order,
            new_status=OrderStatus.DELIVERED,
            changed_by=self.admin,
        )

        self.assertEqual(
            OrderStatusLog.objects.filter(
                order=order
            ).count(),
            3,
        )

        logs = OrderStatusLog.objects.filter(order=order)

        self.assertEqual(
            logs.filter(
                new_status=OrderStatus.PREPARING
            ).count(),
            1,
        )
        self.assertEqual(
            logs.filter(
                new_status=OrderStatus.SHIPPED
            ).count(),
            1,
        )
        self.assertEqual(
            logs.filter(
                new_status=OrderStatus.DELIVERED
            ).count(),
            1,
        )

    def test_admin_executa_ciclo_completo_ate_entrega(self):
        order = self.create_order(OrderStatus.PAID)
        Payment.objects.create(
            order=order,
            method="PIX",
            status=PaymentStatus.PAID,
            total_amount=115.00,
        )
        url = self.admin_url(order)

        # PAID -> PREPARING
        response = self.client.patch(
            url,
            {
                "status": "PREPARING",
                "comment": "Pedido em separação",
            },
            format="json",
            **self.admin_auth,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # PREPARING -> SHIPPED
        response = self.client.patch(
            url,
            {
                "status": "SHIPPED",
                "tracking_code": "BR123456789BR",
                "comment": "Pedido enviado",
            },
            format="json",
            **self.admin_auth,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # SHIPPED -> DELIVERED
        response = self.client.patch(
            url,
            {
                "status": "DELIVERED",
                "comment": "Pedido entregue",
            },
            format="json",
            **self.admin_auth,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        order.refresh_from_db()

        self.assertEqual(order.status, OrderStatus.DELIVERED)
        self.assertEqual(order.tracking_code, "BR123456789BR")
        
        logs = OrderStatusLog.objects.filter(order=order)
        self.assertEqual(logs.count(), 3)

    def test_cliente_recebe_linha_do_tempo_na_ordem_correta(self):
        order = self.create_order(OrderStatus.PAID)
        Payment.objects.create(
            order=order,
            method="PIX",
            status=PaymentStatus.PAID,
            total_amount=115.00,
        )

        update_status(
            order=order,
            new_status=OrderStatus.PREPARING,
            changed_by=self.admin,
        )

        update_status(
            order=order,
            new_status=OrderStatus.SHIPPED,
            tracking_code="BR123456789BR",
            changed_by=self.admin,
        )

        update_status(
            order=order,
            new_status=OrderStatus.DELIVERED,
            changed_by=self.admin,
        )

        response = self.client.get(
            self.customer_url(order),
            **self.customer_auth,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()

        self.assertIn("status_logs", data)

        timeline = [log["new_status"] for log in data["status_logs"]]

        self.assertEqual(
            timeline,
            [
                OrderStatus.PREPARING,
                OrderStatus.SHIPPED,
                OrderStatus.DELIVERED,
            ],
        )

    def test_api_retorna_400_para_transicao_invalida(self):
        order = self.create_order(OrderStatus.AWAITING_PAYMENT)
        response = self.client.patch(
            self.admin_url(order),
            { "status": "DELIVERED" },
            format="json",
            **self.admin_auth,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        order.refresh_from_db()

        self.assertEqual(order.status, OrderStatus.AWAITING_PAYMENT)
        self.assertEqual(
            OrderStatusLog.objects.filter(
                order=order
            ).count(),
            0,
        )


class CepLookupViewTests(APITestCase):
    def cep_url(self, cep):
        return f"/api/orders/correios/cep/{cep}/"

    @patch("orders.correios_views.fetch_address_data_by_cep")
    def test_cep_valido_retorna_endereco(self, mock_fetch):
        mock_fetch.return_value = {
            "cep": "71000000",
            "uf": "DF",
            "localidade": "Brasília",
            "logradouro": "Rua Teste",
            "bairro": "Centro",
            "complemento": "",
        }

        response = self.client.get(self.cep_url("71000000"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["cep"], "71000000")
        self.assertEqual(data["cidade"], "Brasília")
        self.assertEqual(data["uf"], "DF")

    @patch("orders.correios_views.fetch_address_data_by_cep")
    def test_cep_com_hifen_e_normalizado_e_consultado(self, mock_fetch):
        mock_fetch.return_value = {
            "cep": "71000000",
            "uf": "DF",
            "localidade": "Brasília",
            "logradouro": "Rua Teste",
            "bairro": "Centro",
            "complemento": "",
        }

        response = self.client.get(self.cep_url("71000-000"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_fetch.assert_called_once_with("71000000")

    def test_cep_com_formato_invalido_retorna_400(self):
        response = self.client.get(self.cep_url("abc"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("orders.correios_views.fetch_address_data_by_cep")
    def test_cep_inexistente_retorna_404(self, mock_fetch):
        from orders.correios import CorreiosCepNotFoundError

        mock_fetch.side_effect = CorreiosCepNotFoundError("CEP não encontrado")

        response = self.client.get(self.cep_url("00000000"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch("orders.correios_views.fetch_address_data_by_cep")
    def test_falha_na_api_dos_correios_retorna_503(self, mock_fetch):
        mock_fetch.side_effect = Exception("Timeout")

        response = self.client.get(self.cep_url("71000000"))
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)


class ShippingOptionsViewTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="frete_user@shio.com",
            name="Usuario Frete",
            password="senha_forte_123",
        )
        self.url = "/api/orders/correios/frete/"

    @patch("orders.correios_views.fetch_shipping_price_by_service_and_ceps")
    @patch("orders.correios_views.fetch_shipping_deadline_by_service_and_ceps")
    def test_calcula_frete_com_sucesso(self, mock_prazo, mock_preco):
        mock_prazo.return_value = {
            "prazoEntrega": 3,
            "dataMaxima": "2026-06-20T23:58:00",
            "entregaDomiciliar": "S",
            "entregaSabado": "N",
        }
        mock_preco.return_value = {"pcFinal": "19,92", "psCobrado": "300"}

        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.url, {"cep_destino": "71000000"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["prazo_dias"], 3)
        self.assertEqual(data["preco_final"], "19,92")
        self.assertTrue(data["entrega_domiciliar"])

    def test_sem_cep_destino_retorna_400(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_usuario_nao_autenticado_retorna_401(self):
        response = self.client.get(self.url, {"cep_destino": "71000000"})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("orders.correios_views.fetch_shipping_deadline_by_service_and_ceps")
    def test_falha_na_api_dos_correios_retorna_503(self, mock_prazo):
        mock_prazo.side_effect = Exception("Timeout")

        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.url, {"cep_destino": "71000000"})
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)


class AgencySearchViewTests(APITestCase):
    url = "/api/orders/correios/agencias/"

    @patch("orders.correios_views.fetch_agencies_by_city_and_state")
    def test_busca_agencias_com_sucesso(self, mock_fetch):
        mock_fetch.return_value = {
            "itens": [
                {
                    "nome": "AC CENTRAL DE BRASILIA",
                    "endereco": {
                        "logradouro": "SBN",
                        "numero": "SN",
                        "bairro": "Asa Norte",
                        "localidade": "Brasília",
                        "uf": "DF",
                        "cep": "70002900",
                    },
                    "horarios": {
                        "funcionamento": "SEGUNDA À SEXTA",
                        "iniExpediente": "09:00",
                        "fimExpediente": "18:00",
                    },
                }
            ],
            "page": {"totalElements": 1, "totalPages": 1},
        }

        response = self.client.get(self.url, {"municipio": "Brasilia", "uf": "DF"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(len(data["agencias"]), 1)
        self.assertEqual(data["agencias"][0]["nome"], "AC CENTRAL DE BRASILIA")

    def test_sem_municipio_retorna_400(self):
        response = self.client.get(self.url, {"uf": "DF"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_sem_uf_retorna_400(self):
        response = self.client.get(self.url, {"municipio": "Brasilia"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("orders.correios_views.fetch_agencies_by_city_and_state")
    def test_falha_na_api_dos_correios_retorna_503(self, mock_fetch):
        mock_fetch.side_effect = Exception("Timeout")

        response = self.client.get(self.url, {"municipio": "Brasilia", "uf": "DF"})
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)


class CartAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="cliente_cart@shio.com", name="Cliente Cart", password="password_123"
        )
        self.category = Category.objects.create(name="Calçados", slug="calcados")
        self.product = Product.objects.create(
            category=self.category, name="Tênis Teste", base_price=150.00
        )
        self.variation = ProductVariation.objects.create(
            product=self.product, size="40", sku="TENIS-40", stock_quantity=5
        )
        self.variation_out_of_stock = ProductVariation.objects.create(
            product=self.product, size="41", sku="TENIS-41", stock_quantity=0
        )

        self.cart_url = "/api/orders/cart/"
        self.cart_items_url = "/api/orders/cart/items/"

    def test_anonymous_get_empty_cart(self):
        response = self.client.get(self.cart_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIsNone(data["id"])
        self.assertEqual(len(data["items"]), 0)
        self.assertEqual(float(data["subtotal"]), 0.00)

    def test_anonymous_add_item(self):
        response = self.client.post(
            self.cart_items_url,
            {"variation_id": str(self.variation.id), "quantity": 2},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(len(data["items"]), 1)


class MergeSessionCartTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="merge@shio.com", name="Merge", password="senha"
        )
        self.category = Category.objects.create(name="Test", slug="test")
        self.product = Product.objects.create(
            category=self.category, name="Produto", base_price=150.00
        )
        self.variation = ProductVariation.objects.create(
            product=self.product, size="M", sku="SKU-M", stock_quantity=5
        )
        self.variation_out_of_stock = ProductVariation.objects.create(
            product=self.product, size="L", sku="SKU-L", stock_quantity=0
        )
        # reuse the cart endpoints used elsewhere in tests
        self.cart_url = "/api/orders/cart/"
        self.cart_items_url = "/api/orders/cart/items/"

    def test_merge_respects_stock_limit(self):
        # DB cart already has quantity 4
        cart = Cart.objects.create(user=self.user, status="ACTIVE")
        CartItem.objects.create(
            cart=cart, variation=self.variation, quantity=4, unit_price=100.00
        )

        # Session has quantity 3 -> total 7 but should be capped at 5
        session = self.client.session
        session["cart"] = {
            str(self.variation.id): {"quantity": 3, "unit_price": "100.00"}
        }
        session.save()

        from types import SimpleNamespace

        from orders.services import merge_session_cart_to_db

        dummy_request = SimpleNamespace(session=session)
        merge_session_cart_to_db(dummy_request, self.user)

        cart_item = CartItem.objects.get(cart=cart, variation=self.variation)
        self.assertEqual(cart_item.quantity, 5)

    def test_merge_ignores_deleted_variations(self):
        # Session contains a variation id that no longer exists
        session = self.client.session
        session["cart"] = {"999999": {"quantity": 2, "unit_price": "10.00"}}
        session.save()

        from types import SimpleNamespace

        from orders.services import merge_session_cart_to_db

        dummy_request = SimpleNamespace(session=session)

        # Should not raise and should clear the session key for the missing variation
        merge_session_cart_to_db(dummy_request, self.user)

        self.assertNotIn("999999", self.client.session.get("cart", {}))
        self.assertEqual(self.client.session.get("cart", {}), {})

    def test_anonymous_add_duplicate_item(self):
        self.client.post(
            self.cart_items_url,
            {"variation_id": str(self.variation.id), "quantity": 1},
            format="json",
        )
        response = self.client.post(
            self.cart_items_url,
            {"variation_id": str(self.variation.id), "quantity": 2},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["items"][0]["quantity"], 3)
        self.assertEqual(float(data["subtotal"]), 450.00)

    def test_anonymous_add_insufficient_stock(self):
        response = self.client.post(
            self.cart_items_url,
            {"variation_id": str(self.variation_out_of_stock.id), "quantity": 1},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_anonymous_update_quantity(self):
        self.client.post(
            self.cart_items_url,
            {"variation_id": str(self.variation.id), "quantity": 2},
            format="json",
        )
        url = f"{self.cart_items_url}{self.variation.id}/"
        response = self.client.patch(url, {"quantity": 4}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["items"][0]["quantity"], 4)

    def test_anonymous_update_insufficient_stock(self):
        self.client.post(
            self.cart_items_url,
            {"variation_id": str(self.variation.id), "quantity": 2},
            format="json",
        )
        url = f"{self.cart_items_url}{self.variation.id}/"
        response = self.client.patch(url, {"quantity": 6}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_anonymous_remove_item(self):
        self.client.post(
            self.cart_items_url,
            {"variation_id": str(self.variation.id), "quantity": 2},
            format="json",
        )
        url = f"{self.cart_items_url}{self.variation.id}/"
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(len(data["items"]), 0)

    def test_anonymous_clear_cart(self):
        self.client.post(
            self.cart_items_url,
            {"variation_id": str(self.variation.id), "quantity": 2},
            format="json",
        )
        response = self.client.delete(self.cart_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(len(data["items"]), 0)

    def test_authenticated_get_empty_cart(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.cart_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(len(data["items"]), 0)

    def test_cart_expõe_elegibilidade_de_desconto_para_usuario_sem_pedidos(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.cart_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()["eligible_for_welcome_discount"])

    def test_cart_nao_expõe_desconto_para_usuario_com_pedido_anterior(self):
        self.client.force_authenticate(user=self.user)
        CustomerOrder.objects.create(
            user=self.user,
            subtotal=10.00,
            total_amount=10.00,
            status=OrderStatus.PAID,
        )
        response = self.client.get(self.cart_url)
        self.assertFalse(response.json()["eligible_for_welcome_discount"])
        self.assertEqual(response.json()["welcome_discount_amount"], "0.00")

    def test_authenticated_add_item(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            self.cart_items_url,
            {"variation_id": str(self.variation.id), "quantity": 2},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIsNotNone(data["id"])
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["quantity"], 2)

        # Check DB
        cart = Cart.objects.get(user=self.user, status="ACTIVE")
        self.assertEqual(cart.items.count(), 1)

    def test_authenticated_add_duplicate_item(self):
        self.client.force_authenticate(user=self.user)
        self.client.post(
            self.cart_items_url,
            {"variation_id": str(self.variation.id), "quantity": 1},
            format="json",
        )
        response = self.client.post(
            self.cart_items_url,
            {"variation_id": str(self.variation.id), "quantity": 2},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["items"][0]["quantity"], 3)

    def test_authenticated_update_quantity(self):
        self.client.force_authenticate(user=self.user)
        self.client.post(
            self.cart_items_url,
            {"variation_id": str(self.variation.id), "quantity": 2},
            format="json",
        )
        url = f"{self.cart_items_url}{self.variation.id}/"
        response = self.client.patch(url, {"quantity": 4}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["items"][0]["quantity"], 4)

    def test_authenticated_remove_item(self):
        self.client.force_authenticate(user=self.user)
        self.client.post(
            self.cart_items_url,
            {"variation_id": str(self.variation.id), "quantity": 2},
            format="json",
        )
        url = f"{self.cart_items_url}{self.variation.id}/"
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(len(data["items"]), 0)

    def test_authenticated_clear_cart(self):
        self.client.force_authenticate(user=self.user)
        self.client.post(
            self.cart_items_url,
            {"variation_id": str(self.variation.id), "quantity": 2},
            format="json",
        )
        response = self.client.delete(self.cart_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(len(data["items"]), 0)

    @patch("authentication.services.id_token.verify_oauth2_token")
    @patch(
        "authentication.services.settings.GOOGLE_CLIENT_ID",
        "test-client-id.apps.googleusercontent.com",
    )
    def test_login_merges_session_cart_to_db(self, mock_verify):
        mock_verify.return_value = {
            "sub": "google-oauth-12345",
            "email": "test_merge@example.com",
            "name": "Test Merge User",
            "picture": "https://example.com/avatar.jpg",
            "email_verified": True,
            "aud": "test-client-id.apps.googleusercontent.com",
        }

        # Add item to session cart (anonymous)
        response = self.client.post(
            self.cart_items_url,
            {"variation_id": str(self.variation.id), "quantity": 2},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Login with Google
        login_url = "/api/auth/google/"
        login_response = self.client.post(
            login_url, {"id_token": "valid-token"}, format="json"
        )
        self.assertEqual(login_response.status_code, status.HTTP_201_CREATED)

        # Verify session is cleared
        self.assertNotIn("cart", self.client.session)

        # Verify DB cart contains merged items
        user = User.objects.get(email="test_merge@example.com")
        cart = Cart.objects.get(user=user, status="ACTIVE")
        cart_item = CartItem.objects.get(cart=cart, variation=self.variation)
        self.assertEqual(cart_item.quantity, 2)

    @patch("authentication.services.id_token.verify_oauth2_token")
    @patch(
        "authentication.services.settings.GOOGLE_CLIENT_ID",
        "test-client-id.apps.googleusercontent.com",
    )
    def test_login_merges_session_cart_with_existing_db_cart(self, mock_verify):
        # Create user and active cart in DB
        user = User.objects.create_user(
            email="test_merge_existing@example.com",
            name="Existing Merge User",
            google_id="google-oauth-123456",
        )
        db_cart = Cart.objects.create(user=user, status="ACTIVE")
        CartItem.objects.create(
            cart=db_cart, variation=self.variation, quantity=1, unit_price=150.00
        )

        mock_verify.return_value = {
            "sub": "google-oauth-123456",
            "email": "test_merge_existing@example.com",
            "name": "Existing Merge User",
            "picture": "https://example.com/avatar.jpg",
            "email_verified": True,
            "aud": "test-client-id.apps.googleusercontent.com",
        }

        # Add item to session cart (anonymous)
        self.client.post(
            self.cart_items_url,
            {"variation_id": str(self.variation.id), "quantity": 2},
            format="json",
        )

        # Login
        login_url = "/api/auth/google/"
        login_response = self.client.post(
            login_url, {"id_token": "valid-token"}, format="json"
        )
        self.assertEqual(login_response.status_code, status.HTTP_200_OK)

        # Verify DB cart quantity is summed
        cart_item = CartItem.objects.get(cart=db_cart, variation=self.variation)
        self.assertEqual(cart_item.quantity, 3)


class WelcomeCouponSeedTests(APITestCase):
    def test_seed_cria_cupom_bemvindo10(self):
        from orders.models import Coupon

        coupon = Coupon.objects.filter(code="BEMVINDO10").first()
        self.assertIsNotNone(coupon)
        self.assertEqual(coupon.discount_type, "PERCENTAGE")
        self.assertEqual(coupon.discount_value, 10)
        self.assertTrue(coupon.is_active)


class GetWelcomeDiscountConcurrencyTests(APITestCase):
    """Cobre o risco de corrida entre dois checkouts concorrentes do mesmo
    usuário que nunca comprou antes: ambos não podem aplicar o desconto de
    boas-vindas (ver get_welcome_discount em orders/services.py).

    Nota sobre a estratégia de teste: o banco usado nos testes é SQLite em
    memória. Nesse backend, `django.db.models.QuerySet.select_for_update()`
    é essencialmente um no-op — a feature `has_select_for_update` é False
    para o SQLite, então o Django nem adiciona a cláusula `FOR UPDATE` nem
    valida que a chamada está dentro de uma transação (ver
    django/db/models/sql/compiler.py, condição
    `self.query.select_for_update and features.has_select_for_update`).
    Ou seja: um teste com threads reais batendo no SQLite não provaria nada
    sobre o `select_for_update` em si (ele não bloqueia lá) — só mostraria
    uma peculiaridade de locking do SQLite, o que tornaria o teste flaky e
    não relacionado ao comportamento real de produção (Postgres, onde
    `SELECT ... FOR UPDATE` bloqueia de verdade e serializa as transações).

    Por isso o teste abaixo prova, de forma determinística, que o lock
    "governa" a checagem: dentro de uma única transaction.atomic() — a mesma
    seção que, em Postgres, uma segunda transação concorrente só atravessaria
    depois que a primeira commitasse — criamos o pedido da primeira "checkout"
    e então chamamos get_welcome_discount() de novo, simulando a checagem que
    a segunda transação concorrente faria ao ser liberada pelo lock. Ela deve
    enxergar o pedido recém-criado e negar o desconto, confirmando que a
    ordem lock -> checagem -> criação está correta e que, em um banco com
    locking real, isso serializa as duas requisições concorrentes.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            email="concorrencia@shio.com",
            name="Concorrencia",
            password="senha_forte_123",
        )

    def test_lock_do_usuario_governa_checagem_de_primeira_compra(self):
        with transaction.atomic():
            User.objects.select_for_update().get(pk=self.user.pk)

            coupon, discount = get_welcome_discount(self.user, Decimal("200.00"))
            self.assertIsNotNone(coupon)
            self.assertEqual(coupon.code, "BEMVINDO10")
            self.assertEqual(discount, Decimal("20.00"))

            CustomerOrder.objects.create(
                user=self.user,
                coupon=coupon,
                subtotal=Decimal("200.00"),
                discount_amount=discount,
                total_amount=Decimal("180.00"),
                status=OrderStatus.AWAITING_PAYMENT,
            )

            # Simula a segunda transação concorrente retomando após o lock:
            # ela deve enxergar o pedido acabado de criar e não conceder
            # desconto duplicado.
            coupon2, discount2 = get_welcome_discount(self.user, Decimal("200.00"))
            self.assertIsNone(coupon2)
            self.assertEqual(discount2, Decimal("0.00"))

    def test_helper_bloqueia_linha_do_usuario_antes_de_checar_pedidos_anteriores(self):
        """Prova, via SQL de fato executado, que get_welcome_discount adquire
        o lock na linha do usuário (SELECT ... na tabela de usuário via
        select_for_update) ANTES de consultar se ele já possui pedido. Essa
        ordem é o que, em um banco com locking real (Postgres em produção),
        serializa dois checkouts concorrentes do mesmo usuário — a segunda
        transação bloqueia no lock até a primeira commitar. Diferente do
        teste acima (que só confirma o resultado final e passaria mesmo sem
        o lock, já que o SQLite ignora select_for_update), este teste
        garante que uma remoção acidental do select_for_update() quebre o
        CI, checando a ordem real das queries emitidas."""
        User = get_user_model()
        user_table = User._meta.db_table
        order_table = CustomerOrder._meta.db_table

        with transaction.atomic():
            with CaptureQueriesContext(connection) as ctx:
                get_welcome_discount(self.user, Decimal("200.00"))

        queries = [q["sql"] for q in ctx.captured_queries]
        user_query_index = next(
            (i for i, sql in enumerate(queries) if user_table in sql), None
        )
        order_query_index = next(
            (i for i, sql in enumerate(queries) if order_table in sql), None
        )

        self.assertIsNotNone(
            user_query_index, "Esperava uma query de lock na tabela de usuário."
        )
        self.assertIsNotNone(
            order_query_index,
            "Esperava uma query checando pedidos anteriores do usuário.",
        )
        self.assertLess(
            user_query_index,
            order_query_index,
            "O lock select_for_update na linha do usuário deve ocorrer antes "
            "da checagem de pedidos anteriores (CustomerOrder.objects...exists()).",
        )


class WelcomeDiscountCouponRulesTests(APITestCase):
    """Cobre as regras do cupom que antes eram ignoradas pelos helpers de
    desconto: expiration_date e discount_type (PERCENTAGE vs FIXED_VALUE).
    """

    def setUp(self):
        self.user = User.objects.create_user(
            email="cupom@shio.com", name="Cupom", password="senha_forte_123"
        )
        self.coupon = Coupon.objects.get(code="BEMVINDO10")

    def test_cupom_sem_expiracao_continua_valido(self):
        coupon, discount = get_welcome_discount_preview(self.user, Decimal("200.00"))

        self.assertIsNotNone(coupon)
        self.assertEqual(discount, Decimal("20.00"))

    def test_cupom_com_expiracao_futura_continua_valido(self):
        self.coupon.expiration_date = timezone.now() + timedelta(days=1)
        self.coupon.save()

        coupon, discount = get_welcome_discount_preview(self.user, Decimal("200.00"))

        self.assertIsNotNone(coupon)
        self.assertEqual(discount, Decimal("20.00"))

    def test_cupom_expirado_nao_concede_desconto(self):
        self.coupon.expiration_date = timezone.now() - timedelta(days=1)
        self.coupon.save()

        coupon, discount = get_welcome_discount_preview(self.user, Decimal("200.00"))

        self.assertIsNone(coupon)
        self.assertEqual(discount, Decimal("0.00"))

    def test_cupom_expirado_tambem_bloqueia_no_checkout(self):
        """A versão com lock (usada no checkout) compartilha o mesmo helper,
        então também precisa respeitar a expiração."""
        self.coupon.expiration_date = timezone.now() - timedelta(days=1)
        self.coupon.save()

        with transaction.atomic():
            coupon, discount = get_welcome_discount(self.user, Decimal("200.00"))

        self.assertIsNone(coupon)
        self.assertEqual(discount, Decimal("0.00"))

    def test_cupom_fixed_value_aplica_valor_fixo(self):
        # Alteração restrita a esta transação de teste (rollback no tearDown),
        # simulando alguém editando o cupom pelo admin.
        self.coupon.discount_type = "FIXED_VALUE"
        self.coupon.discount_value = Decimal("30.00")
        self.coupon.save()

        coupon, discount = get_welcome_discount_preview(self.user, Decimal("200.00"))

        self.assertIsNotNone(coupon)
        self.assertEqual(discount, Decimal("30.00"))

    def test_cupom_fixed_value_maior_que_subtotal_e_limitado_ao_subtotal(self):
        self.coupon.discount_type = "FIXED_VALUE"
        self.coupon.discount_value = Decimal("500.00")
        self.coupon.save()

        coupon, discount = get_welcome_discount_preview(self.user, Decimal("200.00"))

        self.assertIsNotNone(coupon)
        self.assertEqual(discount, Decimal("200.00"))

    def test_preview_e_versao_com_lock_retornam_o_mesmo_resultado(self):
        """Guarda contra drift entre os dois helpers públicos (ambos delegam a
        _compute_welcome_discount)."""
        preview_coupon, preview_discount = get_welcome_discount_preview(
            self.user, Decimal("200.00")
        )
        with transaction.atomic():
            locked_coupon, locked_discount = get_welcome_discount(
                self.user, Decimal("200.00")
            )

        self.assertEqual(preview_coupon, locked_coupon)
        self.assertEqual(preview_discount, locked_discount)
