import logging

from django.dispatch import receiver

from authentication.signals import login_completed

from .services import merge_session_cart_to_db

logger = logging.getLogger(__name__)


@receiver(login_completed)
def merge_guest_cart_on_login(sender, request, user, **kwargs):
    """Move o carrinho do visitante (sessão) para a conta ao entrar."""
    try:
        merge_session_cart_to_db(request, user)
    except Exception:
        logger.exception("Erro ao mesclar carrinho da sessão após login")
