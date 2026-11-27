from django.db import connection
from django.db.utils import OperationalError
from django.http import JsonResponse


def health_check(_request):
    """Public liveness/readiness endpoint verifying database connectivity."""
    try:
        connection.ensure_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except OperationalError:
        return JsonResponse({"status": "error", "database": "unavailable"}, status=503)

    return JsonResponse({"status": "ok", "database": "ok"}, status=200)
