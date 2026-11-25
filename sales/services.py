import logging
import math
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone
from django.db import transaction
from django.db.models import F, Q

from schools.models import Escuela, Curso, PlanCurso
from .models import (
    AccessKey, EstudianteCurso, IntentoPago, MovimientoLlaves, Producto, Venta, TransbankTransaction,
)
from .utils import extract_ids_from_buy_order, extract_dias_from_buy_order

# Días de acceso que habilita 1 llave. Otorgar N días cuesta ceil(N/7) llaves.
DIAS_POR_LLAVE = 7
def registrar_movimiento_llaves(escuela_id, cantidad, motivo):
    """Deja constancia de llaves gastadas (<0) o devueltas (>0) por una escuela."""
    if escuela_id and cantidad:
        MovimientoLlaves.objects.create(escuela_id=escuela_id, cantidad=cantidad, motivo=motivo)


def llaves_para_dias(dias) -> int:
    """Nº de llaves que cuesta habilitar `dias` de acceso (1 llave = 7 días).

    Fracciones se redondean hacia arriba: 7 días = 1 llave, 8 días = 2 llaves,
    35 días = 5 llaves. `dias <= 0` cuesta 0.
    """
    d = int(dias or 0)
    if d <= 0:
        return 0
    return math.ceil(d / DIAS_POR_LLAVE)


def _inscripciones_vigentes(user, now):
    """Inscripciones con llave/compra vigente (los cupos se evalúan aparte)."""
    return (
        EstudianteCurso.objects
        .filter(estudiante_id=user, access_key_id__status='active')
        .exclude(access_key_id__origen='seat')
        .filter(Q(access_key_id__valid_from__isnull=True) | Q(access_key_id__valid_from__lte=now))
        .filter(Q(access_key_id__valid_until__isnull=True) | Q(access_key_id__valid_until__gte=now))
    )


def cursos_con_acceso_vigente(user) -> set:
    """Ids de cursos a los que `user` (estudiante) tiene acceso VIGENTE.

    Dos vías: (1) una inscripción cuya llave/compra está 'active' y dentro de
    su rango temporal; (2) un cupo de suscripción activo cuya suscripción da
    acceso (vigente o en gracia) → todos los cursos. Las AccessKey de origen
    'seat' solo marcan la inscripción (para el avance); el acceso lo decide el
    cupo. Es la fuente de verdad para gatear el contenido premium.
    """
    from .suscripciones import cupo_con_acceso
    now = timezone.now()
    ids = set(_inscripciones_vigentes(user, now).values_list('curso_id_id', flat=True))
    if getattr(user, 'pk', None) and cupo_con_acceso(user, now) is not None:
        ids |= set(Curso.objects.values_list('id', flat=True))
    return ids


def tiene_acceso_a_curso(user, curso_id) -> bool:
    """True si el estudiante tiene acceso vigente al curso indicado."""
    try:
        cid = int(curso_id)
    except (TypeError, ValueError):
        return False
    return cid in cursos_con_acceso_vigente(user)


def vencimiento_acceso(user, curso_id):
    """(tiene_acceso, valid_until | None) del acceso vigente de `user` al curso.

    Mismo criterio que `cursos_con_acceso_vigente`; `valid_until` None = sin
    vencimiento. Con cupo de suscripción, vence al fin de la gracia. Lo usa el
    modo offline para acotar la licencia de descarga al fin del acceso.
    """
    try:
        cid = int(curso_id)
    except (TypeError, ValueError):
        return False, None
    from .suscripciones import cupo_con_acceso, fin_acceso
    now = timezone.now()
    tiene, hasta = False, None
    ec = _inscripciones_vigentes(user, now).select_related('access_key_id').filter(curso_id=cid).first()
    if ec is not None:
        tiene, hasta = True, ec.access_key_id.valid_until
    cupo = cupo_con_acceso(user, now) if getattr(user, 'pk', None) else None
    if cupo is not None and Curso.objects.filter(pk=cid).exists():
        hasta_cupo = fin_acceso(cupo.suscripcion)
        # None = sin vencimiento: gana siempre.
        hasta = None if (tiene and hasta is None) else max(filter(None, [hasta, hasta_cupo]))
        tiene = True
    return tiene, hasta


def precio_plan(curso: Curso, dias) -> int | None:
    """Precio autoritativo (CLP entero) del plan activo (curso, dias).

    Fuente de verdad server-side (PlanCurso) para validar el `amount` del
    cliente. Devuelve None si no existe un plan activo con esa duración.
    """
    plan = PlanCurso.objects.filter(curso=curso, dias=dias, activo=True).first()
    return int(plan.precio) if plan is not None else None


def precio_final_producto(producto: Producto) -> int:
    """Precio autoritativo (CLP entero) de un Producto: valor_neto menos descuento.

    Fuente de verdad server-side para validar el `amount` que envía el cliente
    en el inicio del pago (evita que se manipule el precio en el front).
    """
    base = Decimal(producto.valor_neto or 0)
    desc = Decimal(producto.descuento or 0)
    if desc > 0:
        base = base * (Decimal(1) - desc / Decimal(100))
    return int(base.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


class PriceMismatchError(Exception):
    """El monto enviado por el cliente no coincide con el precio autoritativo."""


class CanjeError(Exception):
    """Error de negocio al canjear una llave de acceso."""
    def __init__(self, message, code='canje_error'):
        super().__init__(message)
        self.code = code


@transaction.atomic
def canjear_access_key(estudiante, key_str: str, curso_id: int) -> EstudianteCurso:
    """Canjea una AccessKey (entregada por su director) por una inscripción.

    Reglas:
      - La llave debe existir, estar 'active', y dentro del rango temporal.
      - El estudiante no debe estar ya inscrito en el curso.
      - El curso debe existir.
      - La llave no debe estar ligada ya a otra inscripción (un solo uso: las
        llaves que entrega `activar_curso` ya vienen ligadas a su estudiante).
      - Tras canjearse queda 'active' y ligada a la inscripción: el acceso
        vigente (`cursos_con_acceso_vigente`) exige status 'active'.

    Returns el EstudianteCurso creado. Raise CanjeError en cualquier violación.
    """
    if not key_str:
        raise CanjeError("access_key requerida.", 'missing_key')

    try:
        curso = Curso.objects.get(pk=curso_id)
    except Curso.DoesNotExist:
        raise CanjeError("Curso no existe.", 'curso_not_found')

    try:
        access_key = AccessKey.objects.select_for_update().get(key=key_str)
    except AccessKey.DoesNotExist:
        raise CanjeError("Llave no encontrada.", 'key_not_found')

    if access_key.status != 'active':
        raise CanjeError(f"Llave en estado '{access_key.status}'.", 'key_inactive')

    now = timezone.now()
    if access_key.valid_until and access_key.valid_until < now:
        access_key.status = 'revoked'
        access_key.save(update_fields=['status'])
        raise CanjeError("Llave expirada.", 'key_expired')

    if EstudianteCurso.objects.filter(access_key_id=access_key).exists():
        raise CanjeError("La llave ya fue utilizada.", 'key_already_bound')

    if EstudianteCurso.objects.filter(estudiante_id=estudiante, curso_id=curso).exists():
        raise CanjeError("Ya estás inscrito en este curso.", 'already_enrolled')

    # Código generado por un director: la vigencia corre desde el canje (antes
    # valid_until era el vencimiento del código) y, si el estudiante no tiene
    # escuela, queda vinculado a la emisora (misma regla que aprobar solicitud).
    if access_key.dias:
        access_key.valid_from = now
        access_key.valid_until = now + timedelta(days=access_key.dias)
        access_key.save(update_fields=['valid_from', 'valid_until'])
        if access_key.escuela_id and not estudiante.escuela_id:
            estudiante.escuela_id = access_key.escuela_id
            estudiante.save(update_fields=['escuela'])

    return EstudianteCurso.objects.create(
        estudiante_id=estudiante,
        curso_id=curso,
        access_key_id=access_key,
    )


# ============================================================
# Códigos canjeables (director → estudiante fuera de la plataforma)
# ============================================================
CODIGO_VIGENCIA_DIAS = 90   # plazo para canjear un código antes de que venza
CODIGOS_MAX_POR_LOTE = 100
_CODIGO_ALFABETO = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sin 0/O/1/I


class CodigoError(Exception):
    def __init__(self, message, code='invalid'):
        super().__init__(message)
        self.code = code


def _nuevo_codigo(length=10):
    import secrets
    while True:
        k = "".join(secrets.choice(_CODIGO_ALFABETO) for _ in range(length))
        if not AccessKey.objects.filter(key=k).exists():
            return k


@transaction.atomic
def generar_codigos(*, escuela_id, cantidad, dias, descontar=True):
    """Crea `cantidad` códigos de `dias` días para la escuela.

    Con `descontar` (director) cobra `cantidad × llaves_para_dias(dias)` del
    saldo de forma atómica; el admin genera sin descontar (`llaves=0`).
    """
    try:
        cantidad, dias = int(cantidad), int(dias)
    except (TypeError, ValueError):
        raise CodigoError("cantidad y dias deben ser enteros.")
    if not 1 <= cantidad <= CODIGOS_MAX_POR_LOTE:
        raise CodigoError(f"La cantidad debe estar entre 1 y {CODIGOS_MAX_POR_LOTE}.")
    if dias <= 0 or dias % DIAS_POR_LLAVE:
        raise CodigoError(f"Los días deben ser múltiplo de {DIAS_POR_LLAVE}.")

    escuela = Escuela.objects.select_for_update().get(pk=escuela_id)
    llaves_c_u = llaves_para_dias(dias) if descontar else 0
    total = cantidad * llaves_c_u
    if escuela.basic_key < total:
        raise CodigoError(
            f"No hay llaves suficientes. Requieres {total}, tienes {escuela.basic_key}.", 'sin_saldo',
        )
    if total:
        escuela.basic_key -= total
        escuela.save(update_fields=['basic_key'])
        registrar_movimiento_llaves(escuela.pk, -total, 'codigos')

    vence = timezone.now() + timedelta(days=CODIGO_VIGENCIA_DIAS)
    return [
        AccessKey.objects.create(
            key=_nuevo_codigo(), escuela=escuela, dias=dias, llaves=llaves_c_u,
            valid_until=vence, origen='key',
        )
        for _ in range(cantidad)
    ]


@transaction.atomic
def anular_codigo(codigo: AccessKey):
    """Anula un código no canjeado y devuelve sus llaves a la escuela."""
    codigo = AccessKey.objects.select_for_update().get(pk=codigo.pk)
    if EstudianteCurso.objects.filter(access_key_id=codigo).exists():
        raise CodigoError("El código ya fue canjeado.", 'canjeado')
    if codigo.status != 'active':
        raise CodigoError("El código ya está anulado.", 'anulado')
    codigo.status = 'revoked'
    codigo.save(update_fields=['status'])
    if codigo.llaves and codigo.escuela_id:
        Escuela.objects.filter(pk=codigo.escuela_id).update(basic_key=F('basic_key') + codigo.llaves)
        registrar_movimiento_llaves(codigo.escuela_id, codigo.llaves, 'anulacion')
    return codigo



# ============================================================
# Activación de curso (seat / key) — reutilizable
# ------------------------------------------------------------
# Estos helpers y el servicio `activar_curso_para_estudiante` concentran las
# reglas de consumo de cupos/llaves de una escuela. Los usan tanto la vista
# `ActivarCursoView` / `SolicitudAccesoViewSet.aprobar` (sales) como el alta
# masiva de estudiantes (accounts) para no duplicar la lógica de saldo.
# ============================================================

class SinSaldoError(Exception):
    """La escuela no tiene cupos ni llaves suficientes para activar el curso."""
    def __init__(self, message, code='sin_saldo'):
        super().__init__(message)
        self.code = code


def suscripcion_vigente(escuela, now=None):
    """True si la suscripción de la escuela da acceso hoy (vigente o en gracia)."""
    from .suscripciones import con_acceso
    return con_acceso(escuela, now)


def _cupo_en_escuela(escuela, estudiante):
    from .suscripciones import cupo_activo
    if estudiante is None:
        return None
    cupo = cupo_activo(estudiante)
    return cupo if cupo and cupo.suscripcion.escuela_id == escuela.pk else None


def tiene_seat(escuela, estudiante=None):
    """True si activar vía cupo es posible: el estudiante ya ocupa un cupo de
    esta escuela (no consume nada) o quedan cupos libres en el plan."""
    from .suscripciones import cupos_activos, suscripcion_de
    sus = suscripcion_de(escuela)
    if sus is None or not sus.con_acceso():
        return False
    if _cupo_en_escuela(escuela, estudiante) is not None:
        return True
    return cupos_activos(sus).count() < sus.plan.cupos


def tiene_key(escuela, keys_needed):
    return escuela.basic_key >= keys_needed


def resolver_source_director(escuela, source, keys_needed, estudiante=None):
    """Devuelve 'seat' | 'key' | None según disponibilidad en la escuela.

    Un cupo da al estudiante acceso a todos los cursos mientras dure la
    suscripción (si ya tiene cupo, activar otro curso no consume nada). Una
    llave habilita 7 días: otorgar `days` días cuesta ceil(days/7) llaves.
    """
    if source == "seat":
        return "seat" if tiene_seat(escuela, estudiante) else None
    if source == "key":
        return "key" if tiene_key(escuela, keys_needed) else None
    # auto: cupo primero (más barato), luego llaves.
    if tiene_seat(escuela, estudiante):
        return "seat"
    if tiene_key(escuela, keys_needed):
        return "key"
    return None


def decrementar_saldo(escuela, resolved_source, keys_needed, estudiante=None, por=None):
    """Consume el saldo: asigna el cupo al estudiante o descuenta llaves.

    Con cupo, refresca en `escuela` el espejo `basic_*` (el llamador la
    guarda después y no debe pisar el conteo de cupos).
    """
    if resolved_source == "seat":
        from .suscripciones import SuscripcionError, asignar_cupo
        try:
            asignar_cupo(escuela, estudiante, por=por)
        except SuscripcionError as e:
            raise SinSaldoError(str(e))
        fresca = Escuela.objects.get(pk=escuela.pk)
        for campo in ('basic_access', 'basic_seats_max', 'basic_seats_used', 'basic_access_until'):
            setattr(escuela, campo, getattr(fresca, campo))
    else:  # key
        escuela.basic_key -= keys_needed
        registrar_movimiento_llaves(escuela.pk, -keys_needed, 'activacion')


def mensaje_sin_saldo(source, keys_needed=1):
    if source == "seat":
        return "Tu escuela no tiene cupos disponibles en la suscripción."
    if source == "key":
        return f"Tu escuela no tiene suficientes llaves disponibles (se requieren {keys_needed})."
    return "Tu escuela no tiene ni cupos ni llaves disponibles."


def inscripcion_previa(estudiante, curso):
    """(EstudianteCurso | None, vigente: bool) del estudiante en el curso."""
    ec = (
        EstudianteCurso.objects.select_related('access_key_id')
        .filter(estudiante_id=estudiante, curso_id=curso).first()
    )
    if ec is None:
        return None, False
    return ec, bool(ec.access_key_id and ec.access_key_id.is_valid())


def asignar_por_source(estudiante, curso, days, resolved_source, decrement_escuela=None,
                       contar_seat=False, escuela=None, llaves_cobradas=0, escuela_cobro=None):
    """Crea AccessKey + EstudianteCurso según el origen ('seat' | 'key').

    'seat': el estudiante ya debe tener (o recibe aquí, con `contar_seat=True`,
    p. ej. activación de admin) un cupo; la AccessKey solo marca la inscripción
    y no vence (el acceso lo decide el cupo). 'key': acceso por `days` días.
    Si el estudiante ya tenía una inscripción sin acceso vigente, se le asigna
    la llave nueva (unique estudiante+curso). El llamador debe rechazar antes
    el caso con acceso vigente (`inscripcion_previa`). `llaves_cobradas` /
    `escuela_cobro` registran lo que pagó el director (se devuelven si el alumno
    nunca abre el curso; ver `devolver_llaves_no_usadas`).
    """
    with transaction.atomic():
        if resolved_source == "seat":
            escuela = escuela or getattr(estudiante, 'escuela', None)
            if contar_seat:
                from .suscripciones import SuscripcionError, asignar_cupo
                if escuela is None:
                    raise SinSaldoError("El estudiante no pertenece a una escuela con suscripción.")
                try:
                    asignar_cupo(escuela, estudiante)
                except SuscripcionError as e:
                    raise SinSaldoError(str(e))
            access_key = AccessKey.objects.create(valid_until=None, origen="seat")
        else:
            access_key = AccessKey.objects.create(
                valid_until=timezone.now() + timedelta(days=days),
                origen="key",
                llaves=llaves_cobradas or 0,
                cobrada_a=escuela_cobro if llaves_cobradas else None,
            )
        previa, _ = inscripcion_previa(estudiante, curso)
        if previa is not None:
            previa.access_key_id = access_key
            previa.save(update_fields=['access_key_id'])
        else:
            EstudianteCurso.objects.create(
                estudiante_id=estudiante,
                curso_id=curso,
                access_key_id=access_key,
            )
    return access_key


@transaction.atomic
def activar_curso_para_estudiante(*, estudiante, curso, days, source, es_admin, escuela=None):
    """Activa un curso para un estudiante, consumiendo saldo si aplica.

    - Admin (`es_admin=True`): activa sin descontar saldo ('seat' o 'key' según
      `source`; el default 'auto' cae a 'key').
    - Director / escuela: bloquea la fila de la escuela (`select_for_update`),
      resuelve seat/key con `source`, descuenta `ceil(days/7)` llaves o 1 cupo y
      crea la inscripción. Si no hay saldo → `SinSaldoError`.

    Devuelve la `AccessKey` creada. Es atómico: si algo falla, no descuenta.
    """
    if es_admin:
        resolved = "seat" if source == "seat" else "key"
        return asignar_por_source(estudiante, curso, days, resolved, contar_seat=True)

    if escuela is None:
        raise SinSaldoError("No se indicó la escuela para descontar el saldo.")

    keys_needed = llaves_para_dias(days)
    escuela_locked = Escuela.objects.select_for_update().get(pk=escuela.pk)
    resolved = resolver_source_director(escuela_locked, source, keys_needed, estudiante)
    if resolved is None:
        raise SinSaldoError(mensaje_sin_saldo(source, keys_needed))
    decrementar_saldo(escuela_locked, resolved, keys_needed, estudiante)
    escuela_locked.save()
    cobro = {'llaves_cobradas': keys_needed, 'escuela_cobro': escuela_locked} if resolved == 'key' else {}
    return asignar_por_source(estudiante, curso, days, resolved, escuela=escuela_locked, **cobro)


# ============================================================
# Devolución de llaves asignadas que el estudiante nunca usó
# ------------------------------------------------------------
# Una llave asignada por un director (origen 'key', `llaves` > 0, `cobrada_a`)
# cuyo estudiante nunca abrió el curso (`usada_en` null, sin avance) y que ya
# venció devuelve sus llaves al saldo de la escuela que pagó. No aplica a
# estudiantes sin escuela, compras, cupos ni códigos canjeables.
# ============================================================

def marcar_uso_llave(estudiante, curso):
    """Marca como usada (primera apertura del curso) la llave de la inscripción."""
    if not getattr(estudiante, 'pk', None):
        return 0
    return AccessKey.objects.filter(
        estudiantecurso__estudiante_id=estudiante.pk,
        estudiantecurso__curso_id=getattr(curso, 'pk', curso),
        usada_en__isnull=True,
    ).update(usada_en=timezone.now())


def llaves_devolvibles(now=None, escuela_id=None):
    """QuerySet de llaves asignadas, vencidas, cobradas y nunca usadas."""
    from django.db.models import Exists, OuterRef
    from accounts.models import EstudianteLeccion, Prueba
    now = now or timezone.now()
    # Red de seguridad para llaves previas a `usada_en`: cualquier avance del
    # alumno en el curso (lección completada o prueba) cuenta como uso.
    avance = EstudianteLeccion.objects.filter(
        estudiante_id=OuterRef('estudiante_id'), leccion__curso_id=OuterRef('curso_id'),
    )
    pruebas = Prueba.objects.filter(
        estudiante_id=OuterRef('estudiante_id'), curso_id=OuterRef('curso_id'),
    )
    inscripciones = (
        EstudianteCurso.objects
        .filter(access_key_id__origen='key', access_key_id__escuela__isnull=True,
                access_key_id__llaves__gt=0, access_key_id__cobrada_a__isnull=False,
                access_key_id__devuelta_en__isnull=True, access_key_id__usada_en__isnull=True,
                access_key_id__valid_until__isnull=False, access_key_id__valid_until__lt=now,
                estudiante_id__escuela__isnull=False)
        .exclude(Exists(avance))
        .exclude(Exists(pruebas))
    )
    if escuela_id is not None:
        inscripciones = inscripciones.filter(access_key_id__cobrada_a_id=escuela_id)
    return AccessKey.objects.filter(pk__in=inscripciones.values('access_key_id'))


def devolver_llaves_no_usadas(now=None, escuela_id=None, dry_run=False, avisar=True):
    """Devuelve al saldo las llaves vencidas que el estudiante nunca usó.

    Idempotente (`devuelta_en`). Por llave: suma `llaves` a `cobrada_a.basic_key`,
    registra `MovimientoLlaves(+llaves, 'devolucion')` y la deja `revoked`. La
    inscripción se conserva: activar de nuevo el curso la reutiliza. Devuelve
    [{access_key_id, escuela_id, llaves, estudiante, curso}]; con `dry_run`
    solo lista.
    """
    now = now or timezone.now()
    resultados = []
    ids = list(llaves_devolvibles(now, escuela_id).values_list('pk', flat=True))
    for pk in ids:
        with transaction.atomic():
            ak = (AccessKey.objects.select_for_update()
                  .filter(pk=pk, devuelta_en__isnull=True, usada_en__isnull=True).first())
            if ak is None or not ak.llaves or not ak.cobrada_a_id:
                continue
            ec = (EstudianteCurso.objects.select_related('estudiante_id', 'curso_id')
                  .filter(access_key_id=ak).first())
            if ec is None or not ec.estudiante_id.escuela_id:
                continue
            est = ec.estudiante_id
            resultados.append({
                'access_key_id': str(ak.pk), 'escuela_id': ak.cobrada_a_id, 'llaves': ak.llaves,
                'estudiante': f"{est.nombre} {est.apellido}".strip() or est.email,
                'curso': ec.curso_id.nombre,
            })
            if dry_run:
                continue
            Escuela.objects.filter(pk=ak.cobrada_a_id).update(basic_key=F('basic_key') + ak.llaves)
            registrar_movimiento_llaves(ak.cobrada_a_id, ak.llaves, 'devolucion')
            ak.status = 'revoked'
            ak.devuelta_en = now
            ak.save(update_fields=['status', 'devuelta_en'])
    if resultados and avisar and not dry_run:
        _avisar_devolucion(resultados)
    return resultados


def _avisar_devolucion(resultados):
    """Aviso in-app (best-effort) a los directores de cada escuela."""
    from collections import defaultdict
    from accounts.models import Usuario
    from accounts.notifications import notificar
    por_escuela = defaultdict(list)
    for r in resultados:
        por_escuela[r['escuela_id']].append(r)
    for escuela_id, filas in por_escuela.items():
        total = sum(f['llaves'] for f in filas)
        detalle = "; ".join(f"{f['estudiante']} ({f['curso']})" for f in filas[:10])
        if len(filas) > 10:
            detalle += f" y {len(filas) - 10} más"
        mensaje = (
            f"Volvieron {total} llave(s) a tu saldo: estos estudiantes no abrieron el curso "
            f"antes de que venciera su acceso: {detalle}."
        )
        for director in Usuario.objects.filter(escuela_id=escuela_id, is_director=True):
            try:
                notificar(director, 'info', 'Llaves devueltas', mensaje,
                          data={'llaves': total, 'access_keys': [f['access_key_id'] for f in filas]})
            except Exception:  # noqa: BLE001 — el aviso no debe deshacer la devolución
                logger.exception("No se pudo avisar la devolución de llaves a %s", director.pk)


def _aplicar_efectos_a_escuela(escuela_id: int, producto, is_director: bool):
    """Suma las llaves de un paquete al saldo de la escuela (lock de fila).

    Las suscripciones ya no son `Producto`: se venden como `PlanSuscripcion`
    (ver `sales/suscripciones.py`). Un producto viejo con `basic_access` no
    otorga nada (están desactivados desde la migración 0031).
    """
    if not is_director or escuela_id is None:
        return
    if producto.cant_basic_key and producto.cant_basic_key > 0:
        escuela_locked = Escuela.objects.select_for_update().get(pk=escuela_id)
        escuela_locked.basic_key += producto.cant_basic_key
        escuela_locked.save(update_fields=['basic_key'])


@transaction.atomic
def registrar_venta_transbank(*, user, producto, escuela, result, token_ws, fecha_venta=None):
    """Persiste la Venta + TransbankTransaction y aplica efectos a la escuela."""
    venta = Venta.objects.create(
        usuario=user,
        escuela=escuela,
        producto=producto,
        monto_pagado=result['amount'],
        pay_system="WEBPAY",
        payment_status=result['status'],
        fecha_venta=fecha_venta,
        item_type='producto',
        buy_order=result.get('buy_order'),
    )
    TransbankTransaction.objects.create(
        sale=venta,
        transaction_date=result['transaction_date'],
        payment_type_code=result['payment_type_code'],
        token=token_ws,
        buy_order=result['buy_order'],
        status=result['status'],
        amount=result['amount'],
    )
    _aplicar_efectos_a_escuela(
        escuela.pk if escuela else None, producto, user.is_director
    )
    return venta


class CompraCursoError(Exception):
    """Error de negocio al registrar la compra individual de un curso."""
    def __init__(self, message, code='compra_error'):
        super().__init__(message)
        self.code = code


@transaction.atomic
def registrar_compra_curso_individual(*, user, method, result, fecha_venta=None,
                                      dias=None):
    """Registra la compra individual de un curso por un estudiante.

    Otorga acceso por un plan de 7/14/35 días (1/2/5 llaves), derivado del
    MONTO pagado: crea una AccessKey(origen='purchase') con expiración, la
    inscripción del estudiante (EstudianteCurso) y la Venta (con `curso`, sin
    `producto`).

    Seguridad: el curso y el plan (días) se derivan del `result['buy_order']`
    AUTORITATIVO devuelto por Transbank (no de datos que el cliente pueda
    manipular en la confirmación), y el monto se revalida contra el precio del
    plan (PlanCurso). Así no se puede pagar un monto arbitrario y reclamar más
    días ni otro plan.

    Raise CompraCursoError en cualquier violación.
    """
    # El buy_order autoritativo viene en extra_data (flujo unificado); fallback
    # al nivel superior por compatibilidad.
    buy_order = (result.get('extra_data') or {}).get('buy_order') or result.get('buy_order') or ''
    curso_id, student_id = extract_ids_from_buy_order(buy_order)
    if curso_id is None:
        raise CompraCursoError("buy_order inválido en la confirmación.", 'bad_buy_order')

    # El comprador debe ser quien inició el pago.
    if student_id != user.id:
        raise CompraCursoError("El comprador no coincide con el pago.", 'owner_mismatch')

    try:
        curso = Curso.objects.get(pk=curso_id)
    except Curso.DoesNotExist:
        raise CompraCursoError("Curso no existe.", 'curso_not_found')

    # El plan (días) lo dicta el buy_order (contrato estricto: siempre lo lleva).
    # El monto debe coincidir EXACTO con el precio del plan activo. Anti-tampering.
    dias = extract_dias_from_buy_order(buy_order)
    if dias is None:
        raise CompraCursoError("buy_order de curso sin plan (días).", 'bad_buy_order')
    precio = precio_plan(curso, dias)
    if precio is None:
        raise CompraCursoError("Plan de acceso no disponible para este curso.", 'plan_not_found')
    if int(round(float(result.get('amount') or 0))) != precio:
        raise CompraCursoError(
            "El monto cobrado no coincide con el precio del plan.", 'price_mismatch',
        )

    # ¿Ya inscrito? Distinguimos renovación de compra nueva:
    #  - Compra propia (origen='purchase') VENCIDA → renovar: extiende +dias.
    #  - Compra propia VIGENTE → ya tiene acceso (no cobrar de nuevo).
    #  - Acceso de escuela (key/seat) → lo gestiona la escuela, no se renueva
    #    con pago individual.
    existing = (
        EstudianteCurso.objects
        .select_related('access_key_id')
        .filter(estudiante_id=user, curso_id=curso)
        .first()
    )
    if existing is not None:
        ak = existing.access_key_id
        if ak and ak.origen == 'purchase' and not ak.is_valid():
            # Renovación: reactiva y extiende desde ahora.
            ak.valid_from = timezone.now()
            ak.valid_until = timezone.now() + timedelta(days=dias)
            ak.status = 'active'
            ak.save(update_fields=['valid_from', 'valid_until', 'status'])
            access_key = ak
        elif ak and ak.origen == 'purchase':
            raise CompraCursoError("Ya tienes acceso vigente a este curso.", 'already_enrolled')
        else:
            raise CompraCursoError(
                "Este curso lo gestiona tu escuela; contáctala para renovar.",
                'managed_by_school',
            )
    else:
        access_key = AccessKey.objects.create(
            valid_until=timezone.now() + timedelta(days=dias),
            origen='purchase',
        )
        EstudianteCurso.objects.create(
            estudiante_id=user, curso_id=curso, access_key_id=access_key,
        )

    venta = Venta.objects.create(
        usuario=user,
        curso=curso,
        producto=None,
        escuela=user.escuela,  # informativo; null si el estudiante no tiene escuela
        monto_pagado=result['amount'],
        pay_system=method.upper(),
        payment_status=result['status'],
        fecha_venta=fecha_venta,
        item_type='curso',
        dias=dias,
        buy_order=buy_order or None,
    )
    if method == 'transbank':
        extra = result['extra_data']
        TransbankTransaction.objects.create(
            sale=venta,
            transaction_date=result['transaction_date'],
            payment_type_code=extra['payment_type_code'],
            token=extra['token'],
            buy_order=extra['buy_order'],
            status=result['status'],
            amount=result['amount'],
        )
    return venta


@transaction.atomic
def registrar_venta_unificada(*, user, producto, escuela, method, result, fecha_venta=None):
    """Variante del flujo unificado: persiste Venta + transacción específica."""
    venta = Venta.objects.create(
        usuario=user,
        escuela=escuela,
        producto=producto,
        monto_pagado=result["amount"],
        pay_system=method.upper(),
        payment_status=result["status"],
        fecha_venta=fecha_venta,
        item_type="producto",
        buy_order=(result.get("extra_data") or {}).get("buy_order") or result.get("buy_order"),
    )
    if method == "transbank":
        extra = result["extra_data"]
        TransbankTransaction.objects.create(
            sale=venta,
            transaction_date=result["transaction_date"],
            payment_type_code=extra["payment_type_code"],
            token=extra["token"],
            buy_order=extra["buy_order"],
            status=result["status"],
            amount=result["amount"],
        )
    _aplicar_efectos_a_escuela(
        escuela.pk if escuela else None, producto, user.is_director
    )
    return venta


# ============================================================
# Intentos de pago (conversión) — ver sales.models.IntentoPago
# ============================================================
logger = logging.getLogger(__name__)

# Un intento iniciado sin confirmar tras este plazo se considera abandonado.
INTENTO_ABANDONO = timedelta(hours=2)


def registrar_intento_pago(*, usuario, buy_order, token, item_type, monto, pay_system):
    """Registra el inicio de un pago. Nunca interrumpe el pago si falla."""
    try:
        item_id, _ = extract_ids_from_buy_order(buy_order)
        es_curso = item_type == 'curso'
        curso = Curso.objects.filter(pk=item_id).first() if es_curso and item_id else None
        producto = Producto.objects.filter(pk=item_id).first() if not es_curso and item_id else None
        return IntentoPago.objects.create(
            buy_order=buy_order,
            token=token,
            usuario=usuario,
            escuela=getattr(usuario, 'escuela', None),
            item_type='curso' if es_curso else 'producto',
            producto=producto,
            curso=curso,
            dias=extract_dias_from_buy_order(buy_order) if es_curso else None,
            monto=int(round(float(monto or 0))),
            pay_system=(pay_system or '').upper(),
        )
    except Exception:  # pragma: no cover - el registro es best-effort
        logger.exception("No se pudo registrar el intento de pago %s", buy_order)
        return None


def resolver_intento_pago(token, *, estado, codigo=None, venta=None):
    """Cierra el intento iniciado de ese token (autorizado o rechazado)."""
    if not token:
        return 0
    try:
        return IntentoPago.objects.filter(token=token, estado='iniciado').update(
            estado=estado,
            codigo_respuesta=(str(codigo)[:50] if codigo is not None else None),
            venta=venta,
            resuelto_en=timezone.now(),
        )
    except Exception:  # pragma: no cover
        logger.exception("No se pudo resolver el intento de pago %s", token)
        return 0


def estado_intento(intento, now=None):
    """Estado para mostrar: un 'iniciado' viejo se reporta como 'abandonado'."""
    now = now or timezone.now()
    if intento.estado == 'iniciado' and intento.creado_en <= now - INTENTO_ABANDONO:
        return 'abandonado'
    return intento.estado
