"""Genera llaves para el modo offline o muestra la llave pública vigente.

    python manage.py offline_llaves            # llave pública actual (embeber en la app)
    python manage.py offline_llaves --nuevas   # valores nuevos para .env + su llave pública
"""
import base64
import secrets

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from django.core.management.base import BaseCommand

from offline.crypto import b64url, llave_publica_b64


class Command(BaseCommand):
    help = 'Llaves del modo offline (firma Ed25519 y llave maestra de contenido).'

    def add_arguments(self, parser):
        parser.add_argument('--nuevas', action='store_true', help='Genera valores nuevos para el .env.')

    def handle(self, *args, **opts):
        if not opts['nuevas']:
            self.stdout.write(f'OFFLINE_PUBLIC_KEY={llave_publica_b64()}')
            return
        semilla = secrets.token_bytes(32)
        pub = Ed25519PrivateKey.from_private_bytes(semilla).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.stdout.write(f'OFFLINE_SIGNING_KEY={base64.urlsafe_b64encode(semilla).decode()}')
        self.stdout.write(f'OFFLINE_MASTER_KEY={base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}')
        self.stdout.write(f'# Llave pública para la app (EXPO_PUBLIC_OFFLINE_PUBLIC_KEY): {b64url(pub)}')
