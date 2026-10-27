from django.core.management.base import BaseCommand

from products.drop_waitlist import notify_open_drops, open_drops_with_pending_waitlist


class Command(BaseCommand):
    help = (
        "Envia o e-mail de abertura para a lista de espera dos drops que já "
        "abriram. Para agendar, rode de novo no intervalo (ex.: a cada minuto)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Só lista os drops que seriam avisados, sem enviar nada.",
        )

    def handle(self, *args, dry_run, **options):
        if dry_run:
            drops = list(open_drops_with_pending_waitlist())
            for drop in drops:
                pending = drop.waitlist_entries.filter(notified_at__isnull=True)
                self.stdout.write(f"{drop.slug}  {pending.count()} pendente(s)")
            self.stdout.write(
                self.style.WARNING(f"{len(drops)} drop(s) seriam avisados (--dry-run).")
            )
            return

        sent = notify_open_drops()
        if sent:
            self.stdout.write(self.style.SUCCESS(f"{sent} e-mail(s) enviados."))
        else:
            self.stdout.write("Nenhum e-mail de abertura para enviar.")
