"""Entry point del paquete `settings`.

Selecciona el módulo de settings basado en la variable de entorno
DJANGO_ENV (defaults: 'development' si DEBUG=True, sino 'production').

Soportados:
    DJANGO_ENV=development -> .development
    DJANGO_ENV=production  -> .production
    DJANGO_ENV=test        -> .test
"""
import os

_env = os.getenv('DJANGO_ENV')
if not _env:
    _env = 'development' if os.getenv('DEBUG', 'False') == 'True' else 'production'

if _env == 'production':
    from .production import *  # noqa: F401,F403
elif _env == 'test':
    from .test import *  # noqa: F401,F403
else:
    from .development import *  # noqa: F401,F403
