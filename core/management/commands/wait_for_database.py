from time import monotonic, sleep

from django.core.management.base import BaseCommand, CommandError
from django.db import connections


class Command(BaseCommand):
    help = "Waits for the configured Django database to accept a connection."

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=int, default=60)
        parser.add_argument("--interval", type=float, default=2)

    def handle(self, *args, **options):
        timeout = max(1, options["timeout"])
        interval = max(0.1, options["interval"])
        deadline = monotonic() + timeout
        last_error = None

        while monotonic() < deadline:
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT 1")
                self.stdout.write(self.style.SUCCESS("Database ready."))
                return
            except Exception as exc:  # Database drivers expose different errors.
                last_error = exc
                sleep(min(interval, max(0, deadline - monotonic())))

        raise CommandError(f"Database unavailable after {timeout}s: {last_error}")
