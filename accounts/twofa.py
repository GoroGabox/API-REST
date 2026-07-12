"""Utilidades para códigos de recuperación 2FA.

Los códigos son tokens de un solo uso, de alta entropía, que permiten iniciar
sesión cuando el usuario pierde su app autenticadora. Se almacenan **hasheados**
(SHA-256) en `Usuario.totp_recovery_codes`; el texto plano se muestra una única
vez al generarlos. Al ser aleatorios y de alta entropía, un SHA-256 sin salt es
suficiente frente a fuerza bruta.
"""
import hashlib
import secrets

# Alfabeto sin caracteres ambiguos (0/O, 1/I/L).
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
RECOVERY_CODE_COUNT = 8
_RAW_LEN = 10  # caracteres antes de formatear como XXXXX-XXXXX


def _normalize(code: str) -> str:
    """Quita separadores y normaliza a mayúsculas para comparar/hashear."""
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def hash_code(code: str) -> str:
    return hashlib.sha256(_normalize(code).encode()).hexdigest()


def generate_recovery_codes(n: int = RECOVERY_CODE_COUNT):
    """Devuelve `(plaintext_list, hashed_list)`.

    `plaintext_list` se muestra al usuario UNA sola vez; `hashed_list` es lo que
    se persiste.
    """
    plain = []
    for _ in range(n):
        raw = "".join(secrets.choice(_ALPHABET) for _ in range(_RAW_LEN))
        plain.append(f"{raw[:5]}-{raw[5:]}")
    hashed = [hash_code(c) for c in plain]
    return plain, hashed


def consume_recovery_code(user, code: str) -> bool:
    """Si `code` coincide con un código sin usar, lo elimina y persiste,
    devolviendo True. En caso contrario devuelve False.
    """
    if not code:
        return False
    target = hash_code(code)
    codes = list(user.totp_recovery_codes or [])
    if target in codes:
        codes.remove(target)
        user.totp_recovery_codes = codes
        user.save(update_fields=["totp_recovery_codes"])
        return True
    return False
