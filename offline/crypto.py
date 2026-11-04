"""Llaves del modo offline: firma de licencias (Ed25519) y derivación de la CEK.

Config (env / settings):
    OFFLINE_SIGNING_KEY  -> semilla Ed25519 de 32 bytes en base64 (privada).
    OFFLINE_MASTER_KEY   -> 32 bytes en base64; de ella se deriva cada CEK.

Sin config se derivan de ``SECRET_KEY`` (sirve en dev/test). En producción conviene
fijarlas: la app embebe la llave pública y cambiar ``SECRET_KEY`` la rompería.
Genera valores nuevos con ``python manage.py offline_llaves``.
"""
from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from django.conf import settings


def _b64d(valor: str) -> bytes:
    valor = valor.strip()
    return base64.urlsafe_b64decode(valor + '=' * (-len(valor) % 4))


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def _hkdf(material: bytes, info: bytes, salt: bytes | None = None) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(material)


def _secreto(nombre: str, info: bytes) -> bytes:
    valor = getattr(settings, nombre, '') or ''
    if valor:
        data = _b64d(valor)
        if len(data) != 32:
            raise ValueError(f'{nombre} debe ser de 32 bytes en base64.')
        return data
    return _hkdf(settings.SECRET_KEY.encode(), info)


def llave_firma() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_secreto('OFFLINE_SIGNING_KEY', b'autotest-offline-firma'))


def llave_publica_b64() -> str:
    pub = llave_firma().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return b64url(pub)


def firmar(payload: dict) -> tuple[str, str]:
    """Serializa la licencia y la firma. Devuelve (licencia_json, firma_b64url).

    La app verifica la firma sobre los bytes exactos de ``licencia_json`` (no
    re-serializa), así que el formato del JSON no importa.
    """
    texto = json.dumps(payload, separators=(',', ':'), sort_keys=True, ensure_ascii=False)
    firma = llave_firma().sign(texto.encode('utf-8'))
    return texto, b64url(firma)


def cek(licencia) -> bytes:
    """Llave de contenido (AES-256) de una licencia. Estable mientras no cambie la sal."""
    master = _secreto('OFFLINE_MASTER_KEY', b'autotest-offline-master')
    info = f'autotest-offline-cek|{licencia.id}|{licencia.dispositivo.device_id}|{licencia.curso_id}'.encode()
    return _hkdf(master, info, salt=bytes.fromhex(licencia.sal))
