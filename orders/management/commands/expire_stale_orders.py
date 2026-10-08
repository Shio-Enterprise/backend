from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from orders.expiration import expired_reservations, release_expired_reservations


class Command(BaseCommand):
    help = (
        "Libera reservas de estoque vencidas (pedidos aguardando pagamento). "
        "Processa um lote por execução; para agendar, rode de novo no intervalo."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Só lista os pedidos que seriam liberados, sem alterar nada.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Máximo de pedidos por execução (padrão: 100).",
        )

    def handle(self, *args, dry_run, batch_size, **options):
        if batch_size < 1:
            raise CommandError("--batch-size precisa ser maior que zero.")

        if dry_run:
            orders = list(expired_reservations().prefetch_related("items")[:batch_size])
            for order in orders:
                self.stdout.write(
                    f"{order.pk}  venceu em {timezone.localtime(order.reservation_expires_at):%d/%m %H:%M}"
                    f"  {sum(item.quantity for item in order.items.all())} unidade(s)"
                )
            self.stdout.write(
                self.style.WARNING(
                    f"{len(orders)} reserva(s) seriam liberadas (--dry-run)."
                )
            )
            return

        released = release_expired_reservations(batch_size=batch_size)
        if released:
            self.stdout.write(self.style.SUCCESS(f"{released} reserva(s) liberadas."))
        else:
            self.stdout.write("Nenhuma reserva vencida para liberar.")
