"""Endpoints del modo offline (app móvil, solo estudiantes).

    POST   offline/licencias/                  {curso, device_id, plataforma?, nombre?}
           → 201 {licencia, firma, cek, manifest}  | 403 sin_acceso | 409 limite_dispositivos
    POST   offline/licencias/renovar/          {device_id, licencias: [lic]}
           → {resultados: [{lic, estado, licencia?, firma?}], server_now}
    GET    offline/licencias/<lic>/manifest/?device_id=
           → manifiesto con URLs firmadas nuevas (actualizar / reanudar descarga)
    DELETE offline/licencias/<lic>/?device_id= → libera el cupo (eliminar descarga)
    GET    offline/dispositivos/               → dispositivos del usuario y sus cursos
    DELETE offline/dispositivos/<device_id>/   → revoca todas sus licencias
    GET    offline/llave-publica/              → llave pública Ed25519 (público)
"""
import base64
from datetime import timedelta

from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import is_estudiante

from . import crypto, services
from .models import DispositivoOffline, LicenciaOffline


class EsEstudiante(permissions.BasePermission):
    message = 'Solo estudiantes pueden descargar cursos.'

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and is_estudiante(request.user))


def _error(exc: services.OfflineError):
    return Response({'detail': str(exc), 'codigo': exc.code, **exc.extra}, status=exc.status)


def _device_id(valor):
    if not services.es_uuid(valor):
        raise services.OfflineError('device_id inválido.', 'device_id_invalido', 400)
    return str(valor)


class LicenciaListView(APIView):
    permission_classes = [EsEstudiante]

    def post(self, request):
        data = request.data
        try:
            device_id = _device_id(data.get('device_id'))
            lic, valid_until, ahora = services.emitir_licencia(
                request.user, data.get('curso'), device_id,
                plataforma=str(data.get('plataforma') or ''), nombre=str(data.get('nombre') or ''),
            )
        except services.OfflineError as exc:
            return _error(exc)
        return Response({
            **services.licencia_firmada(lic, valid_until, ahora),
            'cek': base64.b64encode(crypto.cek(lic)).decode(),
            'manifest': services.manifiesto(lic.curso, request),
        }, status=status.HTTP_201_CREATED)


class LicenciaRenovarView(APIView):
    permission_classes = [EsEstudiante]

    def post(self, request):
        try:
            device_id = _device_id(request.data.get('device_id'))
        except services.OfflineError as exc:
            return _error(exc)
        lic_ids = request.data.get('licencias') or []
        if not isinstance(lic_ids, list):
            return Response({'detail': 'licencias debe ser una lista.'}, status=status.HTTP_400_BAD_REQUEST)
        resultados, ahora = services.renovar_licencias(request.user, device_id, lic_ids)
        return Response({'resultados': resultados, 'server_now': services._iso(ahora)})


def _licencia_propia(request, lic_id):
    device_id = _device_id(request.query_params.get('device_id'))
    if not services.es_uuid(lic_id):
        raise services.OfflineError('Licencia no encontrada.', 'licencia_no_existe', 404)
    lic = (
        LicenciaOffline.objects.select_related('dispositivo', 'curso')
        .filter(pk=lic_id, dispositivo__usuario=request.user, dispositivo__device_id=device_id)
        .first()
    )
    if lic is None:
        raise services.OfflineError('Licencia no encontrada.', 'licencia_no_existe', 404)
    return lic


class LicenciaDetalleView(APIView):
    permission_classes = [EsEstudiante]

    def delete(self, request, lic_id):
        try:
            lic = _licencia_propia(request, lic_id)
        except services.OfflineError as exc:
            return _error(exc)
        lic.revocada = True
        lic.save(update_fields=['revocada'])
        return Response(status=status.HTTP_204_NO_CONTENT)


class LicenciaManifestView(APIView):
    permission_classes = [EsEstudiante]

    def get(self, request, lic_id):
        try:
            lic = _licencia_propia(request, lic_id)
            if lic.revocada:
                raise services.OfflineError('La descarga fue eliminada.', 'revocada', 410)
            tiene, _ = services.vencimiento_acceso(request.user, lic.curso_id)
            if not tiene:
                raise services.OfflineError('No tienes acceso vigente a este curso.', 'sin_acceso', 403)
        except services.OfflineError as exc:
            return _error(exc)
        return Response(services.manifiesto(lic.curso, request))


class DispositivoListView(APIView):
    permission_classes = [EsEstudiante]

    def get(self, request):
        ahora = timezone.now()
        dispositivos = DispositivoOffline.objects.filter(usuario=request.user).prefetch_related('licencias__curso')
        out = []
        for d in dispositivos:
            cursos = [
                {'lic': str(l.id), 'curso_id': l.curso_id, 'curso_nombre': l.curso.nombre,
                 'offline_hasta': services._iso(l.offline_hasta)}
                for l in d.licencias.all()
                if not l.revocada
                and l.renovada_en >= ahora - timedelta(days=services.CUPO_INACTIVO_DIAS)
            ]
            if cursos:
                out.append({**services.describir_dispositivo(d), 'cursos': cursos})
        return Response({'dispositivos': out, 'max_por_curso': services.MAX_DISPOSITIVOS_POR_CURSO})


class DispositivoDetalleView(APIView):
    permission_classes = [EsEstudiante]

    def delete(self, request, device_id):
        if not services.es_uuid(device_id):
            return Response({'detail': 'device_id inválido.'}, status=status.HTTP_400_BAD_REQUEST)
        d = DispositivoOffline.objects.filter(usuario=request.user, device_id=device_id).first()
        if d is None:
            return Response({'detail': 'Dispositivo no encontrado.'}, status=status.HTTP_404_NOT_FOUND)
        d.licencias.update(revocada=True)
        return Response(status=status.HTTP_204_NO_CONTENT)


class LlavePublicaView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request):
        return Response({'alg': 'Ed25519', 'llave_publica': crypto.llave_publica_b64(),
                         'version': services.LICENCIA_VERSION})
