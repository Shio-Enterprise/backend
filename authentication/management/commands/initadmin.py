from decouple import config
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Cria um superusuário com base nas variáveis de ambiente se ainda não existir."
    )

    def handle(self, *args, **options):
        User = get_user_model()
        email = config("ADMIN_EMAIL")
        name = config("ADMIN_NAME")
        password = config("ADMIN_PASS")

        if User.objects.filter(email__iexact=email.strip()).exists():
            self.stdout.write(self.style.WARNING("Conta de administrador já existe!"))
            return

        User.objects.create_superuser(
            email=email,
            password=password,
            name=name,
        )

        self.stdout.write(self.style.SUCCESS(f"Conta do usuário {email} foi criada!"))
