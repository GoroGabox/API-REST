"""Entry point del paquete `settings`.

Selecciona el módulo de settings basado en la variable de entorno
DJANGO_ENV (defaults: 'development' si DEBUG=True, sino 'production').

Soportados:
    DJANGO_ENV=development -> .development
    DJANGO_ENV=production  -> .production
    DJANGO_ENV=test        -> .test
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Cargar el .env ANTES de decidir el entorno. Si no, `os.getenv('DEBUG')` se
# evalúa antes de que base.py cargue el .env y, sin DJANGO_ENV explícito, el
# default cae a 'production' incluso en local — activando SECURE_SSL_REDIRECT,
# cookies seguras y HSTS, lo que rompe el desarrollo por HTTP.
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')

_env = os.getenv('DJANGO_ENV')
if not _env:
    # `manage.py test` usa siempre el entorno de test (DB en memoria, email
    # locmem, hasher rápido) sin necesidad de exportar DJANGO_ENV.
    if 'test' in sys.argv:
        _env = 'test'
    else:
        _env = 'development' if os.getenv('DEBUG', 'False') == 'True' else 'production'

if _env == 'production':
    from .production import *  # noqa: F401,F403
elif _env == 'test':
    from .test import *  # noqa: F401,F403
else:
    from .development import *  # noqa: F401,F403
