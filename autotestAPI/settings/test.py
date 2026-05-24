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
