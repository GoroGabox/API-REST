"""Planes de estudio por escuela (director) y costo de mantención (admin).

Director (con escuela): adapta los cursos de la plataforma al plan de estudio de
su escuela. El admin opera sobre cualquier escuela con ``?escuela=<id>``.
Lógica en ``schools.plan_estudio``.
"""
import csv
import hashlib
import os
from datetime import date

from django.db.models import Count
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import permissions as drf_permissions
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin, is_admin, is_director

from . import plan_estudio as pe
from .models import (
    CambioPlanEstudio, CostoMantencion, Curso, Escuela, LeccionRecurso, PlanEstudio,
)
from .serializers import LeccionDetalleSerializer


def _error(exc: pe.PlanError):
    data = {'error': str(exc), 'code': exc.codigo}
    if exc.op_indice is not None:
        data['op_indice'] = exc.op_indice
    return Response(data, status=status.HTTP_400_BAD_REQUEST)


def _plan_dict(plan, *, con_estructura=False):
    data = {
        'id': plan.id, 'escuela': plan.escuela_id, 'curso': plan.curso_id,
        'curso_nombre': plan.curso.nombre, 'curso_slug': plan.curso.slug,
        'nombre': plan.nombre, 'codigo_resolucion': plan.codigo_resolucion,
        'estado': plan.estado, 'activado_en': plan.activado_en,
        'creado_en': plan.creado_en, 'actualizado_en': plan.actualizado_en,
    }
    if con_estructura:
        est = pe.estructura_plan(plan)
        data['novedades'] = sum(1 for u in est for lp in u.lecciones if lp.nueva_base)
        data['unidades'] = pe.serializar_estructura(est, plan.curso_id, editor=True)
    return data


class _PlanBase(APIView):
    permission_classes = [drf_permissions.IsAuthenticated]

    def escuela_id(self, request):
        """Escuela sobre la que opera: la del director, o ``?escuela=`` para el admin."""
        user = request.user
        if is_admin(user):
            valor = request.query_params.get('escuela')
            if valor is None and isinstance(request.data, dict):
                valor = request.data.get('escuela')
            try:
                return int(valor), None
            except (TypeError, ValueError):
                return None, Response({'error': 'Indica la escuela (?escuela=<id>).'},
                                      status=status.HTTP_400_BAD_REQUEST)
        if is_director(user) and user.escuela_id:
            return user.escuela_id, None
        return None, Response({'error': 'Solo un director con escuela.'}, status=status.HTTP_403_FORBIDDEN)

    def plan(self, request, plan_id):
        escuela_id, err = self.escuela_id(request)
        if err:
            return None, err
        plan = PlanEstudio.objects.select_related('curso').filter(pk=plan_id, escuela_id=escuela_id).first()
        if plan is None:
            return None, Response({'error': 'Plan inexistente.'}, status=status.HTTP_404_NOT_FOUND)
        return plan, None


class PlanesEstudioView(_PlanBase):
    """GET: cursos de la plataforma con el estado del plan de la escuela.
    POST {curso, nombre?, codigo_resolucion?}: crea el plan (copia la disposición del curso base)."""

    def get(self, request):
        escuela_id, err = self.escuela_id(request)
        if err:
            return err
        planes = {p.curso_id: p for p in PlanEstudio.objects.select_related('curso').filter(escuela_id=escuela_id)}
        cursos = Curso.objects.annotate(n=Count('leccion')).order_by('id')
        out = []
        for c in cursos:
            p = planes.get(c.id)
            fila = {'curso': c.id, 'curso_nombre': c.nombre, 'curso_slug': c.slug,
                    'codigo': c.codigo, 'plan': None}
            if p is not None:
                fila['plan'] = {
                    'id': p.id, 'nombre': p.nombre, 'estado': p.estado,
                    'codigo_resolucion': p.codigo_resolucion, 'actualizado_en': p.actualizado_en,
                    'novedades': pe.novedades_pendientes(p),
                }
            out.append(fila)
        return Response(out)

    def post(self, request):
        escuela_id, err = self.escuela_id(request)
        if err:
            return err
        curso_ref = str(request.data.get('curso') or '').strip()
        curso = (Curso.objects.filter(pk=int(curso_ref)).first() if curso_ref.isdigit()
                 else Curso.objects.filter(slug=curso_ref).first())
        if curso is None:
            return Response({'error': 'Curso inexistente.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            plan = pe.crear_plan(
                escuela_id, curso, usuario=request.user,
                nombre=(request.data.get('nombre') or '').strip()[:150],
                codigo_resolucion=(request.data.get('codigo_resolucion') or '').strip()[:100],
            )
        except pe.PlanError as exc:
            resp = _error(exc)
            resp.status_code = status.HTTP_409_CONFLICT if exc.codigo == 'plan_existente' else 400
            return resp
        return Response(_plan_dict(plan, con_estructura=True), status=status.HTTP_201_CREATED)


class PlanEstudioDetalleView(_PlanBase):
    """GET: plan con su estructura editable (ocultas y novedades del base).
    PATCH {nombre?, codigo_resolucion?}."""

    def get(self, request, plan_id):
        plan, err = self.plan(request, plan_id)
        if err:
            return err
        return Response(_plan_dict(plan, con_estructura=True))

    def patch(self, request, plan_id):
        plan, err = self.plan(request, plan_id)
        if err:
            return err
        campos = {}
        if 'nombre' in request.data:
            campos['nombre'] = (request.data.get('nombre') or '').strip()[:150]
        if 'codigo_resolucion' in request.data:
            campos['codigo_resolucion'] = (request.data.get('codigo_resolucion') or '').strip()[:100]
        pe.editar_datos_plan(plan, usuario=request.user, **campos)
        return Response(_plan_dict(plan, con_estructura=True))


class PlanEstudioEstadoView(_PlanBase):
    """POST .../activar/ o .../desactivar/."""
    accion = 'activar'

    def post(self, request, plan_id):
        plan, err = self.plan(request, plan_id)
        if err:
            return err
        try:
            if self.accion == 'activar':
                pe.activar_plan(plan, usuario=request.user)
            else:
                pe.desactivar_plan(plan, usuario=request.user)
        except pe.PlanError as exc:
            return _error(exc)
        return Response(_plan_dict(plan, con_estructura=True))


class PlanEstudioOpsView(_PlanBase):
    """POST {ops: [{op, ...}]} — atómico. Ops: reordenar_unidades {unidades},
    reordenar_lecciones {unidad, lecciones}, mover_leccion {leccion, unidad, posicion?},
    ocultar/mostrar {leccion|unidad}, renombrar {leccion|unidad, nombre, descripcion?},
    agregar_unidad {nombre, descripcion?, posicion?}, quitar_unidad {unidad},
    aceptar_novedad {leccion}. ``unidad`` = ``plan_unidad_id``."""

    def post(self, request, plan_id):
        plan, err = self.plan(request, plan_id)
        if err:
            return err
        try:
            pe.aplicar_ops(plan, request.data.get('ops'), usuario=request.user)
        except pe.PlanError as exc:
            return _error(exc)
        plan.refresh_from_db()
        return Response(_plan_dict(plan, con_estructura=True))


def _tiene_avance(leccion_id):
    from accounts.models import EstudianteLeccion
    return EstudianteLeccion.objects.filter(leccion_id=leccion_id).exists()


class PlanLeccionesPropiasView(_PlanBase):
    """POST {unidad, nombre, contenido?, descripcion?, duracion_min?, posicion?}: lección propia.
    GET/PATCH/DELETE .../lecciones/<id>/ sobre una lección propia del plan."""
    parser_classes = [JSONParser]

    def post(self, request, plan_id, leccion_id=None):
        plan, err = self.plan(request, plan_id)
        if err:
            return err
        d = request.data
        try:
            lec = pe.crear_leccion_propia(
                plan, unidad=d.get('unidad'), nombre=d.get('nombre'), contenido=d.get('contenido') or '',
                descripcion=d.get('descripcion') or '', duracion_min=d.get('duracion_min') or 0,
                posicion=d.get('posicion'), usuario=request.user,
            )
        except pe.PlanError as exc:
            return _error(exc)
        return Response(LeccionDetalleSerializer(lec).data, status=status.HTTP_201_CREATED)

    def get(self, request, plan_id, leccion_id=None):
        plan, err = self.plan(request, plan_id)
        if err:
            return err
        try:
            lec = pe.leccion_propia(plan, leccion_id)
        except pe.PlanError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_404_NOT_FOUND)
        return Response(LeccionDetalleSerializer(lec).data)

    def patch(self, request, plan_id, leccion_id=None):
        plan, err = self.plan(request, plan_id)
        if err:
            return err
        campos = {k: request.data.get(k) for k in ('nombre', 'contenido', 'descripcion', 'duracion_min')
                  if k in request.data}
        try:
            lec = pe.editar_leccion_propia(plan, leccion_id, usuario=request.user, **campos)
        except pe.PlanError as exc:
            return _error(exc)
        return Response(LeccionDetalleSerializer(lec).data)

    def delete(self, request, plan_id, leccion_id=None):
        plan, err = self.plan(request, plan_id)
        if err:
            return err
        try:
            resultado = pe.eliminar_leccion_propia(plan, leccion_id, usuario=request.user,
                                                   tiene_avance=_tiene_avance(leccion_id))
        except pe.PlanError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_404_NOT_FOUND)
        return Response({'resultado': resultado})


_EXT_TIPO = {
    '.png': 'imagen', '.jpg': 'imagen', '.jpeg': 'imagen', '.webp': 'imagen', '.gif': 'imagen',
    '.pdf': 'pdf',
}


class PlanLeccionRecursosView(_PlanBase):
    """POST: recurso de una lección propia — multipart ``archivo`` (imagen/pdf) o JSON
    {tipo: imagen|pdf|video|audio, url, titulo?}. DELETE .../recursos/<id>/."""
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, plan_id, leccion_id, recurso_id=None):
        plan, err = self.plan(request, plan_id)
        if err:
            return err
        try:
            lec = pe.leccion_propia(plan, leccion_id)
        except pe.PlanError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_404_NOT_FOUND)
        archivo = request.FILES.get('archivo')
        titulo = (request.data.get('titulo') or '').strip()[:255]
        meta = {}
        if archivo is not None:
            ext = os.path.splitext(archivo.name or '')[1].lower()
            tipo = _EXT_TIPO.get(ext)
            if tipo is None:
                return Response({'error': 'Formato no admitido (imagen png/jpg/webp/gif o pdf).'},
                                status=status.HTTP_400_BAD_REQUEST)
            if archivo.size > pe.MAX_BYTES_RECURSO:
                return Response({'error': f'Archivo sobre {pe.MAX_BYTES_RECURSO // (1024 * 1024)} MB.'},
                                status=status.HTTP_400_BAD_REQUEST)
            data = archivo.read()
            from content_pipeline.media.storage import StorageError, get_storage
            nombre = f"escuelas/{plan.escuela_id}/{hashlib.sha1(data).hexdigest()[:16]}{ext}"
            try:
                storage = get_storage(kind='images' if tipo == 'imagen' else 'docs')
                url = storage.url(nombre) if storage.exists(nombre) else storage.save(nombre, data)
            except StorageError as exc:
                return Response({'error': f'No se pudo guardar el archivo: {exc}'},
                                status=status.HTTP_502_BAD_GATEWAY)
            meta = {'bytes': len(data), 'archivo': archivo.name}
        else:
            tipo = request.data.get('tipo')
            url = (request.data.get('url') or '').strip()
            if tipo not in ('imagen', 'pdf', 'video', 'audio') or not url.startswith(('http://', 'https://')):
                return Response({'error': 'Indica «archivo», o «tipo» (imagen|pdf|video|audio) y una «url» http(s).'},
                                status=status.HTTP_400_BAD_REQUEST)
        rol = {'imagen': 'figura', 'pdf': 'descarga'}.get(tipo, 'principal')
        n = lec.recursos.count() + 1
        clave = f"{tipo}-{n}"
        while lec.recursos.filter(clave=clave).exists():
            n += 1
            clave = f"{tipo}-{n}"
        rec = LeccionRecurso.objects.create(leccion=lec, tipo=tipo, rol=rol, clave=clave, url=url,
                                            orden=n, titulo=titulo, meta=meta)
        pe.registrar_cambio(plan, 'agregar_recurso', usuario=request.user, objeto_tipo='recurso',
                            objeto_id=rec.id, despues={'leccion': lec.id, 'tipo': tipo, 'clave': clave, **meta})
        return Response({'id': rec.id, 'tipo': rec.tipo, 'rol': rec.rol, 'clave': rec.clave, 'url': rec.url,
                         'titulo': rec.titulo, 'meta': rec.meta,
                         'marcador': '{{figura:%s}}' % clave if tipo == 'imagen' else None},
                        status=status.HTTP_201_CREATED)

    def delete(self, request, plan_id, leccion_id, recurso_id=None):
        plan, err = self.plan(request, plan_id)
        if err:
            return err
        try:
            lec = pe.leccion_propia(plan, leccion_id)
        except pe.PlanError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_404_NOT_FOUND)
        rec = lec.recursos.filter(pk=recurso_id).first()
        if rec is None:
            return Response({'error': 'Recurso inexistente.'}, status=status.HTTP_404_NOT_FOUND)
        antes = {'leccion': lec.id, 'tipo': rec.tipo, 'clave': rec.clave}
        rid = rec.id
        rec.delete()
        pe.registrar_cambio(plan, 'quitar_recurso', usuario=request.user, objeto_tipo='recurso',
                            objeto_id=rid, antes=antes)
        return Response(status=status.HTTP_204_NO_CONTENT)


class _Paginacion(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 500


def _cambio_dict(c):
    return {
        'id': c.id, 'plan': c.plan_id, 'escuela': c.escuela_id, 'curso': c.curso_id, 'tipo': c.tipo,
        'tipo_nombre': c.get_tipo_display(), 'origen': c.origen, 'objeto_tipo': c.objeto_tipo,
        'objeto_id': c.objeto_id, 'antes': c.antes, 'despues': c.despues, 'caracteres': c.caracteres,
        'puntos': float(c.puntos), 'usuario': c.usuario_id,
        'usuario_email': getattr(c.usuario, 'email', None), 'fecha': c.fecha,
    }


class PlanEstudioHistorialView(_PlanBase):
    """GET: cambios del plan (paginado)."""

    def get(self, request, plan_id):
        plan, err = self.plan(request, plan_id)
        if err:
            return err
        qs = CambioPlanEstudio.objects.filter(plan=plan).select_related('usuario')
        pag = _Paginacion()
        page = pag.paginate_queryset(qs, request, view=self)
        return pag.get_paginated_response([_cambio_dict(c) for c in page])


# ---------------------------------------------------------------------------
# Admin: costo de mantención por escuela
# ---------------------------------------------------------------------------

def _fecha(valor):
    try:
        return date.fromisoformat(valor) if valor else None
    except ValueError:
        return None


class MantencionView(APIView):
    """GET schools/admin/mantencion/?desde&hasta[&formato=csv] — una fila por escuela."""
    permission_classes = [IsAdmin]

    def get(self, request):
        desde, hasta = _fecha(request.query_params.get('desde')), _fecha(request.query_params.get('hasta'))
        filas = pe.costo_escuelas(desde=desde, hasta=hasta)
        if request.query_params.get('formato') == 'csv':
            resp = HttpResponse(content_type='text/csv; charset=utf-8')
            resp['Content-Disposition'] = 'attachment; filename="mantencion_planes.csv"'
            w = csv.writer(resp)
            w.writerow(['escuela_id', 'escuela', 'planes', 'planes_activos', 'lecciones_propias', 'cambios',
                        'puntos', 'caracteres', 'horas_staff', 'monto_clp'])
            for f in filas:
                w.writerow([f['escuela_id'], f['escuela_nombre'], f['planes'], f['planes_activos'],
                            f['lecciones_propias'], f['cambios'], f['puntos'], f['caracteres'],
                            f['horas_staff'], f['monto_clp']])
            return resp
        return Response({'desde': desde, 'hasta': hasta, 'pesos': pe.PESOS_CAMBIO,
                         'puntos_por_mil_caracteres': pe.PUNTOS_POR_MIL_CARACTERES, 'escuelas': filas})


def _costo_dict(c):
    return {'id': c.id, 'escuela': c.escuela_id, 'plan': c.plan_id, 'cambio': c.cambio_id, 'tipo': c.tipo,
            'minutos': c.minutos, 'monto_clp': c.monto_clp, 'descripcion': c.descripcion,
            'usuario': c.usuario_id, 'fecha': c.fecha}


class MantencionEscuelaView(APIView):
    """GET schools/admin/mantencion/<escuela>/ — resumen, planes, costos e historial (paginado)."""
    permission_classes = [IsAdmin]

    def get(self, request, escuela_id):
        escuela = Escuela.objects.filter(pk=escuela_id).first()
        if escuela is None:
            return Response({'error': 'Escuela inexistente.'}, status=status.HTTP_404_NOT_FOUND)
        desde, hasta = _fecha(request.query_params.get('desde')), _fecha(request.query_params.get('hasta'))
        resumen = pe.costo_escuelas(desde=desde, hasta=hasta, escuela_id=escuela.id)
        planes = [_plan_dict(p) | {'novedades': pe.novedades_pendientes(p)}
                  for p in PlanEstudio.objects.select_related('curso').filter(escuela=escuela)]
        costos = pe._rango(CostoMantencion.objects.filter(escuela=escuela), 'fecha', desde, hasta)
        cambios = pe._rango(CambioPlanEstudio.objects.filter(escuela=escuela).select_related('usuario'),
                            'fecha', desde, hasta)
        pag = _Paginacion()
        page = pag.paginate_queryset(cambios, request, view=self)
        resp = pag.get_paginated_response([_cambio_dict(c) for c in page])
        resp.data = {
            'escuela': {'id': escuela.id, 'nombre': escuela.nombre},
            'resumen': resumen[0] if resumen else None,
            'planes': planes,
            'costos': [_costo_dict(c) for c in costos],
            'historial': resp.data,
        }
        return resp


class MantencionCostosView(APIView):
    """POST schools/admin/mantencion/<escuela>/costos/ {tipo, minutos?, monto_clp?, descripcion?,
    plan?, cambio?, fecha?}; DELETE .../costos/<id>/."""
    permission_classes = [IsAdmin]

    def post(self, request, escuela_id, costo_id=None):
        escuela = Escuela.objects.filter(pk=escuela_id).first()
        if escuela is None:
            return Response({'error': 'Escuela inexistente.'}, status=status.HTTP_404_NOT_FOUND)
        d = request.data
        tipos = dict(CostoMantencion.TIPO_CHOICES)
        if d.get('tipo') not in tipos:
            return Response({'error': f'tipo debe ser uno de {", ".join(tipos)}.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            minutos = int(d.get('minutos') or 0)
            monto = int(d.get('monto_clp') or 0)
        except (TypeError, ValueError):
            return Response({'error': 'minutos y monto_clp deben ser enteros.'}, status=status.HTTP_400_BAD_REQUEST)
        if minutos < 0 or monto < 0 or (minutos == 0 and monto == 0):
            return Response({'error': 'Indica minutos y/o monto_clp positivos.'}, status=status.HTTP_400_BAD_REQUEST)
        plan = PlanEstudio.objects.filter(pk=d.get('plan'), escuela=escuela).first() if d.get('plan') else None
        cambio = (CambioPlanEstudio.objects.filter(pk=d.get('cambio'), escuela=escuela).first()
                  if d.get('cambio') else None)
        fecha = timezone.now()
        f = _fecha(d.get('fecha'))
        if f is not None:
            fecha = timezone.make_aware(timezone.datetime(f.year, f.month, f.day, 12))
        c = CostoMantencion.objects.create(
            escuela=escuela, plan=plan or (cambio.plan if cambio else None), cambio=cambio, tipo=d['tipo'],
            minutos=minutos, monto_clp=monto, descripcion=(d.get('descripcion') or '')[:255],
            usuario=request.user, fecha=fecha,
        )
        return Response(_costo_dict(c), status=status.HTTP_201_CREATED)

    def delete(self, request, escuela_id, costo_id=None):
        borrados, _ = CostoMantencion.objects.filter(pk=costo_id, escuela_id=escuela_id).delete()
        if not borrados:
            return Response({'error': 'Costo inexistente.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)
