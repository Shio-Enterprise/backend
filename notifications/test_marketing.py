from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from authentication.models import NewsletterSubscriber
from notifications.marketing import (
    Recipient,
    build_audience,
    is_receiving,
    normalize_email,
    opt_in,
    opt_out,
)
from orders.models import CustomerOrder, OrderStatus

User = get_user_model()


def make_user(email, name="Fulano", is_active=True):
    return User.objects.create_user(
        email=email, password="senha-forte-123", name=name, is_active=is_active
    )


def make_order(user, status=OrderStatus.PAID):
    return CustomerOrder.objects.create(
        user=user,
        status=status,
        subtotal=100,
        total_amount=100,
        shipping_zip_code="70000-000",
        shipping_street="Rua A",
        shipping_number="1",
        shipping_neighborhood="Centro",
        shipping_city="Brasília",
        shipping_state="DF",
    )


def make_subscriber(email, consent=True, unsubscribed=False):
    return NewsletterSubscriber.objects.create(
        email=email,
        consent_lgpd=consent,
        unsubscribed_at=timezone.now() if unsubscribed else None,
    )


def emails(audience):
    return [r.email for r in audience.recipients]


class MarketingAudienceTests(TestCase):
    def test_normalize_email(self):
        self.assertEqual(normalize_email("  Ana@X.COM "), "ana@x.com")

    def test_comprador_pago_recebe(self):
        make_order(make_user("ana@x.com"))
        audience = build_audience()
        self.assertEqual(emails(audience), ["ana@x.com"])
        self.assertEqual(audience.buyers, 1)
        self.assertEqual(audience.total, 1)

    def test_pedido_aguardando_ou_cancelado_nao_conta(self):
        user = make_user("ana@x.com")
        make_order(user, OrderStatus.AWAITING_PAYMENT)
        make_order(user, OrderStatus.CANCELED)
        self.assertEqual(build_audience().total, 0)

    def test_usuario_inativo_nao_recebe(self):
        make_order(make_user("ana@x.com", is_active=False))
        self.assertEqual(build_audience().total, 0)

    def test_inscrito_com_consentimento_recebe_sem_conta(self):
        make_subscriber("bia@x.com")
        audience = build_audience()
        self.assertEqual(audience.recipients, [Recipient("bia@x.com", None)])
        self.assertEqual(audience.subscribers, 1)

    def test_inscrito_sem_consentimento_nao_recebe(self):
        make_subscriber("bia@x.com", consent=False)
        self.assertEqual(build_audience().total, 0)

    def test_descadastrado_nao_recebe_mesmo_comprando(self):
        make_order(make_user("ana@x.com"))
        make_subscriber("ana@x.com", unsubscribed=True)
        audience = build_audience()
        self.assertEqual(audience.total, 0)
        self.assertEqual(audience.suppressed, 1)

    def test_deduplica_e_bloqueia_ignorando_maiusculas(self):
        make_order(make_user("Ana@X.com"))
        make_subscriber("ana@x.com")
        audience = build_audience()
        self.assertEqual(audience.total, 1)
        self.assertEqual(audience.overlap, 1)
        opt_out("ANA@x.com")
        self.assertEqual(build_audience().total, 0)

    def test_primeira_compra_so_inscritos_sem_pedido_valido(self):
        make_subscriber("com-pedido@x.com")
        make_order(make_user("com-pedido@x.com"), OrderStatus.AWAITING_PAYMENT)
        make_subscriber("sem-pedido@x.com")
        make_order(make_user("comprador@x.com"))
        audience = build_audience(first_purchase_only=True)
        self.assertEqual(emails(audience), ["sem-pedido@x.com"])

    def test_nome_vem_do_usuario_quando_existe(self):
        make_order(make_user("ana@x.com", name="Ana Souza"))
        make_subscriber("ana@x.com")
        self.assertEqual(
            build_audience().recipients, [Recipient("ana@x.com", "Ana Souza")]
        )

    def test_opt_in_reativa_e_opt_out_bloqueia(self):
        opt_in("Bia@x.com")
        sub = NewsletterSubscriber.objects.get(email="bia@x.com")
        self.assertTrue(sub.consent_lgpd)
        self.assertIsNone(sub.unsubscribed_at)
        opt_out("bia@x.com")
        sub.refresh_from_db()
        self.assertIsNotNone(sub.unsubscribed_at)
        self.assertTrue(sub.consent_lgpd)
        opt_in("bia@x.com")
        sub.refresh_from_db()
        self.assertIsNone(sub.unsubscribed_at)

    def test_is_receiving(self):
        make_order(make_user("ana@x.com"))
        self.assertTrue(is_receiving("ana@x.com"))
        opt_out("ana@x.com")
        self.assertFalse(is_receiving("ANA@x.com"))
        opt_in("ana@x.com")
        self.assertTrue(is_receiving("ana@x.com"))
        self.assertFalse(is_receiving("ninguem@x.com"))
