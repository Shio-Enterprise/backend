from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from notifications.backends import CONSOLE_BACKEND
from notifications.email import send_email


class Command(BaseCommand):
    help = "Envia um e-mail de teste para validar a configuração de envio."

    def add_arguments(self, parser):
        parser.add_argument("destino", help="E-mail que vai receber o teste.")

    def handle(self, *args, destino, **options):
        if not send_email(destino, "Teste de envio — Shio", "teste"):
            raise CommandError(
                "Falha ao enviar o e-mail de teste. Veja o log para detalhes."
            )

        self.stdout.write(
            self.style.SUCCESS(f"E-mail de teste enviado para {destino}.")
        )
        if settings.EMAIL_BACKEND == CONSOLE_BACKEND:
            self.stdout.write(
                self.style.WARNING(
                    "Backend de console ativo (RESEND_API_KEY ausente): "
                    "e-mail exibido no log, não enviado."
                )
            )
