"""Verificación de tokens de OAuth (Google, Apple) y obtención/creación del usuario.

Diseño:
- Verifiers configurables vía `settings.SOCIAL_AUTH_VERIFIERS` para tests/mocks.
- En producción usa:
    - `google-auth` (`google.oauth2.id_token.verify_oauth2_token`)
    - Apple: verificación manual contra los JWKS públicos de Apple
      (https://appleid.apple.com/auth/keys). Hoy se implementa con `PyJWT`
      + `requests`; si no están disponibles, lanza NotImplementedError.
- Devuelve `{email, nombre, apellido, sub}` y el caller resuelve usuario.
"""
import json
import os
from typing import Optional

from django.conf import settings


class SocialAuthError(Exception):
    pass


# ---------------- Google ----------------

def verify_google_id_token(id_token: str) -> dict:
    """Devuelve `{email, nombre, apellido, sub}` o lanza SocialAuthError."""
    custom = getattr(settings, 'SOCIAL_AUTH_VERIFIERS', {}).get('google')
    if callable(custom):
        return custom(id_token)

    try:
        from google.oauth2 import id_token as google_id_token  # type: ignore
        from google.auth.transport import requests as google_requests  # type: ignore
    except ImportError:
        raise SocialAuthError(
            "google-auth no instalado. Instalar `google-auth` y configurar GOOGLE_OAUTH_CLIENT_ID."
        )

    client_id = os.getenv('GOOGLE_OAUTH_CLIENT_ID')
    if not client_id:
        raise SocialAuthError("GOOGLE_OAUTH_CLIENT_ID no configurado.")

    try:
        payload = google_id_token.verify_oauth2_token(
            id_token, google_requests.Request(), client_id,
        )
    except ValueError as e:
        raise SocialAuthError(f"id_token inválido: {e}")

    email = payload.get('email')
    if not email or not payload.get('email_verified'):
        raise SocialAuthError("Email no verificado por Google.")

    return {
        'email': email,
        'nombre': payload.get('given_name', ''),
        'apellido': payload.get('family_name', ''),
        'sub': payload.get('sub'),
    }


# ---------------- Apple ----------------

def verify_apple_identity_token(identity_token: str) -> dict:
    custom = getattr(settings, 'SOCIAL_AUTH_VERIFIERS', {}).get('apple')
    if callable(custom):
        return custom(identity_token)

    try:
        import jwt  # type: ignore
        import requests
    except ImportError:
        raise SocialAuthError(
            "PyJWT + requests requeridos para validar Apple identity tokens."
        )

    bundle = os.getenv('APPLE_BUNDLE_ID')  # iOS app
    services_id = os.getenv('APPLE_SERVICES_ID')  # Sign in with Apple JS
    audiences = [a for a in (bundle, services_id) if a]
    if not audiences:
        raise SocialAuthError("APPLE_BUNDLE_ID o APPLE_SERVICES_ID requerido.")

    try:
        headers = jwt.get_unverified_header(identity_token)
        kid = headers['kid']
        jwks = requests.get('https://appleid.apple.com/auth/keys', timeout=5).json()
        key_jwk = next((k for k in jwks['keys'] if k['kid'] == kid), None)
        if key_jwk is None:
            raise SocialAuthError("Key id no encontrado en JWKS de Apple.")
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key_jwk))
        payload = jwt.decode(
            identity_token, public_key, algorithms=['RS256'],
            audience=audiences, issuer='https://appleid.apple.com',
        )
    except Exception as e:
        raise SocialAuthError(f"identity_token inválido: {e}")

    email = payload.get('email')
    if not email:
        raise SocialAuthError("Apple no devolvió email.")
    return {
        'email': email,
        'nombre': payload.get('given_name', ''),
        'apellido': payload.get('family_name', ''),
        'sub': payload.get('sub'),
    }


# ---------------- Login orquestación ----------------

def obtener_o_crear_usuario_social(payload: dict):
    """Busca por email; si no existe, crea estudiante activo (verificado por OAuth)."""
    from .models import Usuario
    email = payload['email'].lower().strip()
    user = Usuario.objects.filter(email__iexact=email).first()
    if user is not None:
        return user, False

    user = Usuario.objects.create_user(
        email=email,
        nombre=payload.get('nombre') or 'Usuario',
        apellido=payload.get('apellido') or '',
        password=Usuario.objects.make_random_password() if hasattr(Usuario.objects, 'make_random_password') else _random_password(),
        is_estudiante=True,
    )
    user.is_active = True  # email validado por el IdP
    user.activation_token = None
    user.save(update_fields=['is_active', 'activation_token'])
    return user, True


def _random_password() -> str:
    import secrets
    return secrets.token_urlsafe(24)


def emitir_jwt_para_usuario(user) -> dict:
    """Devuelve par {access, refresh} usando MyTokenObtainPairSerializer."""
    from .serializers import MyTokenObtainPairSerializer
    refresh = MyTokenObtainPairSerializer.get_token(user)
    return {'refresh': str(refresh), 'access': str(refresh.access_token)}
