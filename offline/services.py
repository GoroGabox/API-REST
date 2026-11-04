"""Reglas del modo offline: emisión/renovación de licencias y manifiesto de descarga."""
from __future__ import annotations

import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from content_pipeline.media.storage import url_firmada
from sales.services import vencimiento_acceso
from schools.models import Curso, Leccion, Unidad
from schools.serializers import CursoSerializer, LeccionDetalleSerializer, UnidadSerializer

from . import crypto
from .models import DispositivoOffline, LicenciaOffline, _sal

# El estudiante debe conectarse al menos cada OFFLINE_DIAS para seguir estudiando offline.
OFFLINE_DIAS = 7
# Máximo de dispositivos con el mismo curso descargado a la vez.
MAX_DISPOSITIVOS_POR_CURSO = 2
# Una licencia sin renovar en este plazo deja de ocupar cupo (app desinstalada, equipo perdido).
CUPO_INACTIVO_DIAS = 30
# Vida de las URLs firmadas del manifiesto.
URL_EXPIRA_SEG = 15 * 60
# Tipos que se descargan; el video queda solo en línea.
TIPOS_DESCARGABLES = ('imagen', 'audio', 'pdf')
PLACEHOLDER = 'http://placeholder.url'
LICENCIA_VERSION = 1


class OfflineError(Exception):
    def __init__(self, message, code, status=400, extra=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra or {}


def _iso(dt):
    return dt.isoformat().replace('+00:00', 'Z') if dt else None


def es_uuid(valor) -> bool:
    try:
        uuid.UUID(str(valor))
        return True
    except (TypeError, ValueError, AttributeError):
        return False


def calcular_offline_hasta(valid_until, ahora):
    tope = ahora + timedelta(days=OFFLINE_DIAS)
    return min(tope, valid_until) if valid_until else tope


def payload_licencia(lic: LicenciaOffline, valid_until, ahora) -> dict:
    return {
        'v': LICENCIA_VERSION,
        'lic': str(lic.id),
        'usuario': lic.dispositivo.usuario_id,
        'dispositivo': str(lic.dispositivo.device_id),
        'curso': lic.curso_id,
        'acceso_hasta': _iso(valid_until),
        'offline_hasta': _iso(lic.offline_hasta),
        'emitida_en': _iso(lic.emitida_en),
        'server_now': _iso(ahora),
    }


def licencia_firmada(lic: LicenciaOffline, valid_until, ahora) -> dict:
    texto, firma = crypto.firmar(payload_licencia(lic, valid_until, ahora))
    return {'licencia': texto, 'firma': firma}


def licencias_ocupando(usuario, curso_id, ahora):
    """Licencias activas del curso que cuentan para el límite de dispositivos."""
    return (
        LicenciaOffline.objects
        .select_related('dispositivo')
        .filter(dispositivo__usuario=usuario, curso_id=curso_id, revocada=False,
                renovada_en__gte=ahora - timedelta(days=CUPO_INACTIVO_DIAS))
    )


def describir_dispositivo(d: DispositivoOffline) -> dict:
    return {
        'device_id': str(d.device_id), 'nombre': d.nombre, 'plataforma': d.plataforma,
        'ultimo_uso': _iso(d.ultimo_uso),
    }


def resolver_curso(curso_ref) -> Curso:
    curso = None
    if str(curso_ref).isdigit():
        curso = Curso.objects.filter(pk=int(curso_ref)).first()
    if curso is None and curso_ref:
        curso = Curso.objects.filter(slug=str(curso_ref)).first()
    if curso is None:
        raise OfflineError('Curso no existe.', 'curso_no_existe', 404)
    return curso


@transaction.atomic
def emitir_licencia(usuario, curso_ref, device_id, plataforma='', nombre=''):
    """Crea o renueva la licencia del (dispositivo, curso).

    Devuelve (licencia, valid_until, ahora). Raise OfflineError sin acceso (403) o
    si el curso ya está descargado en el máximo de dispositivos (409).
    """
    curso = resolver_curso(curso_ref)
    tiene, valid_until = vencimiento_acceso(usuario, curso.id)
    if not tiene:
        raise OfflineError('No tienes acceso vigente a este curso.', 'sin_acceso', 403)

    ahora = timezone.now()
    plataforma = (plataforma or '')[:20]
    nombre = (nombre or '')[:100]
    disp, _ = DispositivoOffline.objects.select_for_update().get_or_create(
        usuario=usuario, device_id=device_id,
        defaults={'plataforma': plataforma, 'nombre': nombre},
    )
    if plataforma:
        disp.plataforma = plataforma
    if nombre:
        disp.nombre = nombre
    disp.save()

    otros = licencias_ocupando(usuario, curso.id, ahora).exclude(dispositivo=disp)
    if otros.count() >= MAX_DISPOSITIVOS_POR_CURSO:
        raise OfflineError(
            f'Ya tienes este curso descargado en {MAX_DISPOSITIVOS_POR_CURSO} dispositivos.',
            'limite_dispositivos', 409,
            {'dispositivos': [describir_dispositivo(l.dispositivo) for l in otros]},
        )

    offline_hasta = calcular_offline_hasta(valid_until, ahora)
    lic = LicenciaOffline.objects.filter(dispositivo=disp, curso=curso).first()
    if lic is None:
        lic = LicenciaOffline.objects.create(
            dispositivo=disp, curso=curso, renovada_en=ahora, offline_hasta=offline_hasta,
        )
    else:
        if lic.revocada:
            # Re-descarga tras revocar: sal nueva → CEK nueva (lo anterior queda ilegible).
            lic.sal = _sal()
            lic.revocada = False
            lic.emitida_en = ahora
        lic.renovada_en = ahora
        lic.offline_hasta = offline_hasta
        lic.save()
    return lic, valid_until, ahora


def renovar_licencias(usuario, device_id, lic_ids):
    """Renueva licencias del dispositivo. Por cada id: estado y, si sigue vigente, licencia firmada.

    Estados: ``vigente`` | ``revocada`` (la liberaron) | ``sin_acceso`` (venció o se
    revocó el acceso: la licencia queda revocada) | ``desconocida``. Ante cualquier
    estado distinto de ``vigente`` la app debe borrar la CEK y los archivos.
    """
    ahora = timezone.now()
    resultados = []
    for lic_id in (lic_ids or [])[:50]:
        lic = None
        if es_uuid(lic_id):
            lic = (
                LicenciaOffline.objects.select_related('dispositivo')
                .filter(pk=lic_id, dispositivo__usuario=usuario, dispositivo__device_id=device_id)
                .first()
            )
        if lic is None:
            resultados.append({'lic': str(lic_id), 'estado': 'desconocida'})
            continue
        if lic.revocada:
            resultados.append({'lic': str(lic.id), 'estado': 'revocada'})
            continue
        tiene, valid_until = vencimiento_acceso(usuario, lic.curso_id)
        if not tiene:
            lic.revocada = True
            lic.save(update_fields=['revocada'])
            resultados.append({'lic': str(lic.id), 'estado': 'sin_acceso'})
            continue
        lic.renovada_en = ahora
        lic.offline_hasta = calcular_offline_hasta(valid_until, ahora)
        lic.save(update_fields=['renovada_en', 'offline_hasta'])
        DispositivoOffline.objects.filter(pk=lic.dispositivo_id).update(ultimo_uso=ahora)
        resultados.append({'lic': str(lic.id), 'estado': 'vigente', **licencia_firmada(lic, valid_until, ahora)})
    return resultados, ahora


def _url_real(url) -> bool:
    return bool(url) and url != PLACEHOLDER and url.startswith(('http://', 'https://'))


def manifiesto(curso: Curso, request=None) -> dict:
    """Todo lo que la app necesita para mostrar el curso sin conexión.

    ``curso``/``unidades``/``lecciones`` usan los mismos serializers que los endpoints
    en línea (la app reutiliza sus tipos). ``archivos`` lista los medios a descargar
    (imagen/audio/pdf; no video) con URL firmada de corta vida; ``id`` es estable y la
    app lo usa como nombre de archivo y como AAD del cifrado; ``url_original`` es la
    que aparece en ``lecciones`` (para reemplazarla por el archivo local).
    """
    ctx = {'request': request} if request else {}
    lecciones = list(
        Leccion.objects.filter(curso=curso)
        .select_related('unidad').prefetch_related('recursos')
        .order_by('unidad__orden', 'posicion', 'id')
    )
    unidades = Unidad.objects.filter(curso=curso).prefetch_related('lecciones').order_by('orden', 'id')

    archivos = []
    for lec in lecciones:
        tipos_con_recurso = set()
        for r in lec.recursos.all():
            tipos_con_recurso.add(r.tipo)
            if r.tipo not in TIPOS_DESCARGABLES or not _url_real(r.url):
                continue
            meta = r.meta or {}
            archivos.append({
                'id': f'r{r.id}', 'leccion': lec.id, 'recurso': r.id, 'clave': r.clave,
                'tipo': r.tipo, 'rol': r.rol, 'url_original': r.url,
                'url': url_firmada(r.url, expira_seg=URL_EXPIRA_SEG),
                'bytes': meta.get('bytes'), 'hash': meta.get('hash'),
            })
        # Espejo url_* sin recurso del mismo tipo (lecciones antiguas).
        for campo, tipo in (('url_audio', 'audio'), ('url_pdf', 'pdf')):
            url = getattr(lec, campo)
            if tipo in tipos_con_recurso or not _url_real(url):
                continue
            archivos.append({
                'id': f'l{lec.id}-{tipo}', 'leccion': lec.id, 'recurso': None, 'clave': campo,
                'tipo': tipo, 'rol': 'narracion' if tipo == 'audio' else 'descarga',
                'url_original': url, 'url': url_firmada(url, expira_seg=URL_EXPIRA_SEG),
                'bytes': None, 'hash': None,
            })

    return {
        'curso': CursoSerializer(curso, context=ctx).data,
        'unidades': UnidadSerializer(unidades, many=True, context=ctx).data,
        'lecciones': LeccionDetalleSerializer(lecciones, many=True, context=ctx).data,
        'archivos': archivos,
        'url_expira_seg': URL_EXPIRA_SEG,
        'version': _iso(timezone.now()),
    }
