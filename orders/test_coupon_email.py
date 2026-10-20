from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.utils import timezone

from authentication.models import NewsletterSubscriber
from orders.coupon_email import (
    COUPON_EMAIL_SUBJECT,
    coupon_conditions,
    coupon_send_block_reason,
    render_coupon_preview,
    send_coupon_campaign,
    send_coupon_test,
)
from orders.models import (
    Coupon,
    CouponDiscountType,
    CouponEmailSend,
    CustomerOrder,
    OrderStatus,
)

User = get_user_model()


def make_user(email, name="Fulano"):
    return User.objects.create_user(email=email, password="senha-forte-123", name=name)


def make_coupon(**kwargs):
    defaults = {
        "code": "VERAO10",
        "discount_type": CouponDiscountType.PERCENTAGE,
        "discount_value": Decimal("10"),
    }
    defaults.update(kwargs)
    return Coupon.objects.create(**defaults)


def make_order(user, coupon=None, status=OrderStatus.PAID):
    return CustomerOrder.objects.create(
        user=user,
        status=status,
        coupon=coupon,
        subtotal=100,
        total_amount=100,
        shipping_zip_code="70000-000",
        shipping_street="Rua A",
        shipping_number="1",
        shipping_neighborhood="Centro",
        shipping_city="Brasília",
        shipping_state="DF",
    )


class CouponBlockReasonTests(TestCase):
    def test_bloqueio_inativo_vencido_esgotado(self):
        self.assertIsNone(coupon_send_block_reason(make_coupon()))
        inactive = make_coupon(code="INATIVO", is_active=False)
        self.assertEqual(coupon_send_block_reason(inactive), "Cupom inativo.")
        expired = make_coupon(
            code="VENCIDO", expiration_date=timezone.now() - timedelta(days=1)
        )
        self.assertEqual(coupon_send_block_reason(expired), "Cupom vencido.")
        sold_out = make_coupon(code="ESGOTADO", max_uses_total=1)
        make_order(make_user("a@x.com"), coupon=sold_out)
        self.assertEqual(coupon_send_block_reason(sold_out), "Cupom esgotado.")

    def test_inicio_futuro_nao_bloqueia(self):
        coupon = make_coupon(starts_at=timezone.now() + timedelta(days=3))
        self.assertIsNone(coupon_send_block_reason(coupon))


class CouponConditionsTests(TestCase):
    def test_condicoes_percentual_com_teto_minimo_e_validade(self):
        coupon = make_coupon(
            max_discount_amount=Decimal("50.00"),
            min_order_value=Decimal("1234.56"),
            expiration_date=timezone.now() + timedelta(days=10),
            first_purchase_only=True,
        )
        expires = timezone.localtime(coupon.expiration_date).strftime("%d/%m/%Y")
        self.assertEqual(
            coupon_conditions(coupon),
            [
                "10% de desconto (até R$ 50,00)",
                "Pedidos acima de R$ 1.234,56",
                f"Válido até {expires}",
                "Só para a primeira compra",
            ],
        )

    def test_condicoes_valor_fixo_inicio_futuro_e_escopo(self):
        from products.models import Category

        category = Category.objects.create(name="Camisetas", slug="camisetas")
        starts = timezone.now() + timedelta(days=2)
        coupon = make_coupon(
            code="FIXO20",
            discount_type=CouponDiscountType.FIXED_VALUE,
            discount_value=Decimal("20"),
            starts_at=starts,
        )
        coupon.categories.add(category)
        self.assertEqual(
            coupon_conditions(coupon),
            [
                "R$ 20,00 de desconto",
                f"Válido a partir de {timezone.localtime(starts).strftime('%d/%m/%Y')}",
                "Válido em: Camisetas",
            ],
        )


class CouponCampaignTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin@shio.com", name="Admin")
        self.coupon = make_coupon()

    def _audience(self):
        make_order(make_user("ana@x.com", name="Ana"))
        NewsletterSubscriber.objects.create(email="bia@x.com", consent_lgpd=True)

    def test_envia_um_email_por_pessoa_com_link_proprio(self):
        self._audience()
        record = send_coupon_campaign(self.coupon, self.admin, "Aproveite!")
        self.assertEqual(len(mail.outbox), 2)
        for message in mail.outbox:
            self.assertEqual(len(message.to), 1)
            self.assertEqual(message.subject, COUPON_EMAIL_SUBJECT)
            self.assertIn("VERAO10", message.body)
        links = {
            line
            for message in mail.outbox
            for line in message.body.splitlines()
            if "/descadastrar/" in line
        }
        self.assertEqual(len(links), 2)
        self.assertEqual(COUPON_EMAIL_SUBJECT, "Um cupom para você — Shio")
        self.assertIsInstance(record, CouponEmailSend)
        self.assertEqual(record.sent_count, 2)
        self.assertEqual(record.failed_count, 0)
        self.assertEqual(record.sent_by, self.admin)
        self.assertEqual(record.message, "Aproveite!")
        self.assertEqual(record.coupon, self.coupon)

    def test_mensagem_do_admin_e_escapada(self):
        html = render_coupon_preview(self.coupon, "<script>x</script>\nlinha 2")
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>x", html)
        self.assertIn("<br>", html)
        self.assertIn("Não quero receber promoções", html)

    def test_saudacao_com_nome_e_sem_conta(self):
        self._audience()
        send_coupon_campaign(self.coupon, self.admin, "")
        bodies = {m.to[0]: m.body for m in mail.outbox}
        self.assertIn("Olá, Ana!", bodies["ana@x.com"])
        self.assertIn("Olá!", bodies["bia@x.com"])
        self.assertNotIn("Olá, !", bodies["bia@x.com"])

    def test_falha_conta_e_continua(self):
        self._audience()
        with patch(
            "orders.coupon_email.send_email", side_effect=[False, True]
        ) as mocked:
            record = send_coupon_campaign(self.coupon, self.admin, "oi")
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(record.sent_count, 1)
        self.assertEqual(record.failed_count, 1)

    def test_primeira_compra_exclui_quem_ja_tem_pedido(self):
        self.coupon.first_purchase_only = True
        self.coupon.save()
        self._audience()
        record = send_coupon_campaign(self.coupon, self.admin, "")
        self.assertEqual([m.to[0] for m in mail.outbox], ["bia@x.com"])
        self.assertEqual(record.sent_count, 1)

    def test_teste_vai_so_para_admin_e_nao_cria_historico(self):
        self._audience()
        self.assertTrue(send_coupon_test(self.coupon, self.admin, "teste"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["admin@shio.com"])
        self.assertEqual(CouponEmailSend.objects.count(), 0)
