"""Settings para desarrollo local."""
from .base import *  # noqa: F401,F403

DEBUG = True

# En dev, fuerza fallback a sqlite si DATABASE_URL no está cableada.
# (En base.py ya hay fallback automático; este flag solo documenta intención.)

# Email a consola en lugar de SMTP real.
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
