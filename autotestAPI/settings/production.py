"""Settings para producción.

Endurece flags de seguridad y asume HTTPS + headers correctos.
"""
from .base import *  # noqa: F401,F403

DEBUG = False

# Hosts permitidos deben venir de .env explícitamente — sin fallback laxo.
if not ALLOWED_HOSTS or ALLOWED_HOSTS == ['localhost', '127.0.0.1']:
    raise RuntimeError(
        "ALLOWED_HOSTS no configurado para producción. Definir en .env "
        "(ej. ALLOWED_HOSTS=api.dominio.com)."
    )

# CORS estricto.
CORS_ALLOW_ALL_ORIGINS = False

# Cabeceras y cookies seguras.
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Logging básico a stderr (los PaaS modernos capturan stderr).
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'level': 'WARNING',
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
    'loggers': {
        'django.request': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': False,
        },
    },
}
