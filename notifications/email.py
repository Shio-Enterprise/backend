"""Ponto único de envio de e-mails do sistema.

Uso:
    from notifications.email import send_email

    send_email(user.email, "Assunto", "nome_do_template", {"nome": user.name})

Cada e-mail é um par de templates em `templates/emails/<nome>.txt` e
`templates/emails/<nome>.html`, ambos estendendo os templates `base`.
"""

import logging
from datetime import date

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def mask_email(address: str) -> str:
    """Esconde o endereço para os logs (LGPD): `arthur@gmail.com` -> `ar***@gmail.com`."""
    user, sep, domain = address.partition("@")
    if not sep:
        return "***"
    visible = min(2, max(len(user) - 1, 0))
    return f"{user[:visible]}***@{domain}"


def send_email(
    to: str | list[str],
    subject: str,
    template: str,
    context: dict | None = None,
) -> bool:
    """Envia e-mail em texto + HTML. Nunca lança exceção: falhas viram log e `False`."""
    recipients = [r for r in ([to] if isinstance(to, str) else to) if r]
    if not recipients:
        logger.warning("E-mail '%s' sem destinatário; nada foi enviado.", template)
        return False

    masked = ", ".join(mask_email(r) for r in recipients)
    try:
        full_context = {
            "subject": subject,
            "year": date.today().year,
            **(context or {}),
        }
        text_body = render_to_string(f"emails/{template}.txt", full_context)
        html_body = render_to_string(f"emails/{template}.html", full_context)
        message = EmailMultiAlternatives(
            subject, text_body, settings.DEFAULT_FROM_EMAIL, recipients
        )
        message.attach_alternative(html_body, "text/html")
        message.send(fail_silently=False)
    except Exception as exc:
        # Sem traceback nem str(exc): erros do provedor podem conter o endereço.
        logger.error(
            "Falha ao enviar e-mail '%s' para %s (%s).",
            template,
            masked,
            type(exc).__name__,
        )
        return False

    logger.info("E-mail '%s' enviado para %s.", template, masked)
    return True
