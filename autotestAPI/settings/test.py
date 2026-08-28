"""Settings para correr tests.

Aísla DB y email para que la suite no toque infra externa.
"""
from .base import *  # noqa: F401,F403

DEBUG = False

# DB en memoria para tests rápidos.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

# Hasher rápido para fixtures.
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'

# Throttling desactivado en tests: la suite hace muchas llamadas repetidas
# (p. ej. probar códigos 2FA inválidos) y debe ser determinista, no recibir 429.
# Se conservan las keys de scope con rate None (desactiva sin romper la
# instanciación de los throttles a nivel de vista, que exigen que el scope exista).
REST_FRAMEWORK = {
    **REST_FRAMEWORK,  # noqa: F405  (definido en base.py)
    'DEFAULT_THROTTLE_CLASSES': (),
    'DEFAULT_THROTTLE_RATES': {
        k: None for k in REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']  # noqa: F405
    },
}
