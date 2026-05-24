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


def _dispatch_push(user, notif: Notificacion):
    """Envía push a los PushTokens activos del usuario.

    Hook a integrar con Expo Push API / FCM / APNS. Hoy solo loggea.
    Sustituir por requests.post('https://exp.host/--/api/v2/push/send', ...) etc.
    """
    tokens = list(PushToken.objects.filter(usuario=user).values_list('token', 'platform'))
    if not tokens:
        return
    logger.info(
        "push pendiente notif=%s usuario=%s tokens=%d titulo=%s",
        notif.id, user.id, len(tokens), notif.titulo,
    )
    # TODO integrar Expo Push:
    # for token, platform in tokens:
    #     requests.post('https://exp.host/--/api/v2/push/send',
    #                   json={'to': token, 'title': notif.titulo, 'body': notif.mensaje, 'data': notif.data})
