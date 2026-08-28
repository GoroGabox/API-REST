"""Settings para producción.

Endurece flags de seguridad y asume HTTPS + headers correctos.
"""
import os

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
# El health check del PaaS puede llegar por HTTP interno; no redirigir a HTTPS.
SECURE_REDIRECT_EXEMPT = [r'^healthz/?$']
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Logging a stderr (los PaaS modernos lo capturan) + email a ADMINS en error 500.
# El handler mail_admins solo envía si ADMINS está definido y el email configurado.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'level': 'WARNING',
            'class': 'logging.StreamHandler',
        },
        'mail_admins': {
            'level': 'ERROR',
            'class': 'django.utils.log.AdminEmailHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
    'loggers': {
        'django.request': {
            # mail_admins solo si hay SMTP real configurado; si no, un error 500
            # intentaría enviar correo con credenciales vacías y colgaría el request.
            'handlers': ['console', 'mail_admins'] if EMAIL_CONFIGURED else ['console'],
            'level': 'ERROR',
            'propagate': False,
        },
    },
}

# ============================================================================
# Error tracking — Sentry (opcional).
# Se activa solo si SENTRY_DSN está definido; sin DSN es un no-op (no rompe nada).
# ============================================================================
SENTRY_DSN = os.environ.get('SENTRY_DSN', '')
if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=os.environ.get('SENTRY_ENVIRONMENT', 'production'),
        # Muestreo de performance; 0.0 = solo errores (sin costo de trazas).
        traces_sample_rate=float(os.environ.get('SENTRY_TRACES_SAMPLE_RATE', '0.0')),
        # No enviar PII (emails, IPs) salvo que se active explícitamente.
        send_default_pii=os.environ.get('SENTRY_SEND_PII', 'False') == 'True',
    )
