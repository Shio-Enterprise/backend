import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Event
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import (
    IntegrityError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.db.models import Sum
from django.test import (
    RequestFactory,
    TransactionTestCase,
    override_settings,
    skipUnlessDBFeature,
)
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient, APITestCase, APITransactionTestCase
from rest_framework_simplejwt.tokens import RefreshToken

from authentication.models import Address, UserProfile, UserRole
from orders.models import (
    Cart,
    CartItem,
    CheckoutAttempt,
    CheckoutAttemptStatus,
    Coupon,
    CustomerOrder,
    OrderItem,
    OrderStatus,
    OrderStatusLog,
    Payment,
    PaymentStatus,
    ShippingQuote,
)
from orders.services import (
    CheckoutShippingUnavailable,
    create_infinitepay_checkout,
    create_shipping_quote,
    get_welcome_discount,
    get_welcome_discount_preview,
    prepare_checkout_attempt,
    update_status,
    validate_shipping_quote,
)
from products.models import Category, DropCampaign, Product, ProductVariation

User = get_user_model()


def make_checkout_payload(client, address, *, idempotency_key=None):
    """Cria uma cotação real com o transporte externo isolado pelo teste."""
    shipping_settings = override_settings(
        CORREIOS_REMETENTE_CEP="70000000",
        CORREIOS_CODIGO_SERVICO="03220",
        CORREIOS_PESO_PADRAO_GRAMAS="300",
    )
    with (
        shipping_settings,
        patch(
            "orders.services.fetch_shipping_price_by_service_and_ceps",
            return_value={"pcFinal": "15,00"},
        ),
        patch(
            "orders.services.fetch_shipping_deadline_by_service_and_ceps",
            return_value={"prazoEntrega": 3},
        ),
    ):
        response = client.post(
            "/api/orders/checkout/calculate/",
            {"address_id": str(address.pk)},
            format="json",
        )
    if response.status_code != status.HTTP_200_OK:
        raise AssertionError(
            f"Falha ao criar cotação para o teste: {response.status_code} {response.data}"
        )
    return {
        "address_id": str(address.pk),
        "shipping_quote_id": response.data["shipping_quote_id"],
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
    }


class CheckoutAPITests(APITestCase):
    def setUp(self):
        Coupon.objects.filter(code="BEMVINDO10").update(is_active=False)
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
        shipping_patch = patch(
            "orders.services.fetch_shipping_price_by_service_and_ceps"
        )
        self.mock_shipping = shipping_patch.start()
        self.addCleanup(shipping_patch.stop)
        self.mock_shipping.return_value = {"pcFinal": "15,00"}
        deadline_patch = patch(
            "orders.services.fetch_shipping_deadline_by_service_and_ceps"
        )
        self.mock_deadline = deadline_patch.start()
        self.addCleanup(deadline_patch.stop)
        self.mock_deadline.return_value = {"prazoEntrega": 3}
        shipping_settings = override_settings(
            CORREIOS_REMETENTE_CEP="70000000",
            CORREIOS_CODIGO_SERVICO="03220",
            CORREIOS_PESO_PADRAO_GRAMAS="300",
        )
        shipping_settings.enable()
        self.addCleanup(shipping_settings.disable)

    def checkout_payload(self):
        response = self.client.post(
            "/api/orders/checkout/calculate/",
            {"address_id": str(self.address.pk)},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        return {
            "address_id": str(self.address.pk),
            "shipping_quote_id": response.data["shipping_quote_id"],
            "idempotency_key": str(uuid.uuid4()),
        }

    @patch("orders.services.create_infinitepay_checkout")
    def test_checkout_sucesso_gera_pedido_e_reduz_estoque(self, mock_create_checkout):
        """Deve retornar 201, criar o pedido, finalizar o carrinho e deduzir estoque."""
        mock_create_checkout.return_value = "https://pay.infinitepay.io/mock-url"

        payload = self.checkout_payload()

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
        self.assertEqual(order.total_amount, Decimal("215.00"))
        self.assertEqual(order.discount_amount, Decimal("0.00"))

    @patch("orders.services.requests.post")
    def test_promocao_e_boas_vindas_coincidem_na_cotacao_pedido_e_gateway(
        self, gateway
    ):
        Coupon.objects.filter(code="BEMVINDO10").update(is_active=True)
        now = timezone.now()
        Product.objects.filter(pk=self.product.pk).update(
            promotional_price=Decimal("80.00"),
            promo_start=now - timedelta(hours=1),
            promo_end=now + timedelta(hours=1),
        )
        gateway.return_value.json.return_value = {
            "url": "https://pay.infinitepay.io/test"
        }
        payload = self.checkout_payload()
        quote = ShippingQuote.objects.get(pk=payload["shipping_quote_id"])
        self.assertEqual(quote.subtotal, Decimal("160.00"))
        self.assertEqual(quote.discount_amount, Decimal("16.00"))
        self.assertEqual(quote.total_amount, Decimal("159.00"))
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, 201)
        order = CustomerOrder.objects.get(user=self.user)
        self.assertEqual(order.coupon.code, "BEMVINDO10")
        self.assertEqual(order.total_amount, quote.total_amount)
        self.assertEqual(order.discount_amount, quote.discount_amount)
        items = gateway.call_args.kwargs["json"]["items"]
        self.assertEqual(sum(item["price"] * item["quantity"] for item in items), 15900)
        self.client.post(self.url, payload, format="json")
        gateway.assert_called_once()
        self.assertEqual(
            self.variation.stock_movements.filter(reason="VENDA").count(), 1
        )

    @patch("orders.services.create_infinitepay_checkout")
    def test_primeira_compra_aplica_desconto_de_boas_vindas(self, mock_create_checkout):
        Coupon.objects.filter(code="BEMVINDO10").update(is_active=True)
        mock_create_checkout.return_value = "https://pay.infinitepay.io/mock-url"

        payload = self.checkout_payload()
        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = CustomerOrder.objects.get(user=self.user)
        self.assertEqual(order.discount_amount, 20.00)  # 10% de 200.00
        self.assertEqual(order.total_amount, 195.00)  # 200 - 20 + 15
        self.assertEqual(order.coupon.code, "BEMVINDO10")

    @patch("orders.services.create_infinitepay_checkout")
    def test_segunda_compra_nao_aplica_desconto(self, mock_create_checkout):
        Coupon.objects.filter(code="BEMVINDO10").update(is_active=True)
        mock_create_checkout.return_value = "https://pay.infinitepay.io/mock-url"

        CustomerOrder.objects.create(
            user=self.user,
            subtotal=50.00,
            total_amount=50.00,
            status=OrderStatus.PAID,
        )

        payload = self.checkout_payload()
        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        new_order = (
            CustomerOrder.objects.filter(user=self.user).exclude(subtotal=50.00).first()
        )
        self.assertEqual(new_order.discount_amount, 0)
        self.assertIsNone(new_order.coupon)

    def test_checkout_com_carrinho_vazio_retorna_400(self):
        """Deve retornar 400 se o usuário não tiver itens no carrinho ativo."""
        payload = self.checkout_payload()
        self.cart.items.all().delete()

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(CustomerOrder.objects.count(), 0)

    def test_checkout_sem_estoque_faz_rollback_e_retorna_400(self):
        """Deve barrar a compra e garantir que o carrinho continua ativo e o estoque intacto."""
        payload = self.checkout_payload()
        self.cart_item.quantity = 20
        self.cart_item.save()

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.cart.refresh_from_db()
        self.assertEqual(self.cart.status, "ACTIVE")

        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 10)

    @patch("orders.services.create_infinitepay_checkout")
    def test_checkout_falha_gateway_preserva_tentativa_para_conferencia(
        self, mock_create_checkout
    ):
        """Timeout pode ocorrer após criar o link; não apagar nem reenviar a tentativa."""
        mock_create_checkout.side_effect = Exception("InfinitePay Timeout")

        payload = self.checkout_payload()

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(CustomerOrder.objects.count(), 1)
        self.assertEqual(
            CheckoutAttempt.objects.get().status, CheckoutAttemptStatus.UNCERTAIN
        )
        replay = self.client.post(self.url, payload, format="json")
        self.assertEqual(replay.data, response.data)
        mock_create_checkout.assert_called_once()

        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 8)

    def test_rejeita_valores_e_parametros_logisticos_do_cliente(self):
        for field in (
            "shipping_cost",
            "subtotal",
            "discount_amount",
            "total_amount",
            "cep_origem",
            "peso",
            "codigo_servico",
        ):
            for url in (self.url, "/api/orders/checkout/calculate/"):
                with self.subTest(field=field, url=url):
                    response = self.client.post(
                        url,
                        {"address_id": str(self.address.id), field: -100},
                        format="json",
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertIn(field, response.data)
        self.assertEqual(CustomerOrder.objects.count(), 0)
        self.mock_shipping.assert_not_called()

    def test_calculo_reconsulta_preco_e_nao_altera_carrinho_ou_estoque(self):
        self.product.base_price = Decimal("19.99")
        self.product.save()
        self.mock_shipping.return_value = {"pcFinal": "19,92"}
        response = self.client.post(
            "/api/orders/checkout/calculate/",
            {"address_id": str(self.address.id)},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["subtotal"], "39.98")
        self.assertEqual(response.data["shipping_cost"], "19.92")
        self.assertEqual(response.data["prazo_dias"], 3)
        self.assertEqual(response.data["discount_amount"], "0.00")
        self.assertEqual(response.data["total_amount"], "59.90")
        self.assertEqual(response.data["items"][0]["unit_price"], "19.99")
        self.assertEqual(CustomerOrder.objects.count(), 0)
        self.cart.refresh_from_db()
        self.cart_item.refresh_from_db()
        self.variation.refresh_from_db()
        self.assertEqual(self.cart.status, "ACTIVE")
        self.assertEqual(self.cart_item.unit_price, Decimal("100.00"))
        self.assertEqual(self.variation.stock_quantity, 10)
        self.mock_shipping.assert_called_once_with(
            "03220", "70000000", "71000000", "300"
        )

    @patch("orders.services.requests.post")
    def test_calculo_pedido_e_gateway_usam_mesmos_valores(self, mock_post):
        self.product.base_price = Decimal("19.99")
        self.product.save()
        self.mock_shipping.return_value = {"pcFinal": "19,92"}
        mock_post.return_value.json.return_value = {
            "url": "https://pay.infinitepay.io/mock-url"
        }
        preview = self.client.post(
            "/api/orders/checkout/calculate/",
            {"address_id": str(self.address.id)},
            format="json",
        )
        response = self.client.post(
            self.url,
            {
                "address_id": str(self.address.id),
                "shipping_quote_id": preview.data["shipping_quote_id"],
                "idempotency_key": str(uuid.uuid4()),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        order = CustomerOrder.objects.get()
        self.assertEqual(order.total_amount, Decimal(preview.data["total_amount"]))
        self.assertEqual(order.payment.total_amount, Decimal("59.90"))
        self.assertEqual(order.items.get().unit_price, Decimal("19.99"))
        sent = mock_post.call_args.kwargs["json"]["items"]
        self.assertEqual(sent[0]["price"], 1999)
        self.assertEqual(sent[1]["price"], 1992)
        self.assertEqual(sum(i["quantity"] * i["price"] for i in sent), 5990)

    @patch("orders.services.create_infinitepay_checkout")
    def test_frete_invalido_ou_indisponivel_nao_cria_pedido(self, mock_gateway):
        for price in (
            {},
            {"pcFinal": None},
            {"pcFinal": "NaN"},
            {"pcFinal": "Infinity"},
            {"pcFinal": "-1"},
            {"pcFinal": "abc"},
        ):
            with self.subTest(price=price):
                self.mock_shipping.return_value = price
                response = self.client.post(
                    "/api/orders/checkout/calculate/",
                    {"address_id": str(self.address.id)},
                    format="json",
                )
                self.assertEqual(response.status_code, 503)
        self.mock_shipping.side_effect = TimeoutError()
        response = self.client.post(
            "/api/orders/checkout/calculate/",
            {"address_id": str(self.address.id)},
            format="json",
        )
        self.assertEqual(response.status_code, 503)
        mock_gateway.assert_not_called()
        self.assertEqual(CustomerOrder.objects.count(), 0)
        self.cart.refresh_from_db()
        self.variation.refresh_from_db()
        self.assertEqual(self.cart.status, "ACTIVE")
        self.assertEqual(self.variation.stock_quantity, 10)

    def test_rejeita_endereco_invalido_ou_de_outro_usuario(self):
        payload = self.checkout_payload()
        self.mock_shipping.reset_mock()
        other = User.objects.create_user(email="outro@checkout.test", name="Outro")
        self.address.user = other
        self.address.save()
        for address_id in (str(self.address.id), "nao-e-uuid"):
            for url in (self.url, "/api/orders/checkout/calculate/"):
                with self.subTest(address=address_id, url=url):
                    response = self.client.post(
                        url,
                        {
                            **(payload if url == self.url else {}),
                            "address_id": address_id,
                        },
                        format="json",
                    )
                    self.assertEqual(response.status_code, 400)
        self.mock_shipping.assert_not_called()

    def test_calculo_exige_autenticacao(self):
        self.client.force_authenticate(user=None)
        for url in (self.url, "/api/orders/checkout/calculate/"):
            response = self.client.post(
                url, {"address_id": str(self.address.id)}, format="json"
            )
            self.assertEqual(response.status_code, 401)

    @patch("orders.services.create_infinitepay_checkout")
    def test_checkout_exige_cotacao_sem_criar_pedido(self, mock_gateway):
        response = self.client.post(
            self.url, {"address_id": str(self.address.pk)}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("shipping_quote_id", response.data)
        self.assertFalse(CustomerOrder.objects.exists())
        self.assertFalse(Payment.objects.exists())
        mock_gateway.assert_not_called()

    @patch("orders.services.create_infinitepay_checkout")
    def test_checkout_rejeita_cotacao_expirada_ou_invalidada(self, mock_gateway):
        for field in ("expires_at", "invalidated_at"):
            with self.subTest(field=field):
                payload = self.checkout_payload()
                ShippingQuote.objects.filter(pk=payload["shipping_quote_id"]).update(
                    **{field: timezone.now() - timedelta(seconds=1)}
                )
                response = self.client.post(self.url, payload, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertIn("shipping_quote_id", response.data)
        self.assertFalse(CustomerOrder.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 10)
        mock_gateway.assert_not_called()

    @patch("orders.services.create_infinitepay_checkout")
    def test_checkout_rejeita_cotacao_de_outro_usuario(self, mock_gateway):
        payload = self.checkout_payload()
        other = User.objects.create_user(email="intruso@checkout.test", name="Outro")
        self.client.force_authenticate(user=other)
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("shipping_quote_id", response.data)
        self.assertFalse(CustomerOrder.objects.exists())
        mock_gateway.assert_not_called()

    @patch("orders.services.create_infinitepay_checkout")
    def test_checkout_rejeita_cotacao_de_outro_endereco_ou_carrinho(self, mock_gateway):
        payload = self.checkout_payload()
        address = Address.objects.create(
            user=self.user,
            zip_code=self.address.zip_code,
            street="Outra rua",
            address_number="456",
            neighborhood="Centro",
            city="Brasília",
            state="DF",
        )
        response = self.client.post(
            self.url, {**payload, "address_id": str(address.pk)}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.cart.status = "ABANDONED"
        self.cart.save()
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(
            cart=cart, variation=self.variation, quantity=2, unit_price=100
        )
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("shipping_quote_id", response.data)
        self.assertFalse(CustomerOrder.objects.exists())
        mock_gateway.assert_not_called()

    @patch("orders.services.create_infinitepay_checkout")
    def test_checkout_invalida_compra_alterada_antes_de_chamar_gateway(
        self, mock_gateway
    ):
        for model, object_id, changes in (
            (Product, self.product.pk, {"base_price": Decimal("110.00")}),
            (CartItem, self.cart_item.pk, {"quantity": 3}),
            (Address, self.address.pk, {"street": "Rua alterada"}),
        ):
            with self.subTest(model=model.__name__):
                payload = self.checkout_payload()
                quote = ShippingQuote.objects.get(pk=payload["shipping_quote_id"])
                model.objects.filter(pk=object_id).update(**changes)
                response = self.client.post(self.url, payload, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertIn("shipping_quote_id", response.data)
                quote.refresh_from_db()
                self.assertIsNotNone(quote.invalidated_at)
        self.assertFalse(CustomerOrder.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.status, "ACTIVE")
        mock_gateway.assert_not_called()

    @patch("orders.services.requests.post")
    def test_finalizacao_usa_frete_confirmado_sem_nova_consulta(self, mock_post):
        payload = self.checkout_payload()
        self.mock_shipping.reset_mock()
        self.mock_deadline.reset_mock()
        self.mock_shipping.side_effect = TimeoutError()
        mock_post.return_value.json.return_value = {
            "url": "https://pay.infinitepay.io/mock"
        }
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, 201)
        order = CustomerOrder.objects.get()
        self.assertEqual(order.subtotal, Decimal("200.00"))
        self.assertEqual(order.shipping_cost, Decimal("15.00"))
        self.assertEqual(order.total_amount, Decimal("215.00"))
        self.assertEqual(order.payment.total_amount, order.total_amount)
        items = mock_post.call_args.kwargs["json"]["items"]
        self.assertEqual(sum(item["quantity"] * item["price"] for item in items), 21500)
        self.mock_shipping.assert_not_called()
        self.mock_deadline.assert_not_called()

    @patch("orders.services.create_infinitepay_checkout")
    def test_nova_cotacao_permite_finalizar_apos_mudanca_de_preco(self, mock_gateway):
        old_payload = self.checkout_payload()
        Product.objects.filter(pk=self.product.pk).update(base_price=Decimal("110.00"))
        self.assertEqual(
            self.client.post(self.url, old_payload, format="json").status_code, 400
        )
        payload = self.checkout_payload()
        self.assertNotEqual(
            payload["shipping_quote_id"], old_payload["shipping_quote_id"]
        )
        mock_gateway.return_value = "https://pay.infinitepay.io/mock"
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(CustomerOrder.objects.get().total_amount, Decimal("235.00"))
        mock_gateway.assert_called_once()

    @patch("orders.services.requests.post")
    def test_reenvio_retorna_resultado_original_mesmo_apos_expirar_cotacao(
        self, mock_post
    ):
        mock_post.return_value.json.return_value = {
            "url": "https://pay.infinitepay.io/mock"
        }
        payload = self.checkout_payload()
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, 201)
        ShippingQuote.objects.filter(pk=payload["shipping_quote_id"]).update(
            expires_at=timezone.now() - timedelta(days=1)
        )
        for _ in range(3):
            replay = self.client.post(self.url, payload, format="json")
            self.assertEqual(replay.status_code, 201)
            self.assertEqual(replay.data, response.data)
        self.assertEqual(CustomerOrder.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(CheckoutAttempt.objects.count(), 1)
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 8)
        mock_post.assert_called_once()

    @patch(
        "orders.services.create_infinitepay_checkout",
        return_value="https://pay.infinitepay.io/mock",
    )
    def test_rejeita_mesma_chave_para_payload_diferente(self, mock_gateway):
        payload = self.checkout_payload()
        other_payload = self.checkout_payload()
        self.assertEqual(
            self.client.post(self.url, payload, format="json").status_code, 201
        )
        for field, value in (
            ("address_id", str(uuid.uuid4())),
            ("shipping_quote_id", other_payload["shipping_quote_id"]),
        ):
            with self.subTest(field=field):
                response = self.client.post(
                    self.url, {**payload, field: value}, format="json"
                )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.data["code"], "idempotency_conflict")
        mock_gateway.assert_called_once()

    @patch(
        "orders.services.create_infinitepay_checkout",
        return_value="https://pay.infinitepay.io/mock",
    )
    def test_chave_diferente_nao_duplica_mesmo_carrinho(self, mock_gateway):
        payload = self.checkout_payload()
        second = self.checkout_payload()
        self.assertEqual(
            self.client.post(self.url, payload, format="json").status_code, 201
        )
        response = self.client.post(self.url, second, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "checkout_already_started")
        self.assertEqual(response.data["attempt"], payload)
        self.assertEqual(CustomerOrder.objects.count(), 1)
        mock_gateway.assert_called_once()

    @patch("orders.services.create_infinitepay_checkout")
    def test_preparacao_falha_faz_rollback_e_permite_mesma_tentativa(
        self, mock_gateway
    ):
        payload = self.checkout_payload()
        with patch(
            "orders.services.OrderItem.objects.create",
            side_effect=RuntimeError("Falha local"),
        ):
            response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, 500)
        self.assertFalse(CustomerOrder.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(CheckoutAttempt.objects.exists())
        self.cart.refresh_from_db()
        self.variation.refresh_from_db()
        self.assertEqual(self.cart.status, "ACTIVE")
        self.assertEqual(self.variation.stock_quantity, 10)
        mock_gateway.assert_not_called()
        mock_gateway.return_value = "https://pay.infinitepay.io/mock"
        self.assertEqual(
            self.client.post(self.url, payload, format="json").status_code, 201
        )
        self.assertEqual(
            CheckoutAttempt.objects.get().idempotency_key,
            uuid.UUID(payload["idempotency_key"]),
        )

    @patch("orders.services.requests.post")
    def test_tentativa_interrompida_nao_dispara_nova_chamada_externa(self, mock_post):
        payload = self.checkout_payload()
        attempt, result = prepare_checkout_attempt(
            self.user, **{field: uuid.UUID(value) for field, value in payload.items()}
        )
        self.assertIsNone(result)
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response["Retry-After"], "3")
        self.assertEqual(response.data["status"], "PROCESSING")
        CheckoutAttempt.objects.filter(pk=attempt.pk).update(
            created_at=timezone.now() - timedelta(minutes=5)
        )
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["status"], "UNCERTAIN")
        self.assertEqual(CustomerOrder.objects.count(), 1)
        mock_post.assert_not_called()

    @patch("orders.services.requests.post")
    def test_resposta_sem_url_fica_incerta_sem_reenvio(self, mock_post):
        payload = self.checkout_payload()
        mock_post.return_value.json.return_value = {}
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["status"], "UNCERTAIN")
        self.assertEqual(
            self.client.post(self.url, payload, format="json").data, response.data
        )
        self.assertEqual(Payment.objects.get().status, PaymentStatus.PENDING)
        mock_post.assert_called_once()

    def test_exige_chave_uuid(self):
        payload = self.checkout_payload()
        for key in (None, "", "invalid"):
            with self.subTest(key=key):
                response = self.client.post(
                    self.url, {**payload, "idempotency_key": key}, format="json"
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("idempotency_key", response.data)
        self.assertFalse(CustomerOrder.objects.exists())

    @patch(
        "orders.services.create_infinitepay_checkout",
        return_value="https://pay.infinitepay.io/mock",
    )
    def test_chave_e_isolada_por_usuario(self, mock_gateway):
        payload = self.checkout_payload()
        original = self.client.post(self.url, payload, format="json")
        other = User.objects.create_user(email="other-attempt@shio.test", name="Outro")
        self.client.force_authenticate(user=other)
        forbidden = self.client.post(self.url, payload, format="json")
        self.assertEqual(forbidden.status_code, 400)
        self.assertNotIn("checkout_url", forbidden.data)
        address = Address.objects.create(
            user=other,
            zip_code="71000000",
            street="Rua",
            address_number="2",
            neighborhood="Centro",
            city="Brasília",
            state="DF",
        )
        cart = Cart.objects.create(user=other)
        CartItem.objects.create(
            cart=cart, variation=self.variation, quantity=1, unit_price=100
        )
        quote = create_shipping_quote(other, address.pk)
        response = self.client.post(
            self.url,
            {
                "address_id": str(address.pk),
                "shipping_quote_id": str(quote.pk),
                "idempotency_key": payload["idempotency_key"],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertNotEqual(response.data["order_id"], original.data["order_id"])
        self.assertEqual(CheckoutAttempt.objects.count(), 2)
        self.assertEqual(mock_gateway.call_count, 2)

    @patch("orders.services.requests.post")
    def test_falha_ao_persistir_resposta_nao_reenvia_ao_gateway(self, mock_post):
        from django.db.models.query import QuerySet

        payload = self.checkout_payload()
        mock_post.return_value.json.return_value = {
            "url": "https://pay.infinitepay.io/mock"
        }
        original_update = QuerySet.update

        def fail_success(queryset, **kwargs):
            if (
                queryset.model is CheckoutAttempt
                and kwargs.get("status") == CheckoutAttemptStatus.SUCCEEDED
            ):
                raise RuntimeError("Falha ao persistir resposta")
            return original_update(queryset, **kwargs)

        with patch.object(QuerySet, "update", autospec=True, side_effect=fail_success):
            with self.assertRaises(RuntimeError):
                self.client.post(self.url, payload, format="json")
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(CustomerOrder.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        mock_post.assert_called_once()

    @patch("orders.services.requests.post")
    def test_resposta_do_link_nao_regride_pagamento_confirmado(self, mock_post):
        payload = self.checkout_payload()

        def confirm_payment(*args, **kwargs):
            Payment.objects.update(status=PaymentStatus.PAID)
            CustomerOrder.objects.update(status=OrderStatus.PAID)
            response = mock_post.return_value
            response.json.return_value = {"url": "https://pay.infinitepay.io/mock"}
            return response

        mock_post.side_effect = confirm_payment
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Payment.objects.get().status, PaymentStatus.PAID)
        self.assertEqual(CustomerOrder.objects.get().status, OrderStatus.PAID)
        self.assertFalse(mock_post.call_args.kwargs["allow_redirects"])

    def test_frete_zero_explicito_e_arredondamento(self):
        for raw, expected in (
            ("0.00", "200.00"),
            ("0.005", "200.01"),
            ("19,925", "219.93"),
        ):
            with self.subTest(raw=raw):
                self.mock_shipping.return_value = {"pcFinal": raw}
                response = self.client.post(
                    "/api/orders/checkout/calculate/",
                    {"address_id": str(self.address.id)},
                    format="json",
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["total_amount"], expected)


@skipUnlessDBFeature("has_select_for_update")
@override_settings(CORREIOS_REMETENTE_CEP="70000000")
class CheckoutConcurrencyTests(APITransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="concurrent@shio.test", name="Cliente"
        )
        self.address = Address.objects.create(
            user=self.user,
            zip_code="71000000",
            street="Rua",
            address_number="1",
            neighborhood="Centro",
            city="Brasília",
            state="DF",
        )
        product = Product.objects.create(name="Camiseta", base_price=Decimal("100.00"))
        self.variation = ProductVariation.objects.create(
            product=product, size="M", sku="CONCURRENT", stock_quantity=10
        )
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(
            cart=cart,
            variation=self.variation,
            quantity=2,
            unit_price=Decimal("100.00"),
        )
        with (
            patch(
                "orders.services.fetch_shipping_price_by_service_and_ceps",
                return_value={"pcFinal": "15.00"},
            ),
            patch(
                "orders.services.fetch_shipping_deadline_by_service_and_ceps",
                return_value={"prazoEntrega": 3},
            ),
        ):
            quote = create_shipping_quote(self.user, self.address.pk)
        self.payload = {
            "address_id": str(self.address.pk),
            "shipping_quote_id": str(quote.pk),
            "idempotency_key": str(uuid.uuid4()),
        }

    def assert_parallel_checkout(self, different_key):
        entered = Event()
        release = Event()

        def gateway(*args):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("Teste não liberou o gateway")
            return "https://pay.infinitepay.io/mock"

        def checkout(payload):
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(user=self.user)
                return client.post("/api/orders/checkout/", payload, format="json")
            finally:
                connections.close_all()

        with (
            patch(
                "orders.services.create_infinitepay_checkout", side_effect=gateway
            ) as mock_gateway,
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(checkout, self.payload)
            try:
                self.assertTrue(entered.wait(5))
                # Outra conexão enxerga o registro enquanto o gateway ainda não respondeu.
                self.assertEqual(CheckoutAttempt.objects.count(), 1)
                payload = (
                    {**self.payload, "idempotency_key": str(uuid.uuid4())}
                    if different_key
                    else self.payload
                )
                second = pool.submit(checkout, payload).result(timeout=5)
                self.assertEqual(second.status_code, 409 if different_key else 202)
            finally:
                release.set()
            response = first.result(timeout=5)
            self.assertEqual(response.status_code, 201)
            mock_gateway.assert_called_once()
        self.assertEqual(CustomerOrder.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 8)
        replay = checkout(self.payload)
        self.assertEqual(replay.data, response.data)

    def test_requisicoes_simultaneas_com_mesma_chave(self):
        self.assert_parallel_checkout(different_key=False)

    def test_requisicoes_simultaneas_com_chaves_diferentes(self):
        self.assert_parallel_checkout(different_key=True)


@override_settings(
    CORREIOS_MOCK_ENABLED=False,
    CORREIOS_REMETENTE_CEP="70000000",
    CORREIOS_CODIGO_SERVICO="03220",
    CORREIOS_PESO_PADRAO_GRAMAS="300",
    SHIPPING_QUOTE_TTL_SECONDS=900,
)
class ShippingQuoteTests(APITestCase):
    def setUp(self):
        Coupon.objects.filter(code="BEMVINDO10").update(is_active=False)
        self.user = User.objects.create_user(email="cotacao@shio.com", name="Cliente")
        self.other_user = User.objects.create_user(email="outro@shio.com", name="Outro")
        self.address = Address.objects.create(
            user=self.user,
            zip_code="71000000",
            street="Rua Teste",
            address_number="123",
            neighborhood="Centro",
            city="Brasília",
            state="DF",
        )
        self.product = Product.objects.create(
            name="Camiseta", base_price=Decimal("19.99")
        )
        self.variation = ProductVariation.objects.create(
            product=self.product, size="M", sku="COTACAO-M", stock_quantity=10
        )
        self.cart = Cart.objects.create(user=self.user)
        self.cart_item = CartItem.objects.create(
            cart=self.cart,
            variation=self.variation,
            quantity=2,
            unit_price=Decimal("10.00"),
        )
        for target, value, attr in (
            (
                "fetch_shipping_price_by_service_and_ceps",
                {"pcFinal": "15,005"},
                "mock_price",
            ),
            (
                "fetch_shipping_deadline_by_service_and_ceps",
                {"prazoEntrega": 3},
                "mock_deadline",
            ),
        ):
            patcher = patch(f"orders.services.{target}", return_value=value)
            setattr(self, attr, patcher.start())
            self.addCleanup(patcher.stop)

    def assert_quote_rejected(self, quote, code, **kwargs):
        arguments = {
            "user": self.user,
            "quote_id": quote.pk,
            "cart_id": self.cart.pk,
            "address_id": self.address.pk,
            **kwargs,
        }
        with self.assertRaises(ValidationError) as error:
            validate_shipping_quote(**arguments)
        self.assertEqual(error.exception.get_codes()["shipping_quote_id"], code)

    def test_persiste_valores_do_servidor_sem_alterar_compra(self):
        now = timezone.now()
        with patch("orders.services.timezone.now", return_value=now):
            quote = create_shipping_quote(self.user, self.address.pk)
        quote.refresh_from_db()
        self.assertEqual(quote.user_id, self.user.pk)
        self.assertEqual(quote.cart_id, self.cart.pk)
        self.assertEqual(quote.address_id, self.address.pk)
        self.assertEqual(quote.subtotal, Decimal("39.98"))
        self.assertEqual(quote.shipping_cost, Decimal("15.01"))
        self.assertEqual(quote.discount_amount, Decimal("0.00"))
        self.assertEqual(quote.total_amount, Decimal("54.99"))
        self.assertEqual(quote.prazo_dias, 3)
        self.assertEqual(quote.expires_at, now + timedelta(minutes=15))
        self.assertIsNone(quote.invalidated_at)
        self.cart.refresh_from_db()
        self.cart_item.refresh_from_db()
        self.variation.refresh_from_db()
        self.assertEqual(self.cart.status, "ACTIVE")
        self.assertEqual(self.cart_item.unit_price, Decimal("10.00"))
        self.assertEqual(self.cart_item.quantity, 2)
        self.assertEqual(self.variation.stock_quantity, 10)
        self.assertFalse(CustomerOrder.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.mock_price.assert_called_once_with("03220", "70000000", "71000000", "300")

    def test_validacao_reutiliza_valores_sem_chamar_correios(self):
        quote = create_shipping_quote(self.user, self.address.pk)
        self.mock_price.reset_mock()
        self.mock_deadline.reset_mock()
        validated = validate_shipping_quote(
            self.user, str(quote.pk), str(self.cart.pk), str(self.address.pk)
        )
        self.assertEqual(validated.pk, quote.pk)
        self.assertEqual(validated.total_amount, quote.total_amount)
        self.assertEqual(ShippingQuote.objects.count(), 1)
        self.mock_price.assert_not_called()
        self.mock_deadline.assert_not_called()

    @override_settings(SHIPPING_QUOTE_TTL_SECONDS=60)
    def test_validade_configuravel_e_limite_exato(self):
        now = timezone.now()
        with patch("orders.services.timezone.now", return_value=now):
            quote = create_shipping_quote(self.user, self.address.pk)
        self.assertEqual(quote.expires_at, now + timedelta(seconds=60))
        with patch(
            "orders.services.timezone.now",
            return_value=quote.expires_at - timedelta(microseconds=1),
        ):
            validate_shipping_quote(self.user, quote.pk, self.cart.pk, self.address.pk)
        for elapsed in (0, 1):
            with (
                self.subTest(elapsed=elapsed),
                patch(
                    "orders.services.timezone.now",
                    return_value=quote.expires_at + timedelta(seconds=elapsed),
                ),
            ):
                self.assert_quote_rejected(quote, "quote_expired")

    def test_rejeita_configuracao_de_validade_invalida(self):
        for ttl in (0, -1, True, "900", 1.5):
            with (
                self.subTest(ttl=ttl),
                override_settings(SHIPPING_QUOTE_TTL_SECONDS=ttl),
            ):
                with self.assertRaises(ImproperlyConfigured):
                    create_shipping_quote(self.user, self.address.pk)
        self.mock_price.assert_not_called()
        self.assertFalse(ShippingQuote.objects.exists())

    def test_rejeita_expiracao_durante_validacao(self):
        quote = create_shipping_quote(self.user, self.address.pk)
        with patch(
            "orders.services.timezone.now",
            side_effect=[quote.expires_at - timedelta(seconds=1)] * 3
            + [quote.expires_at],
        ):
            self.assert_quote_rejected(quote, "quote_expired")

    def test_rejeita_outro_usuario_e_id_invalido_sem_expor_cotacao(self):
        quote = create_shipping_quote(self.user, self.address.pk)
        self.assert_quote_rejected(quote, "quote_invalid", user=self.other_user)
        for quote_id in (uuid.uuid4(), "invalido", None):
            with self.subTest(quote_id=quote_id):
                self.assert_quote_rejected(quote, "quote_invalid", quote_id=quote_id)
        quote.refresh_from_db()
        self.assertIsNone(quote.invalidated_at)

    def test_rejeita_cotacao_para_outro_carrinho_ou_endereco(self):
        quote = create_shipping_quote(self.user, self.address.pk)
        cart = Cart.objects.create(user=self.user)
        address = Address.objects.create(
            user=self.user,
            zip_code=self.address.zip_code,
            street="Outra rua",
            address_number="456",
            neighborhood="Centro",
            city="Brasília",
            state="DF",
        )
        self.assert_quote_rejected(quote, "quote_mismatch", cart_id=cart.pk)
        self.assert_quote_rejected(quote, "quote_mismatch", address_id=address.pk)

    def test_cupom_alterado_expirado_ou_desativado_invalida_cotacao(self):
        for changes in (
            {"discount_value": Decimal("15.00")},
            {"is_active": False},
            {"expiration_date": timezone.now() - timedelta(seconds=1)},
        ):
            with self.subTest(changes=changes):
                Coupon.objects.filter(code="BEMVINDO10").update(
                    is_active=True,
                    discount_value=10,
                    expiration_date=None,
                )
                quote = create_shipping_quote(self.user, self.address.pk)
                Coupon.objects.filter(code="BEMVINDO10").update(**changes)
                self.assert_quote_rejected(quote, "quote_changed")

    def test_primeira_compra_consumida_invalida_desconto_cotado(self):
        Coupon.objects.filter(code="BEMVINDO10").update(is_active=True)
        quote = create_shipping_quote(self.user, self.address.pk)
        CustomerOrder.objects.create(user=self.user, subtotal=10, total_amount=10)
        self.assert_quote_rejected(quote, "quote_changed")

    def test_promocao_expira_sem_edicao_do_produto_e_invalida_cotacao(self):
        now = timezone.now()
        Product.objects.filter(pk=self.product.pk).update(
            promotional_price=Decimal("15.00"),
            promo_start=now - timedelta(hours=1),
            promo_end=now + timedelta(minutes=1),
        )
        quote = create_shipping_quote(self.user, self.address.pk)
        with patch(
            "orders.services.timezone.now", return_value=now + timedelta(minutes=2)
        ):
            self.assert_quote_rejected(quote, "quote_changed")

    def test_invalida_alteracao_de_quantidade_e_nao_reativa(self):
        quote = create_shipping_quote(self.user, self.address.pk)
        CartItem.objects.filter(pk=self.cart_item.pk).update(quantity=3)
        self.assert_quote_rejected(quote, "quote_changed")
        quote.refresh_from_db()
        self.assertIsNotNone(quote.invalidated_at)
        self.assertEqual(quote.total_amount, Decimal("54.99"))
        CartItem.objects.filter(pk=self.cart_item.pk).update(quantity=2)
        self.assert_quote_rejected(quote, "quote_invalidated")

    def test_detecta_edicao_revertida_antes_de_validar(self):
        quote = create_shipping_quote(self.user, self.address.pk)
        changed_at = quote.created_at + timedelta(seconds=1)
        CartItem.objects.filter(pk=self.cart_item.pk).update(
            quantity=3, updated_at=changed_at
        )
        CartItem.objects.filter(pk=self.cart_item.pk).update(
            quantity=2, updated_at=changed_at + timedelta(seconds=1)
        )
        self.assert_quote_rejected(quote, "quote_changed")

    def test_invalida_adicao_e_remocao_de_itens(self):
        quote = create_shipping_quote(self.user, self.address.pk)
        variation = ProductVariation.objects.create(
            product=self.product, size="G", sku="COTACAO-G", stock_quantity=5
        )
        item = CartItem.objects.create(
            cart=self.cart,
            variation=variation,
            quantity=1,
            unit_price=self.product.base_price,
        )
        self.assert_quote_rejected(quote, "quote_changed")
        quote = create_shipping_quote(self.user, self.address.pk)
        item.delete()
        self.assert_quote_rejected(quote, "quote_changed")

    def test_invalida_alteracao_de_preco(self):
        quote = create_shipping_quote(self.user, self.address.pk)
        Product.objects.filter(pk=self.product.pk).update(base_price=Decimal("20.00"))
        self.assert_quote_rejected(quote, "quote_changed")
        replacement = create_shipping_quote(self.user, self.address.pk)
        self.assertNotEqual(replacement.pk, quote.pk)
        self.assertEqual(replacement.subtotal, Decimal("40.00"))
        self.assertEqual(replacement.total_amount, Decimal("55.01"))

    def test_invalida_troca_de_variacao_mesmo_com_total_igual(self):
        quote = create_shipping_quote(self.user, self.address.pk)
        variation = ProductVariation.objects.create(
            product=self.product, size="G", sku="COTACAO-G", stock_quantity=10
        )
        CartItem.objects.filter(pk=self.cart_item.pk).update(variation=variation)
        self.assert_quote_rejected(quote, "quote_changed")
        replacement = create_shipping_quote(self.user, self.address.pk)
        self.assertEqual(replacement.total_amount, quote.total_amount)

    def test_invalida_edicao_dos_dados_de_entrega(self):
        for field, value in (
            ("zip_code", "72000000"),
            ("street", "Nova rua"),
            ("address_number", "999"),
            ("complement", "Apto 10"),
            ("neighborhood", "Outro bairro"),
            ("city", "Outra cidade"),
            ("state", "SP"),
        ):
            with self.subTest(field=field):
                quote = create_shipping_quote(self.user, self.address.pk)
                Address.objects.filter(pk=self.address.pk).update(**{field: value})
                self.assert_quote_rejected(quote, "quote_changed")

    def test_invalida_alteracao_de_parametros_do_frete(self):
        for setting, value in (
            ("CORREIOS_REMETENTE_CEP", "72000000"),
            ("CORREIOS_CODIGO_SERVICO", "03298"),
            ("CORREIOS_PESO_PADRAO_GRAMAS", "600"),
            ("CORREIOS_API_BASE_URL", "https://correios.example.test"),
            ("CORREIOS_MOCK_ENABLED", True),
        ):
            with self.subTest(setting=setting):
                quote = create_shipping_quote(self.user, self.address.pk)
                with override_settings(**{setting: value}):
                    self.assert_quote_rejected(quote, "quote_changed")

    def test_rejeita_carrinho_finalizado_vazio_ou_sem_estoque(self):
        quote = create_shipping_quote(self.user, self.address.pk)
        self.cart.status = "FINISHED"
        self.cart.save()
        self.assert_quote_rejected(quote, "quote_changed")
        self.cart.status = "ACTIVE"
        self.cart.save()
        quote = create_shipping_quote(self.user, self.address.pk)
        ProductVariation.objects.filter(pk=self.variation.pk).update(stock_quantity=1)
        self.assert_quote_rejected(quote, "quote_changed")
        ProductVariation.objects.filter(pk=self.variation.pk).update(stock_quantity=10)
        quote = create_shipping_quote(self.user, self.address.pk)
        self.cart_item.delete()
        self.assert_quote_rejected(quote, "quote_changed")

    def test_estoque_suficiente_pode_mudar_sem_reserva(self):
        quote = create_shipping_quote(self.user, self.address.pk)
        ProductVariation.objects.filter(pk=self.variation.pk).update(stock_quantity=8)
        validate_shipping_quote(self.user, quote.pk, self.cart.pk, self.address.pk)
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 8)

    def test_nao_persiste_cotacao_quando_frete_falha(self):
        for response in (
            {},
            {"pcFinal": None},
            {"pcFinal": "NaN"},
            {"pcFinal": "-1.00"},
        ):
            with self.subTest(response=response):
                self.mock_price.return_value = response
                with self.assertRaises(CheckoutShippingUnavailable):
                    create_shipping_quote(self.user, self.address.pk)
        self.mock_price.side_effect = TimeoutError()
        with self.assertRaises(CheckoutShippingUnavailable):
            create_shipping_quote(self.user, self.address.pk)
        self.assertFalse(ShippingQuote.objects.exists())
        self.assertFalse(CustomerOrder.objects.exists())
        self.assertFalse(Payment.objects.exists())

    def test_frete_zero_explicito_e_prazo_ausente_sao_preservados(self):
        self.mock_price.return_value = {"pcFinal": "0.00"}
        self.mock_deadline.return_value = {}
        quote = create_shipping_quote(self.user, self.address.pk)
        quote.refresh_from_db()
        self.assertEqual(quote.shipping_cost, Decimal("0.00"))
        self.assertEqual(quote.total_amount, Decimal("39.98"))
        self.assertIsNone(quote.prazo_dias)

    def test_falha_no_prazo_nao_persiste_cotacao_parcial(self):
        self.mock_deadline.side_effect = TimeoutError()
        with self.assertRaises(CheckoutShippingUnavailable):
            create_shipping_quote(self.user, self.address.pk)
        self.assertFalse(ShippingQuote.objects.exists())

    def test_rejeita_compra_alterada_durante_consulta_externa(self):
        def update_price(*args):
            Product.objects.filter(pk=self.product.pk).update(
                base_price=Decimal("25.00")
            )
            return {"pcFinal": "15.00"}

        self.mock_price.side_effect = update_price
        with self.assertRaises(ValidationError) as error:
            create_shipping_quote(self.user, self.address.pk)
        self.assertEqual(
            error.exception.get_codes()["shipping_quote_id"], "quote_changed"
        )
        self.assertFalse(ShippingQuote.objects.exists())

    def test_nao_cria_cotacao_com_endereco_de_outro_usuario(self):
        cart = Cart.objects.create(user=self.other_user)
        CartItem.objects.create(
            cart=cart, variation=self.variation, quantity=1, unit_price=Decimal("19.99")
        )
        with self.assertRaises(ValidationError):
            create_shipping_quote(self.other_user, self.address.pk)
        self.mock_price.assert_not_called()
        self.assertFalse(ShippingQuote.objects.exists())

    def test_banco_rejeita_valores_negativos(self):
        quote = create_shipping_quote(self.user, self.address.pk)
        for field in ("subtotal", "shipping_cost", "discount_amount", "total_amount"):
            with (
                self.subTest(field=field),
                self.assertRaises(IntegrityError),
                transaction.atomic(),
            ):
                ShippingQuote.objects.filter(pk=quote.pk).update(
                    **{field: Decimal("-0.01")}
                )

    def test_rota_de_calculo_retorna_cotacao_persistida(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            "/api/orders/checkout/calculate/",
            {"address_id": str(self.address.pk)},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["total_amount"], "54.99")
        quote = ShippingQuote.objects.get(pk=response.data["shipping_quote_id"])
        self.assertEqual(quote.total_amount, Decimal(response.data["total_amount"]))
        self.assertIn("expires_at", response.data)
        self.assertEqual(response.data["address"]["street"], self.address.street)


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
                f"Bearer {str(RefreshToken.for_user(self.admin).access_token)}"
            )
        }

        self.customer_auth = {
            "HTTP_AUTHORIZATION": (
                f"Bearer {str(RefreshToken.for_user(self.customer).access_token)}"
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
                    "BR123456789BR" if new_status == OrderStatus.SHIPPED else None
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
                    OrderStatusLog.objects.filter(order=order).count(),
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
            OrderStatusLog.objects.filter(order=order).count(),
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
            OrderStatusLog.objects.filter(order=order).count(),
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
            OrderStatusLog.objects.filter(order=order).count(),
            1,
        )

        update_status(
            order=order,
            new_status=OrderStatus.SHIPPED,
            tracking_code="BR123456789BR",
            changed_by=self.admin,
        )

        self.assertEqual(
            OrderStatusLog.objects.filter(order=order).count(),
            2,
        )

        update_status(
            order=order,
            new_status=OrderStatus.DELIVERED,
            changed_by=self.admin,
        )

        self.assertEqual(
            OrderStatusLog.objects.filter(order=order).count(),
            3,
        )

        logs = OrderStatusLog.objects.filter(order=order)

        self.assertEqual(
            logs.filter(new_status=OrderStatus.PREPARING).count(),
            1,
        )
        self.assertEqual(
            logs.filter(new_status=OrderStatus.SHIPPED).count(),
            1,
        )
        self.assertEqual(
            logs.filter(new_status=OrderStatus.DELIVERED).count(),
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
            {"status": "DELIVERED"},
            format="json",
            **self.admin_auth,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        order.refresh_from_db()

        self.assertEqual(order.status, OrderStatus.AWAITING_PAYMENT)
        self.assertEqual(
            OrderStatusLog.objects.filter(order=order).count(),
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


# ─── Disponibilidade de drops no carrinho e checkout ───────────────


def make_address(user, **kwargs):
    defaults = {
        "zip_code": "71000000",
        "street": "Rua Teste",
        "address_number": "1",
        "neighborhood": "Centro",
        "city": "Brasília",
        "state": "DF",
    }
    defaults.update(kwargs)
    return Address.objects.create(user=user, **defaults)


class CartSellabilityTests(APITestCase):
    """Itens de drops indisponíveis (ocultos ou esgotados) não podem ser
    adicionados/atualizados no carrinho."""

    cart_items_url = "/api/orders/cart/items/"

    def setUp(self):
        self.category = Category.objects.create(name="CartSellCat", slug="cart-sell-cat")

    def test_nao_pode_adicionar_produto_de_drop_privado_ao_carrinho(self):
        hidden_drop = DropCampaign.objects.create(
            name="DropOcultoCart", slug="drop-oculto-cart", is_public=False, is_active=True
        )
        product = Product.objects.create(
            category=self.category, drop=hidden_drop, name="ProdutoOculto",
            description="x", base_price=30,
        )
        variation = ProductVariation.objects.create(
            product=product, size="U", sku="OCULTO-1", stock_quantity=10
        )

        response = self.client.post(
            self.cart_items_url,
            {"variation_id": str(variation.id), "quantity": 1},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_nao_pode_adicionar_produto_de_drop_esgotado_ao_carrinho(self):
        sold_out_drop = DropCampaign.objects.create(
            name="DropEsgotadoCart",
            slug="drop-esgotado-cart",
            is_public=True,
            is_active=True,
            max_quantity=1,
        )
        product = Product.objects.create(
            category=self.category, drop=sold_out_drop, name="ProdutoEsgotado",
            description="x", base_price=20,
        )
        variation = ProductVariation.objects.create(
            product=product, size="U", sku="ESGOT-1", stock_quantity=10
        )

        buyer = User.objects.create_user(email="esgotbuyer@shio.com", name="EsgotBuyer")
        make_address(buyer)
        order = CustomerOrder.objects.create(
            user=buyer,
            subtotal=20,
            total_amount=20,
            status=OrderStatus.PAID,
            shipping_zip_code="71000000",
            shipping_street="R",
            shipping_number="1",
            shipping_neighborhood="B",
            shipping_city="C",
            shipping_state="DF",
        )
        OrderItem.objects.create(
            order=order, variation=variation, quantity=1, unit_price=20,
            product_name="ProdutoEsgotado - U",
        )

        response = self.client.post(
            self.cart_items_url,
            {"variation_id": str(variation.id), "quantity": 1},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_produto_sem_drop_pode_ser_adicionado_normalmente(self):
        product = Product.objects.create(
            category=self.category, name="ProdutoLivre", description="x", base_price=15,
        )
        variation = ProductVariation.objects.create(
            product=product, size="U", sku="LIVRE-1", stock_quantity=10
        )

        response = self.client.post(
            self.cart_items_url,
            {"variation_id": str(variation.id), "quantity": 1},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # O frontend usa este campo para decidir se bloqueia/remove o item do
        # carrinho, em vez de recalcular a política de disponibilidade em JS.
        self.assertTrue(response.json()["items"][0]["is_sellable"])

    def test_cliente_autenticado_pode_adicionar_produto_com_drop_ativo(self):
        user = User.objects.create_user(email="cart@shio.com", name="Cliente")
        self.client.force_authenticate(user=user)
        drop = DropCampaign.objects.create(
            name="DropAtivoCart", slug="drop-ativo-cart", is_public=True, is_active=True
        )
        product = Product.objects.create(
            category=self.category,
            drop=drop,
            name="ProdutoAtivo",
            description="x",
            base_price=15,
        )
        variation = ProductVariation.objects.create(
            product=product, size="U", sku="ATIVO-1", stock_quantity=10
        )

        response = self.client.post(
            self.cart_items_url,
            {"variation_id": str(variation.id), "quantity": 1},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["items"][0]["quantity"], 1)


class CheckoutRevalidationTests(APITestCase):
    """O checkout deve revalidar is_active/drop imediatamente antes de criar o
    pedido — mesmo que o item já estivesse no carrinho quando ainda era vendável."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="reval@shio.com", name="Reval", password="senha_forte_123"
        )
        self.address = make_address(self.user)
        self.drop = DropCampaign.objects.create(
            name="DropReval", slug="drop-reval", is_public=True, is_active=True
        )
        self.category = Category.objects.create(name="RevalCat", slug="reval-cat")
        self.product = Product.objects.create(
            category=self.category, drop=self.drop, name="ProdutoReval",
            description="x", base_price=80,
        )
        self.variation = ProductVariation.objects.create(
            product=self.product, size="M", sku="REVAL-M", stock_quantity=5
        )
        self.cart = Cart.objects.create(user=self.user, status="ACTIVE")
        self.cart_item = CartItem.objects.create(
            cart=self.cart, variation=self.variation, quantity=2, unit_price=80
        )
        self.client.force_authenticate(user=self.user)
        self.url = "/api/orders/checkout/"

    def test_checkout_bloqueado_quando_drop_fica_inativo_apos_adicionar_ao_carrinho(self):
        payload = make_checkout_payload(self.client, self.address)
        self.drop.is_active = False
        self.drop.save()

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.status, "ACTIVE")
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 5)
        self.assertEqual(CustomerOrder.objects.count(), 0)

    def test_checkout_bloqueado_quando_produto_fica_inativo_apos_adicionar_ao_carrinho(self):
        payload = make_checkout_payload(self.client, self.address)
        self.product.is_active = False
        self.product.save()

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 5)
        self.assertEqual(CustomerOrder.objects.count(), 0)


class CheckoutDropLimitTests(APITestCase):
    """Checkout deve recusar (409) quando a compra ultrapassaria o max_quantity
    do drop, contabilizado pela soma de unidades de pedidos não cancelados."""

    def setUp(self):
        self.drop = DropCampaign.objects.create(
            name="DropLimite", slug="drop-limite", is_public=True, is_active=True,
            max_quantity=3,
        )
        self.category = Category.objects.create(name="LimiteCat", slug="limite-cat")
        self.product = Product.objects.create(
            category=self.category, drop=self.drop, name="ProdutoLimite",
            description="x", base_price=40,
        )
        self.variation = ProductVariation.objects.create(
            product=self.product, size="U", sku="LIMITE-1", stock_quantity=100
        )

        existing_buyer = User.objects.create_user(email="buyer1@shio.com", name="Buyer1")
        make_address(existing_buyer)
        self.existing_order = CustomerOrder.objects.create(
            user=existing_buyer,
            subtotal=120,
            total_amount=120,
            status=OrderStatus.PAID,
            shipping_zip_code="71000000",
            shipping_street="R",
            shipping_number="1",
            shipping_neighborhood="B",
            shipping_city="C",
            shipping_state="DF",
        )
        OrderItem.objects.create(
            order=self.existing_order, variation=self.variation, quantity=3, unit_price=40,
            product_name="ProdutoLimite - U",
        )

        self.buyer = User.objects.create_user(
            email="buyer2@shio.com", name="Buyer2", password="senha_forte_123"
        )
        self.address = make_address(self.buyer)
        self.cart = Cart.objects.create(user=self.buyer, status="ACTIVE")
        CartItem.objects.create(
            cart=self.cart, variation=self.variation, quantity=1, unit_price=40
        )

        self.client.force_authenticate(user=self.buyer)
        self.url = "/api/orders/checkout/"

    def test_checkout_bloqueado_quando_excede_max_quantity_do_drop(self):
        self.existing_order.status = OrderStatus.CANCELED
        self.existing_order.save()
        payload = make_checkout_payload(self.client, self.address)
        self.existing_order.status = OrderStatus.PAID
        self.existing_order.save()

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.status, "ACTIVE")
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 100)

    @patch("orders.services.create_infinitepay_checkout")
    def test_cancelar_pedido_existente_libera_quantidade_para_novo_checkout(
        self, mock_checkout
    ):
        """Sem reservas: cancelar o pedido que ocupava o limite libera a
        quantidade automaticamente, pois deixa de ser contada (exclude CANCELED)."""
        mock_checkout.return_value = "https://pay.example.com/mock"

        self.existing_order.status = OrderStatus.CANCELED
        self.existing_order.save()
        payload = make_checkout_payload(self.client, self.address)

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.status, "FINISHED")
        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 99)


class ConcurrentCheckoutStockTests(TransactionTestCase):
    """Comprova que dois checkouts concorrentes para a última unidade em
    estoque não resultam em overselling: só um é confirmado."""

    def setUp(self):
        self.category = Category.objects.create(name="ConcCat", slug="conc-cat")
        self.product = Product.objects.create(
            category=self.category, name="ProdutoConcorrente", description="x",
            base_price=100,
        )
        self.variation = ProductVariation.objects.create(
            product=self.product, size="U", sku="CONC-STOCK-1", stock_quantity=1
        )

        self.user1 = User.objects.create_user(email="conc_stock1@shio.com", name="ConcStock1")
        self.user2 = User.objects.create_user(email="conc_stock2@shio.com", name="ConcStock2")

        self.address1 = make_address(self.user1)
        self.address2 = make_address(self.user2)

        for user in (self.user1, self.user2):
            cart = Cart.objects.create(user=user, status="ACTIVE")
            CartItem.objects.create(cart=cart, variation=self.variation, quantity=1, unit_price=100)

    @patch("orders.services.create_infinitepay_checkout")
    def test_checkout_concorrente_nao_ultrapassa_estoque(self, mock_checkout):
        mock_checkout.return_value = "https://pay.example.com/mock"
        results = {}

        # Tokens gerados fora das threads: RefreshToken.for_user() também
        # escreve no banco (OutstandingToken), e no SQLite ':memory:' dos
        # testes (dev/prod usam Postgres, com locking real de linha) qualquer
        # escrita concorrente enquanto a outra thread está em transação pode
        # ser recusada com "database table is locked" em vez de bloquear.
        token1 = str(RefreshToken.for_user(self.user1).access_token)
        token2 = str(RefreshToken.for_user(self.user2).access_token)

        quote_client1 = APIClient()
        quote_client1.force_authenticate(user=self.user1)
        payload1 = make_checkout_payload(quote_client1, self.address1)
        quote_client2 = APIClient()
        quote_client2.force_authenticate(user=self.user2)
        payload2 = make_checkout_payload(quote_client2, self.address2)

        def do_checkout(key, token, payload):
            client = APIClient(raise_request_exception=False)
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
            try:
                response = client.post(
                    "/api/orders/checkout/",
                    payload,
                    format="json",
                )
                results[key] = response.status_code
            except Exception:
                # Ver nota acima sobre SQLITE_LOCKED — tratado como "não
                # confirmado", igual a uma resposta de erro graciosa.
                results[key] = "error"
            finally:
                connection.close()

        t1 = threading.Thread(target=do_checkout, args=("t1", token1, payload1))
        t2 = threading.Thread(target=do_checkout, args=("t2", token2, payload2))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        statuses = list(results.values())
        self.assertEqual(len(statuses), 2)
        self.assertEqual(statuses.count(201), 1, f"esperado exatamente 1 sucesso, obtido {statuses}")

        self.variation.refresh_from_db()
        self.assertEqual(self.variation.stock_quantity, 0)
        self.assertEqual(
            CustomerOrder.objects.filter(status=OrderStatus.AWAITING_PAYMENT).count(), 1
        )


class ConcurrentCheckoutDropLimitTests(TransactionTestCase):
    """Comprova que dois checkouts concorrentes para a última unidade do
    max_quantity de um drop não resultam em overselling: só um é confirmado.

    Nota: a exclusão mútua real depende de select_for_update() no DropCampaign,
    que é aplicado de fato em Postgres (usado em dev/prod). No SQLite dos
    testes, select_for_update() é um no-op — este teste valida o comportamento
    fim-a-fim mas não substitui uma verificação de locking real em Postgres."""

    def setUp(self):
        self.drop = DropCampaign.objects.create(
            name="DropConcorrente", slug="drop-concorrente", is_public=True,
            is_active=True, max_quantity=1,
        )
        self.category = Category.objects.create(name="ConcDropCat", slug="conc-drop-cat")
        self.product = Product.objects.create(
            category=self.category, drop=self.drop, name="ProdutoDropConcorrente",
            description="x", base_price=50,
        )
        self.variation = ProductVariation.objects.create(
            product=self.product, size="U", sku="CONC-DROP-1", stock_quantity=10
        )

        self.user1 = User.objects.create_user(email="conc_drop1@shio.com", name="ConcDrop1")
        self.user2 = User.objects.create_user(email="conc_drop2@shio.com", name="ConcDrop2")

        self.address1 = make_address(self.user1)
        self.address2 = make_address(self.user2)

        for user in (self.user1, self.user2):
            cart = Cart.objects.create(user=user, status="ACTIVE")
            CartItem.objects.create(cart=cart, variation=self.variation, quantity=1, unit_price=50)

    @patch("orders.services.create_infinitepay_checkout")
    def test_checkout_concorrente_nao_ultrapassa_max_quantity(self, mock_checkout):
        mock_checkout.return_value = "https://pay.example.com/mock"
        results = {}

        # Ver comentário equivalente em ConcurrentCheckoutStockTests sobre
        # tokens gerados fora das threads e SQLITE_LOCKED do shared-cache.
        token1 = str(RefreshToken.for_user(self.user1).access_token)
        token2 = str(RefreshToken.for_user(self.user2).access_token)

        quote_client1 = APIClient()
        quote_client1.force_authenticate(user=self.user1)
        payload1 = make_checkout_payload(quote_client1, self.address1)
        quote_client2 = APIClient()
        quote_client2.force_authenticate(user=self.user2)
        payload2 = make_checkout_payload(quote_client2, self.address2)

        def do_checkout(key, token, payload):
            client = APIClient(raise_request_exception=False)
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
            try:
                response = client.post(
                    "/api/orders/checkout/",
                    payload,
                    format="json",
                )
                results[key] = response.status_code
            except Exception:
                results[key] = "error"
            finally:
                connection.close()

        t1 = threading.Thread(target=do_checkout, args=("t1", token1, payload1))
        t2 = threading.Thread(target=do_checkout, args=("t2", token2, payload2))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        statuses = list(results.values())
        self.assertEqual(len(statuses), 2)
        self.assertEqual(statuses.count(201), 1, f"esperado exatamente 1 sucesso, obtido {statuses}")

        total_sold = (
            OrderItem.objects.filter(variation__product__drop=self.drop)
            .exclude(order__status=OrderStatus.CANCELED)
            .aggregate(total=Sum("quantity"))["total"]
        )
        self.assertEqual(total_sold, 1)
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

        def _post(url, json=None, headers=None, timeout=None, **kwargs):
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
            make_checkout_payload(self.client, self.address),
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
