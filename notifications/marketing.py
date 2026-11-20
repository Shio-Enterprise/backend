"""Público de e-mails de marketing (newsletter e cupons): só quem aceitou.

Recebe quem deu o aceite (cadastro, Minha conta ou formulário da newsletter)
e não se descadastrou. Ter comprado não basta: sem aceite, não recebe.
O descadastro vence sempre. E-mails são comparados em minúsculas.
"""

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.utils import timezone

from authentication.models import NewsletterSubscriber
from orders.coupons import valid_order_q
from orders.models import CustomerOrder

User = get_user_model()


def normalize_email(email: str) -> str:
    return email.strip().lower()


@dataclass(frozen=True)
class Recipient:
    email: str
    name: str | None


@dataclass(frozen=True)
class Audience:
    recipients: list[Recipient]
    subscribers: int
    suppressed: int

    @property
    def total(self) -> int:
        return len(self.recipients)


def build_audience(*, first_purchase_only: bool = False) -> Audience:
    consented = NewsletterSubscriber.objects.filter(consent_lgpd=True)
    accepted = {
        normalize_email(e)
        for e in consented.filter(unsubscribed_at__isnull=True).values_list(
            "email", flat=True
        )
    }
    suppressed = consented.filter(unsubscribed_at__isnull=False).count()

    if first_purchase_only:
        has_order = {
            normalize_email(e)
            for e in CustomerOrder.objects.filter(valid_order_q()).values_list(
                "user__email", flat=True
            )
        }
        accepted -= has_order

    names = {
        normalize_email(e): n or None
        for e, n in User.objects.filter(is_active=True).values_list("email", "name")
    }

    recipients = [Recipient(e, names.get(e)) for e in sorted(accepted)]
    return Audience(
        recipients=recipients,
        subscribers=len(recipients),
        suppressed=suppressed,
    )


def is_receiving(email: str) -> bool:
    return NewsletterSubscriber.objects.filter(
        email__iexact=normalize_email(email),
        consent_lgpd=True,
        unsubscribed_at__isnull=True,
    ).exists()


def opt_in(email: str) -> None:
    email = normalize_email(email)
    sub = NewsletterSubscriber.objects.filter(email__iexact=email).first()
    if sub is None:
        NewsletterSubscriber.objects.create(email=email, consent_lgpd=True)
        return
    if sub.consent_lgpd and sub.unsubscribed_at is None:
        return
    # Novo aceite: a data passa a ser a de agora (o perfil no admin mostra, #103).
    sub.consent_lgpd = True
    sub.unsubscribed_at = None
    sub.subscribed_at = timezone.now()
    sub.save(update_fields=["consent_lgpd", "unsubscribed_at", "subscribed_at"])


def opt_out(email: str) -> None:
    email = normalize_email(email)
    sub = NewsletterSubscriber.objects.filter(email__iexact=email).first()
    if sub is None:
        NewsletterSubscriber.objects.create(email=email, unsubscribed_at=timezone.now())
        return
    sub.unsubscribed_at = timezone.now()
    sub.save(update_fields=["unsubscribed_at"])
