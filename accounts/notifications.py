"""Servicios de notificaciones in-app + push.

API:
    notificar(user, tipo, titulo, mensaje, data=None) -> Notificacion

El envío a Expo/FCM se delega al hook `_dispatch_push` que es no-op por defecto
y se debe reemplazar/extender cuando se cablee el SDK de push real.
"""
import logging
from typing import Optional

from .models import Notificacion, PushToken

logger = logging.getLogger(__name__)


def notificar(user, tipo: str, titulo: str, mensaje: str = '', data: Optional[dict] = None) -> Notificacion:
    """Crea registro in-app y dispara push a todos los tokens del usuario."""
    notif = Notificacion.objects.create(
        usuario=user, tipo=tipo, titulo=titulo, mensaje=mensaje, data=data or {},
    )
    _dispatch_push(user, notif)
    return notif


EXPO_PUSH_URL = 'https://exp.host/--/api/v2/push/send'


def _dispatch_push(user, notif: Notificacion):
    """Envía push a los PushTokens activos del usuario.

    Habilita el envío real con `EXPO_PUSH_ENABLED=True` (settings o env).
    En desarrollo / tests permanece desactivado para no salir a la red.
    """
    from django.conf import settings as _settings
    import os as _os

    tokens = list(PushToken.objects.filter(usuario=user).values_list('token', 'platform'))
    if not tokens:
        return

    enabled = bool(
        getattr(_settings, 'EXPO_PUSH_ENABLED', None)
        or _os.getenv('EXPO_PUSH_ENABLED', '').lower() in ('1', 'true', 'yes')
    )
    if not enabled:
        logger.info(
            "push deshabilitado notif=%s usuario=%s tokens=%d titulo=%s",
            notif.id, user.id, len(tokens), notif.titulo,
        )
        return

    try:
        import requests
    except ImportError:
        logger.warning("requests no disponible; push omitido.")
        return

    payloads = [
        {
            'to': token, 'title': notif.titulo, 'body': notif.mensaje,
            'data': {**(notif.data or {}), 'tipo': notif.tipo},
            'sound': 'default',
        }
        for token, _ in tokens
    ]
    try:
        resp = requests.post(EXPO_PUSH_URL, json=payloads, timeout=5,
                             headers={'Accept': 'application/json', 'Accept-encoding': 'gzip, deflate',
                                      'Content-Type': 'application/json'})
        if resp.status_code >= 400:
            logger.warning("Expo Push %s -> %s", resp.status_code, resp.text[:300])
    except Exception as e:
        logger.warning("Expo Push fallo: %s", e)
