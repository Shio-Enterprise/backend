"""Escolha do backend de e-mail conforme a credencial do provedor.

Este módulo é importado por `core/settings/base.py`, então não pode importar
nada do Django no topo (as settings ainda não existem nesse momento).
"""

import logging

RESEND_BACKEND = "anymail.backends.resend.EmailBackend"
CONSOLE_BACKEND = "django.core.mail.backends.console.EmailBackend"

logger = logging.getLogger(__name__)


def select_email_backend(api_key: str) -> str:
    """Resend quando há chave; senão console (e-mail só aparece no log)."""
    return RESEND_BACKEND if api_key.strip() else CONSOLE_BACKEND


def warn_if_email_disabled() -> None:
    """Avisa no log quando produção está sem provedor configurado."""
    from django.conf import settings

    if not settings.DEBUG and settings.EMAIL_BACKEND == CONSOLE_BACKEND:
        logger.warning(
            "RESEND_API_KEY ausente: e-mails não serão enviados, apenas exibidos no log."
        )
