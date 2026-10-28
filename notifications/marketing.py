"""Público de e-mails de marketing: quem compra ou aceitou, menos quem bloqueou.

A = compradores (pedido pago ou além, usuário ativo)
B = inscritos na newsletter com consentimento
C = descadastrados (bloqueio vence sempre)
Recebe: (A ∪ B) − C. E-mails são comparados em minúsculas.
"""

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.utils import timezone

from authentication.models import NewsletterSubscriber
from orders.coupons import valid_order_q
from orders.models import CustomerOrder, OrderStatus

User = get_user_model()

BUYER_STATUSES = [
    OrderStatus.PAID,
    OrderStatus.PREPARING,
    OrderStatus.SHIPPED,
    OrderStatus.DELIVERED,
]


def normalize_email(email: str) -> str:
    return email.strip().lower()


@dataclass(frozen=True)
class Recipient:
    email: str
    name: str | None


@dataclass(frozen=True)
class Audience:
    recipients: list[Recipient]
    buyers: int
    subscribers: int
    overlap: int
    suppressed: int

    @property
    def total(self) -> int:
        return len(self.recipients)


def _buyers() -> dict[str, str | None]:
    """E-mail normalizado -> nome do usuário."""
    rows = CustomerOrder.objects.filter(
        status__in=BUYER_STATUSES, user__is_active=True
    ).values_list("user__email", "user__name")
    buyers: dict[str, str | None] = {}
    for email, name in rows:
        buyers.setdefault(normalize_email(email), name or None)
    return buyers


def _blocked() -> set[str]:
    return {
        normalize_email(e)
        for e in NewsletterSubscriber.objects.filter(
            unsubscribed_at__isnull=False
        ).values_list("email", flat=True)
    }


def build_audience(*, first_purchase_only: bool = False) -> Audience:
    subscribers = {
        normalize_email(e)
        for e in NewsletterSubscriber.objects.filter(consent_lgpd=True).values_list(
            "email", flat=True
        )
    }
    buyers: dict[str, str | None] = {}
    if first_purchase_only:
        has_order = {
            normalize_email(e)
            for e in CustomerOrder.objects.filter(valid_order_q()).values_list(
                "user__email", flat=True
            )
        }
        subscribers -= has_order
    else:
        buyers = _buyers()

    blocked = _blocked()
    everyone = set(buyers) | subscribers
    final = everyone - blocked

    names = {
        normalize_email(e): n or None
        for e, n in User.objects.filter(is_active=True).values_list("email", "name")
    }

    recipients = [Recipient(e, names.get(e)) for e in sorted(final)]
    return Audience(
        recipients=recipients,
        buyers=len(set(buyers) & final),
        subscribers=len(subscribers & final),
        overlap=len(set(buyers) & subscribers & final),
        suppressed=len(everyone & blocked),
    )


def is_receiving(email: str) -> bool:
    email = normalize_email(email)
    sub = NewsletterSubscriber.objects.filter(email__iexact=email).first()
    if sub and sub.unsubscribed_at is not None:
        return False
    if sub and sub.consent_lgpd:
        return True
    return CustomerOrder.objects.filter(
        status__in=BUYER_STATUSES, user__is_active=True, user__email__iexact=email
    ).exists()


def opt_in(email: str) -> None:
    email = normalize_email(email)
    sub = NewsletterSubscriber.objects.filter(email__iexact=email).first()
    if sub is None:
        NewsletterSubscriber.objects.create(email=email, consent_lgpd=True)
        return
    sub.consent_lgpd = True
    sub.unsubscribed_at = None
    sub.save(update_fields=["consent_lgpd", "unsubscribed_at"])


def opt_out(email: str) -> None:
    email = normalize_email(email)
    sub = NewsletterSubscriber.objects.filter(email__iexact=email).first()
    if sub is None:
        NewsletterSubscriber.objects.create(email=email, unsubscribed_at=timezone.now())
        return
    sub.unsubscribed_at = timezone.now()
    sub.save(update_fields=["unsubscribed_at"])
