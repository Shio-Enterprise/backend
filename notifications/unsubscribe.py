"""Link de descadastro de e-mails de marketing (token assinado, sem expiração)."""

from django.conf import settings
from django.core import signing

from notifications.marketing import normalize_email

SALT = "shio.marketing.unsubscribe"


def make_unsubscribe_token(email: str) -> str:
    return signing.dumps(normalize_email(email), salt=SALT)


def read_unsubscribe_token(token: str) -> str | None:
    try:
        return signing.loads(token, salt=SALT)
    except signing.BadSignature:
        return None


def unsubscribe_url(email: str) -> str:
    return f"{settings.FRONTEND_URL}/descadastrar/{make_unsubscribe_token(email)}"
