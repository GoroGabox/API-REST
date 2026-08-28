"""Throttles con scope para endpoints sensibles (anti fuerza bruta / abuso).

Los ritmos se definen en ``settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']``.

Nota de infraestructura: el conteo usa el cache de Django (``LocMemCache`` por
defecto), que NO se comparte entre workers de gunicorn — con N workers el límite
efectivo es ~N veces el configurado. Es protección base suficiente para el MVP;
para límites exactos y compartidos, configurar un cache Redis.
"""
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle


class LoginRateThrottle(AnonRateThrottle):
    """Limita intentos de login por IP."""
    scope = 'login'


class PasswordResetRateThrottle(AnonRateThrottle):
    """Limita solicitudes/confirmaciones de reset de contraseña por IP."""
    scope = 'password_reset'


class RegisterRateThrottle(AnonRateThrottle):
    """Limita registros y reenvíos de email de activación por IP."""
    scope = 'register'


class TwoFAVerifyRateThrottle(UserRateThrottle):
    """Limita verificación de códigos TOTP por usuario (anti fuerza bruta del código)."""
    scope = 'twofa'
