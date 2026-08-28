"""Endpoint de health check para el PaaS / balanceador (Railway).

Verifica que la app responde y que la base de datos está accesible. No requiere
autenticación y está exento del redirect a HTTPS (ver SECURE_REDIRECT_EXEMPT en
production.py) para que el chequeo interno del PaaS no reciba un 301.
"""
from django.db import connection
from django.http import JsonResponse


def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        db_ok = True
    except Exception:
        db_ok = False

    return JsonResponse(
        {'status': 'ok' if db_ok else 'error', 'database': db_ok},
        status=200 if db_ok else 503,
    )
