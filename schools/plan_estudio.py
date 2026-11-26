"""Planes de estudio por escuela: un curso de la plataforma adaptado al plan legal
de una escuela.

Modelo **overlay**: el plan (``PlanEstudio`` + ``PlanUnidad`` + ``PlanLeccion``)
solo guarda la disposición —orden, ocultas, nombres propios— apuntando a las
unidades/lecciones del curso base, más las lecciones propias de la escuela
(``Leccion.escuela``). El contenido del base (texto, audio, figuras) se hereda:
una corrección del base llega sola a todas las escuelas.

- Lecciones del base que el plan no referencia (agregadas después de crear el
  plan) se muestran al final de su unidad como ``nueva_base`` hasta que el
  director las acepta (``aceptar_novedad``) u oculta.
- El alumno ve el plan ACTIVO de su escuela (``Usuario.escuela``); sin escuela o
  sin plan activo, el curso base.
- Cada modificación queda en ``CambioPlanEstudio`` con un costo ponderado
  (``PESOS_CAMBIO``) para medir cuánto cuesta mantener los cursos de cada escuela.

Este módulo es la fuente única de "qué lecciones tiene un curso para X": todo
conteo de avance/elegibilidad debe pasar por ``lecciones_visibles_ids``.
No importa ``accounts`` ni ``sales`` (``schools`` es la app de más bajo nivel).
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Max, Q, Sum, Count
from django.utils import timezone

from .models import (
    CambioPlanEstudio, CostoMantencion, Curso, Leccion, PlanEstudio, PlanLeccion, PlanUnidad, Unidad,
)

# ---------------------------------------------------------------------------
# Costo ponderado por tipo de cambio (se congela en CambioPlanEstudio.puntos)
# ---------------------------------------------------------------------------
PESOS_CAMBIO = {
    'crear_plan': 0,
    'editar_plan': 0,
    'activar': 0,
    'desactivar': 0,
    'reordenar_unidades': 1,
    'reordenar_lecciones': 1,
    'mover_leccion': 1,
    'ocultar': 1,
    'mostrar': 1,
    'renombrar': 1,
    'agregar_unidad': 2,
    'quitar_unidad': 1,
    'agregar_leccion': 5,
    'editar_leccion': 3,
    'eliminar_leccion': 1,
    'agregar_recurso': 2,
    'quitar_recurso': 2,
    'aceptar_novedad': 1,
    'base_eliminada': 1,
}
# Puntos extra por cada 1.000 caracteres de texto propio agregado/cambiado.
PUNTOS_POR_MIL_CARACTERES = 1
# Ediciones del mismo objeto por el mismo usuario dentro de esta ventana se
# acumulan en un solo evento (escribir una lección no son 40 cambios).
VENTANA_COALESCER = timedelta(minutes=10)
TIPOS_COALESCIBLES = {'editar_leccion', 'renombrar', 'editar_plan'}

# Límites de lecciones propias.
MAX_CARACTERES_LECCION = 60_000
MAX_BYTES_RECURSO = 15 * 1024 * 1024


class PlanError(ValueError):
    """Operación inválida sobre un plan (la vista responde 400 con el mensaje)."""

    def __init__(self, mensaje, *, codigo='invalido', op_indice=None):
        super().__init__(mensaje)
        self.codigo = codigo
        self.op_indice = op_indice


# ---------------------------------------------------------------------------
# Registro de cambios
# ---------------------------------------------------------------------------

def puntos_para(tipo: str, caracteres: int = 0) -> Decimal:
    base = Decimal(PESOS_CAMBIO.get(tipo, 1))
    extra = Decimal(PUNTOS_POR_MIL_CARACTERES) * Decimal(max(caracteres, 0)) / Decimal(1000)
    return (base + extra).quantize(Decimal('0.01'))


def _origen_de(usuario, origen=None):
    if origen:
        return origen
    if usuario is None:
        return 'sistema'
    if getattr(usuario, 'is_staff', False) or getattr(usuario, 'is_superuser', False):
        return 'admin'
    return 'director'


def registrar_cambio(plan, tipo, *, usuario=None, objeto_tipo='', objeto_id=None,
                     antes=None, despues=None, caracteres=0, origen=None, escuela_id=None,
                     curso_id=None) -> CambioPlanEstudio:
    """Agrega un evento al log del plan con su costo. Las ediciones repetidas del
    mismo objeto por el mismo usuario dentro de ``VENTANA_COALESCER`` se suman al
    último evento (conserva el ``antes`` original)."""
    ahora = timezone.now()
    escuela_id = escuela_id or plan.escuela_id
    curso_id = curso_id or (plan.curso_id if plan else None)
    usuario_id = getattr(usuario, 'pk', None)
    if tipo in TIPOS_COALESCIBLES and usuario_id and objeto_id is not None:
        previo = (
            CambioPlanEstudio.objects
            .filter(plan=plan, tipo=tipo, objeto_tipo=objeto_tipo, objeto_id=objeto_id,
                    usuario_id=usuario_id, fecha__gte=ahora - VENTANA_COALESCER)
            .order_by('-fecha', '-id').first()
        )
        if previo is not None:
            previo.caracteres += max(caracteres, 0)
            # El peso base se cobra una vez; los caracteres se van sumando.
            previo.puntos = puntos_para(tipo, previo.caracteres)
            previo.despues = despues
            previo.fecha = ahora
            previo.save(update_fields=['caracteres', 'puntos', 'despues', 'fecha'])
            return previo
    return CambioPlanEstudio.objects.create(
        plan=plan, escuela_id=escuela_id, curso_id=curso_id, tipo=tipo,
        origen=_origen_de(usuario, origen), objeto_tipo=objeto_tipo, objeto_id=objeto_id,
        antes=antes, despues=despues, caracteres=max(caracteres, 0),
        puntos=puntos_para(tipo, caracteres), usuario=usuario if usuario_id else None, fecha=ahora,
    )


def caracteres_cambiados(antes: str, despues: str) -> int:
    """Caracteres insertados + borrados entre dos versiones de un texto."""
    antes, despues = antes or '', despues or ''
    if antes == despues:
        return 0
    total = 0
    sm = difflib.SequenceMatcher(None, antes, despues, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != 'equal':
            total += (i2 - i1) + (j2 - j1)
    return total


# ---------------------------------------------------------------------------
# Resolver: estructura del curso para una escuela
# ---------------------------------------------------------------------------

@dataclass
class LeccionPlan:
    leccion: Leccion
    nombre: str
    oculta: bool = False
    nueva_base: bool = False
    propia: bool = False


@dataclass
class UnidadPlan:
    id: int                      # id de la Unidad base; propia → -PlanUnidad.id
    plan_unidad_id: int | None
    unidad_base_id: int | None
    nombre: str
    descripcion: str
    oculta: bool = False
    propia: bool = False
    nueva_base: bool = False
    lecciones: list[LeccionPlan] = field(default_factory=list)


def plan_activo(escuela_id, curso_id) -> PlanEstudio | None:
    if not escuela_id or not curso_id:
        return None
    return PlanEstudio.objects.filter(escuela_id=escuela_id, curso_id=curso_id, estado='activo').first()


def plan_de(escuela_id, curso_id) -> PlanEstudio | None:
    """Plan de la escuela para el curso, en cualquier estado."""
    if not escuela_id or not curso_id:
        return None
    return PlanEstudio.objects.filter(escuela_id=escuela_id, curso_id=curso_id).first()


def _lecciones_base_qs(curso_id, *, sueltas=False):
    qs = Leccion.objects.filter(curso_id=curso_id, escuela__isnull=True)
    if not sueltas:
        qs = qs.filter(unidad__isnull=False)
    return qs.select_related('categoria', 'unidad').order_by('unidad__orden', 'unidad_id', 'posicion', 'id')


def estructura_base(curso_id, *, sueltas=False) -> list[UnidadPlan]:
    """Curso base. ``sueltas``: agrega al final (unidad ``id=None``) las lecciones
    sin unidad, que cuentan para el avance aunque ``units/`` no las muestre."""
    unidades = {
        u.id: UnidadPlan(id=u.id, plan_unidad_id=None, unidad_base_id=u.id, nombre=u.nombre,
                         descripcion=u.descripcion)
        for u in Unidad.objects.filter(curso_id=curso_id).order_by('orden', 'id')
    }
    for lec in _lecciones_base_qs(curso_id, sueltas=sueltas):
        if lec.unidad_id is None:
            unidades.setdefault(None, UnidadPlan(id=None, plan_unidad_id=None, unidad_base_id=None,
                                                 nombre='', descripcion=''))
        unidades[lec.unidad_id].lecciones.append(LeccionPlan(leccion=lec, nombre=lec.nombre))
    return list(unidades.values())


def estructura_plan(plan: PlanEstudio) -> list[UnidadPlan]:
    """Estructura completa del plan (con ocultas y novedades del base marcadas)."""
    pus = list(plan.unidades.select_related('unidad_base').order_by('orden', 'id'))
    entradas = list(
        plan.lecciones.select_related('leccion__categoria', 'leccion__unidad')
        .order_by('plan_unidad__orden', 'orden', 'id')
    )
    salida: list[UnidadPlan] = []
    por_pu: dict[int, UnidadPlan] = {}
    por_base: dict[int, UnidadPlan] = {}
    for pu in pus:
        u = UnidadPlan(
            id=pu.unidad_base_id if pu.unidad_base_id else -pu.id,
            plan_unidad_id=pu.id, unidad_base_id=pu.unidad_base_id,
            nombre=pu.nombre_efectivo(), descripcion=pu.descripcion_efectiva(),
            oculta=pu.oculta, propia=pu.propia,
        )
        salida.append(u)
        por_pu[pu.id] = u
        if pu.unidad_base_id:
            por_base[pu.unidad_base_id] = u
    referenciadas = set()
    for e in entradas:
        referenciadas.add(e.leccion_id)
        por_pu[e.plan_unidad_id].lecciones.append(LeccionPlan(
            leccion=e.leccion, nombre=e.nombre_propio or e.leccion.nombre, oculta=e.oculta,
            propia=e.leccion.escuela_id is not None,
        ))
    # Novedades del base: unidades y lecciones que el plan aún no referencia.
    unidades_base = {u.id: u for u in Unidad.objects.filter(curso_id=plan.curso_id).order_by('orden', 'id')}
    for uid, ub in unidades_base.items():
        if uid not in por_base:
            u = UnidadPlan(id=uid, plan_unidad_id=None, unidad_base_id=uid, nombre=ub.nombre,
                           descripcion=ub.descripcion, nueva_base=True)
            salida.append(u)
            por_base[uid] = u
    for lec in _lecciones_base_qs(plan.curso_id):
        if lec.id in referenciadas:
            continue
        por_base[lec.unidad_id].lecciones.append(
            LeccionPlan(leccion=lec, nombre=lec.nombre, nueva_base=True)
        )
    # Unidades nuevas del base sin lecciones no aportan nada.
    return [u for u in salida if not (u.nueva_base and not u.lecciones)]


def filtrar_visibles(unidades: list[UnidadPlan]) -> list[UnidadPlan]:
    out = []
    for u in unidades:
        if u.oculta:
            continue
        lecs = [lp for lp in u.lecciones if not lp.oculta]
        out.append(UnidadPlan(**{**u.__dict__, 'lecciones': lecs}))
    return out


def estructura(curso_id, escuela_id=None, *, incluir_ocultas=False, plan=None,
               sueltas=False) -> list[UnidadPlan]:
    """Unidades + lecciones del curso tal como las ve la escuela (plan activo) o
    el curso base. ``plan`` fuerza un plan concreto (editor del director).
    ``sueltas`` (solo sin plan): incluye lecciones base sin unidad (conteos)."""
    plan = plan or plan_activo(escuela_id, curso_id)
    if plan is None:
        return estructura_base(curso_id, sueltas=sueltas)
    est = estructura_plan(plan)
    return est if incluir_ocultas else filtrar_visibles(est)


def escuela_de(user):
    return getattr(user, 'escuela_id', None) if user is not None else None


def estructura_para_usuario(user, curso_id):
    return estructura(curso_id, escuela_de(user))


def lecciones_visibles_ids(curso_id, escuela_id=None) -> list[int]:
    """Ids (en orden) de las lecciones que cuentan para el avance en ese curso."""
    return [lp.leccion.id for u in estructura(curso_id, escuela_id, sueltas=True) for lp in u.lecciones]


def lecciones_visibles_por_curso(curso_ids, escuela_id=None) -> dict[int, list[int]]:
    return {cid: lecciones_visibles_ids(cid, escuela_id) for cid in curso_ids}


def unidades_ocultas_ids(curso_id, escuela_id=None) -> set[int]:
    """Unidades del base que el plan activo oculta (para acotar el banco del examen)."""
    plan = plan_activo(escuela_id, curso_id)
    if plan is None:
        return set()
    return set(plan.unidades.filter(oculta=True, unidad_base__isnull=False)
               .values_list('unidad_base_id', flat=True))


def leccion_visible_para(leccion: Leccion, escuela_id) -> bool:
    """La lección es parte del curso que ve esa escuela (para el detalle)."""
    if leccion.escuela_id is not None and leccion.escuela_id != escuela_id:
        return False
    plan = plan_activo(escuela_id, leccion.curso_id)
    if plan is None:
        return leccion.escuela_id is None
    return leccion.id in set(lecciones_visibles_ids(leccion.curso_id, escuela_id))


def nombres_efectivos(curso_ids, escuela_id) -> dict[int, str]:
    """{leccion_id: nombre en el plan} para renombres del plan activo."""
    out = {}
    if not escuela_id:
        return out
    for e in PlanLeccion.objects.filter(
        plan__escuela_id=escuela_id, plan__curso_id__in=list(curso_ids), plan__estado='activo',
    ).exclude(nombre_propio=''):
        out[e.leccion_id] = e.nombre_propio
    return out


# ---------------------------------------------------------------------------
# Serialización de la estructura (misma forma que UnidadSerializer)
# ---------------------------------------------------------------------------

def serializar_estructura(unidades: list[UnidadPlan], curso_id, *, editor=False) -> list[dict]:
    from .serializers import LeccionSerializer
    out = []
    for orden, u in enumerate(unidades, start=1):
        lecciones = []
        for pos, lp in enumerate(u.lecciones, start=1):
            d = LeccionSerializer(lp.leccion).data
            d.update({
                'nombre': lp.nombre, 'posicion': pos, 'unidad': u.id,
                'unidad_orden': orden if u.id is not None else None,
                'unidad_nombre': u.nombre if u.id is not None else None, 'propia': lp.propia,
            })
            if editor:
                d.update({
                    'oculta': lp.oculta, 'nueva_base': lp.nueva_base,
                    'nombre_base': None if lp.propia else lp.leccion.nombre,
                })
            lecciones.append(d)
        item = {
            'id': u.id, 'curso': curso_id, 'orden': orden, 'nombre': u.nombre,
            'descripcion': u.descripcion, 'propia': u.propia, 'lecciones': lecciones,
        }
        if editor:
            item.update({'plan_unidad_id': u.plan_unidad_id, 'oculta': u.oculta,
                         'nueva_base': u.nueva_base})
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Creación y edición del plan
# ---------------------------------------------------------------------------

@transaction.atomic
def crear_plan(escuela_id, curso: Curso, *, usuario=None, nombre='', codigo_resolucion='') -> PlanEstudio:
    """Crea el plan materializando la disposición actual del curso base."""
    if PlanEstudio.objects.filter(escuela_id=escuela_id, curso=curso).exists():
        raise PlanError('La escuela ya tiene un plan de estudio para este curso.', codigo='plan_existente')
    plan = PlanEstudio.objects.create(
        escuela_id=escuela_id, curso=curso, nombre=nombre or curso.nombre,
        codigo_resolucion=codigo_resolucion or '', creado_por=usuario if getattr(usuario, 'pk', None) else None,
    )
    for orden, u in enumerate(estructura_base(curso.id), start=1):
        pu = PlanUnidad.objects.create(plan=plan, unidad_base_id=u.id, nombre_base=u.nombre, orden=orden)
        PlanLeccion.objects.bulk_create([
            PlanLeccion(plan=plan, plan_unidad=pu, leccion=lp.leccion, orden=i)
            for i, lp in enumerate(u.lecciones, start=1)
        ])
    registrar_cambio(plan, 'crear_plan', usuario=usuario, objeto_tipo='plan', objeto_id=plan.id,
                     despues={'nombre': plan.nombre, 'codigo_resolucion': plan.codigo_resolucion})
    return plan


def editar_datos_plan(plan, *, usuario=None, **campos):
    permitidos = {'nombre', 'codigo_resolucion'}
    antes, despues = {}, {}
    for k, v in campos.items():
        if k in permitidos and v is not None and getattr(plan, k) != v:
            antes[k] = getattr(plan, k)
            despues[k] = v
            setattr(plan, k, v)
    if despues:
        plan.save(update_fields=[*despues.keys(), 'actualizado_en'])
        registrar_cambio(plan, 'editar_plan', usuario=usuario, objeto_tipo='plan', objeto_id=plan.id,
                         antes=antes, despues=despues)
    return plan


def activar_plan(plan, *, usuario=None):
    if plan.estado == 'activo':
        return plan
    if not any(u.lecciones for u in filtrar_visibles(estructura_plan(plan))):
        raise PlanError('El plan no tiene lecciones visibles.', codigo='plan_vacio')
    plan.estado = 'activo'
    plan.activado_en = timezone.now()
    plan.save(update_fields=['estado', 'activado_en', 'actualizado_en'])
    registrar_cambio(plan, 'activar', usuario=usuario, objeto_tipo='plan', objeto_id=plan.id)
    return plan


def desactivar_plan(plan, *, usuario=None):
    if plan.estado != 'activo':
        return plan
    plan.estado = 'borrador'
    plan.save(update_fields=['estado', 'actualizado_en'])
    registrar_cambio(plan, 'desactivar', usuario=usuario, objeto_tipo='plan', objeto_id=plan.id)
    return plan


def _pu(plan, pu_id) -> PlanUnidad:
    try:
        return plan.unidades.get(pk=int(pu_id))
    except (PlanUnidad.DoesNotExist, TypeError, ValueError):
        raise PlanError(f'La unidad {pu_id} no es parte del plan.', codigo='unidad_inexistente')


def _pu_de_base(plan, unidad_base: Unidad) -> PlanUnidad:
    """Unidad del plan para una unidad del base (la crea al final si es nueva)."""
    pu = plan.unidades.filter(unidad_base=unidad_base).first()
    if pu is None:
        orden = (plan.unidades.aggregate(m=Max('orden'))['m'] or 0) + 1
        pu = PlanUnidad.objects.create(plan=plan, unidad_base=unidad_base,
                                       nombre_base=unidad_base.nombre, orden=orden)
    return pu


def _siguiente_orden(pu) -> int:
    return (pu.lecciones.aggregate(m=Max('orden'))['m'] or 0) + 1


def _entrada(plan, leccion_id, *, materializar=True) -> PlanLeccion:
    """Entrada del plan para la lección. Una novedad del base se materializa al
    final de su unidad (no se registra como cambio por sí sola)."""
    try:
        leccion_id = int(leccion_id)
    except (TypeError, ValueError):
        raise PlanError('Lección inválida.', codigo='leccion_inexistente')
    e = plan.lecciones.filter(leccion_id=leccion_id).select_related('leccion', 'plan_unidad').first()
    if e is not None:
        return e
    lec = Leccion.objects.filter(
        pk=leccion_id, curso_id=plan.curso_id, escuela__isnull=True, unidad__isnull=False,
    ).select_related('unidad').first()
    if lec is None or not materializar:
        raise PlanError(f'La lección {leccion_id} no es parte del curso.', codigo='leccion_inexistente')
    pu = _pu_de_base(plan, lec.unidad)
    return PlanLeccion.objects.create(plan=plan, plan_unidad=pu, leccion=lec, orden=_siguiente_orden(pu))


def _renumerar(pu):
    for i, e in enumerate(pu.lecciones.order_by('orden', 'id'), start=1):
        if e.orden != i:
            PlanLeccion.objects.filter(pk=e.pk).update(orden=i)


def _op_reordenar_unidades(plan, op, usuario):
    ids = [int(x) for x in (op.get('unidades') or [])]
    actuales = list(plan.unidades.order_by('orden', 'id').values_list('id', flat=True))
    if sorted(ids) != sorted(actuales):
        raise PlanError('«unidades» debe listar todas las unidades del plan, una vez cada una.')
    if ids == actuales:
        return
    for i, pu_id in enumerate(ids, start=1):
        PlanUnidad.objects.filter(pk=pu_id).update(orden=i)
    registrar_cambio(plan, 'reordenar_unidades', usuario=usuario, objeto_tipo='plan', objeto_id=plan.id,
                     antes=actuales, despues=ids)


def _op_reordenar_lecciones(plan, op, usuario):
    pu = _pu(plan, op.get('unidad'))
    ids = [int(x) for x in (op.get('lecciones') or [])]
    actuales = list(pu.lecciones.order_by('orden', 'id').values_list('leccion_id', flat=True))
    if sorted(ids) != sorted(actuales):
        raise PlanError('«lecciones» debe listar todas las lecciones de la unidad, una vez cada una.')
    if ids == actuales:
        return
    for i, lid in enumerate(ids, start=1):
        PlanLeccion.objects.filter(plan_unidad=pu, leccion_id=lid).update(orden=i)
    registrar_cambio(plan, 'reordenar_lecciones', usuario=usuario, objeto_tipo='unidad', objeto_id=pu.id,
                     antes=actuales, despues=ids)


def _op_mover_leccion(plan, op, usuario):
    e = _entrada(plan, op.get('leccion'))
    destino = _pu(plan, op.get('unidad'))
    origen = e.plan_unidad
    if destino.id == origen.id and op.get('posicion') is None:
        return
    resto = list(destino.lecciones.exclude(pk=e.pk).order_by('orden', 'id'))
    pos = op.get('posicion')
    pos = len(resto) if pos is None else max(0, min(int(pos) - 1, len(resto)))
    resto.insert(pos, e)
    e.plan_unidad = destino
    e.save(update_fields=['plan_unidad'])
    for i, x in enumerate(resto, start=1):
        PlanLeccion.objects.filter(pk=x.pk).update(orden=i)
    if origen.id != destino.id:
        _renumerar(origen)
    registrar_cambio(plan, 'mover_leccion', usuario=usuario, objeto_tipo='leccion', objeto_id=e.leccion_id,
                     antes={'unidad': origen.id}, despues={'unidad': destino.id, 'posicion': pos + 1})


def _op_visibilidad(plan, op, usuario, oculta: bool):
    tipo = 'ocultar' if oculta else 'mostrar'
    if op.get('unidad') is not None:
        pu = _pu(plan, op['unidad'])
        if pu.oculta == oculta:
            return
        pu.oculta = oculta
        pu.save(update_fields=['oculta'])
        registrar_cambio(plan, tipo, usuario=usuario, objeto_tipo='unidad', objeto_id=pu.id)
        return
    e = _entrada(plan, op.get('leccion'))
    if e.oculta == oculta:
        return
    e.oculta = oculta
    e.save(update_fields=['oculta'])
    registrar_cambio(plan, tipo, usuario=usuario, objeto_tipo='leccion', objeto_id=e.leccion_id)


def _op_renombrar(plan, op, usuario):
    nombre = (op.get('nombre') or '').strip()
    if op.get('unidad') is not None:
        pu = _pu(plan, op['unidad'])
        if len(nombre) > 100:
            raise PlanError('El nombre admite hasta 100 caracteres.')
        antes = {'nombre': pu.nombre_propio, 'descripcion': pu.descripcion_propia}
        if pu.propia and not nombre:
            raise PlanError('Una unidad propia necesita nombre.')
        pu.nombre_propio = '' if (pu.unidad_base_id and nombre == pu.unidad_base.nombre) else nombre
        if 'descripcion' in op:
            pu.descripcion_propia = (op.get('descripcion') or '').strip()[:255]
        despues = {'nombre': pu.nombre_propio, 'descripcion': pu.descripcion_propia}
        if antes == despues:
            return
        pu.save(update_fields=['nombre_propio', 'descripcion_propia'])
        registrar_cambio(plan, 'renombrar', usuario=usuario, objeto_tipo='unidad', objeto_id=pu.id,
                         antes=antes, despues=despues)
        return
    e = _entrada(plan, op.get('leccion'))
    if len(nombre) > 100:
        raise PlanError('El nombre admite hasta 100 caracteres.')
    if e.leccion.escuela_id is not None:
        # Lección propia: el nombre vive en la lección.
        if not nombre:
            raise PlanError('La lección necesita nombre.')
        if nombre == e.leccion.nombre:
            return
        antes = e.leccion.nombre
        Leccion.objects.filter(pk=e.leccion_id).update(nombre=nombre)
    else:
        nuevo = '' if nombre == e.leccion.nombre else nombre
        if nuevo == e.nombre_propio:
            return
        antes = e.nombre_propio or e.leccion.nombre
        e.nombre_propio = nuevo
        e.save(update_fields=['nombre_propio'])
    registrar_cambio(plan, 'renombrar', usuario=usuario, objeto_tipo='leccion', objeto_id=e.leccion_id,
                     antes={'nombre': antes}, despues={'nombre': nombre or e.leccion.nombre})


def _op_agregar_unidad(plan, op, usuario):
    nombre = (op.get('nombre') or '').strip()
    if not nombre or len(nombre) > 100:
        raise PlanError('La unidad necesita un nombre de hasta 100 caracteres.')
    unidades = list(plan.unidades.order_by('orden', 'id'))
    pos = op.get('posicion')
    pos = len(unidades) if pos is None else max(0, min(int(pos) - 1, len(unidades)))
    pu = PlanUnidad(plan=plan, propia=True, nombre_propio=nombre,
                    descripcion_propia=(op.get('descripcion') or '').strip()[:255])
    unidades.insert(pos, pu)
    for i, u in enumerate(unidades, start=1):
        u.orden = i
        if u.pk:
            PlanUnidad.objects.filter(pk=u.pk).update(orden=i)
    pu.save()
    registrar_cambio(plan, 'agregar_unidad', usuario=usuario, objeto_tipo='unidad', objeto_id=pu.id,
                     despues={'nombre': nombre, 'posicion': pos + 1})
    return pu


def _op_quitar_unidad(plan, op, usuario):
    pu = _pu(plan, op.get('unidad'))
    if not pu.propia:
        raise PlanError('Solo se pueden quitar unidades propias; las del curso base se ocultan.')
    if pu.lecciones.exists():
        raise PlanError('Mueve o elimina las lecciones de la unidad antes de quitarla.', codigo='unidad_con_lecciones')
    antes = {'nombre': pu.nombre_propio}
    pu_id = pu.id
    pu.delete()
    for i, u in enumerate(plan.unidades.order_by('orden', 'id'), start=1):
        PlanUnidad.objects.filter(pk=u.pk).update(orden=i)
    registrar_cambio(plan, 'quitar_unidad', usuario=usuario, objeto_tipo='unidad', objeto_id=pu_id, antes=antes)


def _op_aceptar_novedad(plan, op, usuario):
    if plan.lecciones.filter(leccion_id=op.get('leccion')).exists():
        return
    e = _entrada(plan, op.get('leccion'))
    registrar_cambio(plan, 'aceptar_novedad', usuario=usuario, objeto_tipo='leccion', objeto_id=e.leccion_id)


OPS = {
    'reordenar_unidades': _op_reordenar_unidades,
    'reordenar_lecciones': _op_reordenar_lecciones,
    'mover_leccion': _op_mover_leccion,
    'ocultar': lambda p, o, u: _op_visibilidad(p, o, u, True),
    'mostrar': lambda p, o, u: _op_visibilidad(p, o, u, False),
    'renombrar': _op_renombrar,
    'agregar_unidad': _op_agregar_unidad,
    'quitar_unidad': _op_quitar_unidad,
    'aceptar_novedad': _op_aceptar_novedad,
}


def aplicar_ops(plan, ops, *, usuario=None) -> int:
    """Aplica una lista de operaciones de forma atómica. Devuelve cuántas se
    aplicaron; ante una inválida no aplica ninguna (``PlanError.op_indice``)."""
    if not isinstance(ops, list) or not ops:
        raise PlanError('«ops» debe ser una lista no vacía.')
    with transaction.atomic():
        for i, op in enumerate(ops):
            if not isinstance(op, dict) or op.get('op') not in OPS:
                raise PlanError(f'Operación desconocida: {op!r}. Válidas: {", ".join(OPS)}.', op_indice=i)
            try:
                OPS[op['op']](plan, op, usuario)
            except PlanError as exc:
                exc.op_indice = i
                raise
            except (TypeError, ValueError) as exc:
                raise PlanError(f'Operación {op["op"]} inválida: {exc}', op_indice=i)
        PlanEstudio.objects.filter(pk=plan.pk).update(actualizado_en=timezone.now())
    return len(ops)


# ---------------------------------------------------------------------------
# Lecciones propias
# ---------------------------------------------------------------------------

def validar_contenido(contenido: str):
    if len(contenido or '') > MAX_CARACTERES_LECCION:
        raise PlanError(f'El contenido admite hasta {MAX_CARACTERES_LECCION} caracteres.')
    from content_pipeline.processors.content_blocks import validate_components
    errores, _ = validate_components(contenido or '')
    if errores:
        raise PlanError('Contenido inválido: ' + ' · '.join(errores), codigo='contenido_invalido')


@transaction.atomic
def crear_leccion_propia(plan, *, unidad, nombre, contenido='', descripcion='', duracion_min=0,
                         posicion=None, usuario=None) -> Leccion:
    nombre = (nombre or '').strip()
    if not nombre or len(nombre) > 100:
        raise PlanError('La lección necesita un nombre de hasta 100 caracteres.')
    validar_contenido(contenido)
    pu = _pu(plan, unidad)
    lec = Leccion.objects.create(
        curso_id=plan.curso_id, escuela_id=plan.escuela_id, unidad=None, nombre=nombre,
        posicion=0, tipo='texto', descripcion=(descripcion or '')[:255] or None,
        contenido=contenido or '', duracion_min=max(int(duracion_min or 0), 0),
        url_video='http://placeholder.url', url_audio='http://placeholder.url',
    )
    resto = list(pu.lecciones.order_by('orden', 'id'))
    pos = len(resto) if posicion is None else max(0, min(int(posicion) - 1, len(resto)))
    e = PlanLeccion(plan=plan, plan_unidad=pu, leccion=lec)
    resto.insert(pos, e)
    for i, x in enumerate(resto, start=1):
        x.orden = i
        if x.pk:
            PlanLeccion.objects.filter(pk=x.pk).update(orden=i)
    e.save()
    registrar_cambio(plan, 'agregar_leccion', usuario=usuario, objeto_tipo='leccion', objeto_id=lec.id,
                     despues={'nombre': nombre, 'unidad': pu.id}, caracteres=len(contenido or ''))
    return lec


def leccion_propia(plan, leccion_id) -> Leccion:
    lec = Leccion.objects.filter(pk=leccion_id, escuela_id=plan.escuela_id, curso_id=plan.curso_id).first()
    if lec is None:
        raise PlanError('Lección propia inexistente.', codigo='leccion_inexistente')
    return lec


def editar_leccion_propia(plan, leccion_id, *, usuario=None, **campos) -> Leccion:
    lec = leccion_propia(plan, leccion_id)
    antes, despues, chars = {}, {}, 0
    if 'nombre' in campos and campos['nombre'] is not None:
        nombre = campos['nombre'].strip()
        if not nombre or len(nombre) > 100:
            raise PlanError('La lección necesita un nombre de hasta 100 caracteres.')
        if nombre != lec.nombre:
            antes['nombre'], despues['nombre'] = lec.nombre, nombre
            lec.nombre = nombre
    if 'contenido' in campos and campos['contenido'] is not None:
        validar_contenido(campos['contenido'])
        if campos['contenido'] != lec.contenido:
            chars = caracteres_cambiados(lec.contenido, campos['contenido'])
            antes['contenido_caracteres'] = len(lec.contenido)
            despues['contenido_caracteres'] = len(campos['contenido'])
            lec.contenido = campos['contenido']
    if 'descripcion' in campos and campos['descripcion'] is not None:
        d = campos['descripcion'][:255] or None
        if d != lec.descripcion:
            antes['descripcion'], despues['descripcion'] = lec.descripcion, d
            lec.descripcion = d
    if 'duracion_min' in campos and campos['duracion_min'] is not None:
        m = max(int(campos['duracion_min']), 0)
        if m != lec.duracion_min:
            antes['duracion_min'], despues['duracion_min'] = lec.duracion_min, m
            lec.duracion_min = m
    if despues:
        lec.save()
        registrar_cambio(plan, 'editar_leccion', usuario=usuario, objeto_tipo='leccion', objeto_id=lec.id,
                         antes=antes, despues=despues, caracteres=chars)
    return lec


def eliminar_leccion_propia(plan, leccion_id, *, usuario=None, tiene_avance=False) -> str:
    """Borra la lección propia; si algún alumno ya la completó, solo la oculta
    (no se pierde su avance). Devuelve 'eliminada' u 'oculta'."""
    lec = leccion_propia(plan, leccion_id)
    if tiene_avance:
        e = _entrada(plan, lec.id)
        if not e.oculta:
            e.oculta = True
            e.save(update_fields=['oculta'])
            registrar_cambio(plan, 'ocultar', usuario=usuario, objeto_tipo='leccion', objeto_id=lec.id)
        return 'oculta'
    with transaction.atomic():
        pu = plan.lecciones.filter(leccion=lec).values_list('plan_unidad_id', flat=True).first()
        nombre, lid = lec.nombre, lec.id
        lec.delete()
        if pu:
            _renumerar(PlanUnidad.objects.get(pk=pu))
        registrar_cambio(plan, 'eliminar_leccion', usuario=usuario, objeto_tipo='leccion', objeto_id=lid,
                         antes={'nombre': nombre})
    return 'eliminada'


# ---------------------------------------------------------------------------
# Evento de sistema: el curso base borró una lección que un plan usaba
# ---------------------------------------------------------------------------

def registrar_base_eliminada(leccion: Leccion):
    if leccion.escuela_id is not None:
        return
    for e in PlanLeccion.objects.filter(leccion=leccion).select_related('plan'):
        registrar_cambio(
            e.plan, 'base_eliminada', objeto_tipo='leccion', objeto_id=leccion.id, origen='sistema',
            antes={'nombre': e.nombre_propio or leccion.nombre, 'oculta': e.oculta},
        )


# ---------------------------------------------------------------------------
# Costo de mantención por escuela
# ---------------------------------------------------------------------------

def _rango(qs, campo, desde, hasta):
    if desde:
        qs = qs.filter(**{f'{campo}__date__gte': desde})
    if hasta:
        qs = qs.filter(**{f'{campo}__date__lte': hasta})
    return qs


def costo_escuelas(*, desde=None, hasta=None, escuela_id=None) -> list[dict]:
    """Resumen por escuela: cambios por tipo, puntos, horas, montos, contenido propio."""
    cambios = _rango(CambioPlanEstudio.objects.all(), 'fecha', desde, hasta)
    costos = _rango(CostoMantencion.objects.all(), 'fecha', desde, hasta)
    if escuela_id:
        cambios = cambios.filter(escuela_id=escuela_id)
        costos = costos.filter(escuela_id=escuela_id)
    filas: dict[int, dict] = {}

    def fila(eid):
        return filas.setdefault(eid, {
            'escuela_id': eid, 'cambios': 0, 'puntos': Decimal('0'), 'caracteres': 0, 'por_tipo': {},
            'minutos_staff': 0, 'monto_clp': 0, 'costos_por_tipo': {},
        })

    for r in cambios.values('escuela_id', 'tipo').annotate(n=Count('id'), p=Sum('puntos'), c=Sum('caracteres')):
        f = fila(r['escuela_id'])
        f['cambios'] += r['n']
        f['puntos'] += r['p'] or 0
        f['caracteres'] += r['c'] or 0
        f['por_tipo'][r['tipo']] = r['n']
    for r in costos.values('escuela_id', 'tipo').annotate(m=Sum('minutos'), monto=Sum('monto_clp')):
        f = fila(r['escuela_id'])
        f['minutos_staff'] += r['m'] or 0
        f['monto_clp'] += r['monto'] or 0
        f['costos_por_tipo'][r['tipo']] = r['monto'] or 0
    # Foto actual (no depende del período).
    planes = PlanEstudio.objects.all()
    propias = Leccion.objects.filter(escuela__isnull=False)
    if escuela_id:
        planes = planes.filter(escuela_id=escuela_id)
        propias = propias.filter(escuela_id=escuela_id)
    for r in planes.values('escuela_id').annotate(
        n=Count('id'), activos=Count('id', filter=Q(estado='activo')),
    ):
        f = fila(r['escuela_id'])
        f['planes'] = r['n']
        f['planes_activos'] = r['activos']
    for r in propias.values('escuela_id').annotate(n=Count('id')):
        fila(r['escuela_id'])['lecciones_propias'] = r['n']
    from .models import Escuela
    nombres = dict(Escuela.objects.filter(pk__in=filas.keys()).values_list('id', 'nombre'))
    out = []
    for eid, f in filas.items():
        f.setdefault('planes', 0)
        f.setdefault('planes_activos', 0)
        f.setdefault('lecciones_propias', 0)
        f['escuela_nombre'] = nombres.get(eid, '')
        f['puntos'] = float(f['puntos'])
        f['horas_staff'] = round(f['minutos_staff'] / 60, 2)
        out.append(f)
    out.sort(key=lambda f: (-f['puntos'], -f['monto_clp'], f['escuela_id']))
    return out


def novedades_pendientes(plan) -> int:
    return sum(1 for u in estructura_plan(plan) for lp in u.lecciones if lp.nueva_base)

