"""Suscripciones de escuelas: períodos, cambios de plan y cupos por alumno.

Reglas (ver `CLAUDE.md` → Suscripciones):
- Prepago por período (1/6/12 meses). Renovar con la suscripción vigente
  extiende desde `fin`; si no, el período parte hoy.
- Estado por fecha (`Suscripcion.estado`): vigente → gracia (7 d, con acceso)
  → suspendida (30 d, sin acceso, cupos guardados) → vencida (cupos liberados
  por la tarea diaria).
- Un cupo = un estudiante con acceso a todos los cursos mientras la
  suscripción tenga acceso. Se libera y reasigna sin costo.
- Subir de plan (más caro): se cobra la diferencia prorrateada por los días
  que quedan y aplica al confirmar el pago. Bajar (más barato): queda
  programado para la renovación. Ningún cambio puede dejar menos cupos que
  alumnos con cupo activo.
"""
import calendar
import math

from django.db import transaction
from django.utils import timezone

from schools.models import Curso, Escuela

from .models import (
    GRACIA_DIAS, AccessKey, CupoSuscripcion, EstudianteCurso, PeriodoSuscripcion, PlanSuscripcion,
    Suscripcion,
)

PERIODOS_VALIDOS = (1, 6, 12)
RANGO_TIER = {'esencial': 1, 'pro': 2}

# Funciones habilitadas por tier. Gating único: el backend las expone en el
# endpoint de la escuela y los permisos las consultan con `tiene_funcion`.
FUNCIONES_SUSCRIPCION = ('cupos', 'reasignar_cupos', 'soporte_prioritario', 'onboarding')
FUNCIONES_PRO = ('sedes', 'varios_directores')


class SuscripcionError(Exception):
    """Error de negocio de suscripciones (mensaje para el usuario)."""
    def __init__(self, message, code='suscripcion_error'):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- fechas

def sumar_meses(dt, n):
    m = dt.month - 1 + int(n)
    y = dt.year + m // 12
    m = m % 12 + 1
    return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))


def _segundos(a, b):
    return max(0.0, (b - a).total_seconds())


# ---------------------------------------------------------------- lectura

def normalizar(sus, now=None):
    """Aplica un cambio de plan ya pagado cuando llega su fecha (`cambio_en`)."""
    now = now or timezone.now()
    if sus.plan_siguiente_id and sus.cambio_en and now >= sus.cambio_en:
        sus.plan_id = sus.plan_siguiente_id
        sus.periodo_meses = sus.periodo_siguiente or sus.periodo_meses
        sus.periodo_inicio = sus.cambio_en
        sus.precio_periodo = sus.precio_siguiente or 0
        sus.plan_siguiente = None
        sus.periodo_siguiente = None
        sus.precio_siguiente = None
        sus.cambio_en = None
        sus.save()
    return sus


def suscripcion_de(escuela, now=None):
    """`Suscripcion` de la escuela (normalizada) o None."""
    if escuela is None:
        return None
    escuela_id = escuela if isinstance(escuela, int) else escuela.pk
    sus = Suscripcion.objects.select_related('plan', 'plan_siguiente').filter(escuela_id=escuela_id).first()
    return normalizar(sus, now) if sus else None


def con_acceso(escuela, now=None):
    sus = suscripcion_de(escuela, now)
    return bool(sus and sus.con_acceso(now))


def cupos_activos(sus):
    return CupoSuscripcion.objects.filter(suscripcion=sus, liberado_en__isnull=True)


def cupo_activo(estudiante):
    return (
        CupoSuscripcion.objects.select_related('suscripcion', 'suscripcion__plan')
        .filter(estudiante=estudiante, liberado_en__isnull=True).first()
    )


def cupo_con_acceso(estudiante, now=None):
    """El cupo activo del estudiante si su suscripción da acceso hoy, si no None."""
    cupo = cupo_activo(estudiante)
    if cupo is None:
        return None
    sus = normalizar(cupo.suscripcion, now)
    return cupo if sus.con_acceso(now) else None


def fin_acceso(sus):
    """Hasta cuándo dan acceso los cupos (fin + gracia)."""
    from datetime import timedelta
    return sus.fin + timedelta(days=GRACIA_DIAS)


def funciones_de(escuela, now=None):
    """Funciones habilitadas para la escuela según su suscripción."""
    sus = suscripcion_de(escuela, now)
    if not sus or not sus.con_acceso(now):
        return []
    funciones = list(FUNCIONES_SUSCRIPCION)
    if sus.plan.tier == 'pro':
        funciones += FUNCIONES_PRO
    return funciones


def tiene_funcion(escuela, funcion, now=None):
    return funcion in funciones_de(escuela, now)


def resumen(escuela, now=None):
    """Estado de la suscripción para el panel (dict serializable) o None."""
    now = now or timezone.now()
    sus = suscripcion_de(escuela, now)
    if sus is None:
        return None
    usados = cupos_activos(sus).count()
    return {
        'id': sus.id,
        'estado': sus.estado(now),
        'plan': plan_dict(sus.plan),
        'periodo_meses': sus.periodo_meses,
        'inicio': sus.inicio,
        'periodo_inicio': sus.periodo_inicio,
        'fin': sus.fin,
        'fin_acceso': fin_acceso(sus),
        'precio_periodo': sus.precio_periodo,
        'cupos_usados': usados,
        'cupos_disponibles': max(0, sus.plan.cupos - usados),
        'cambio': (
            {
                'plan': plan_dict(sus.plan_siguiente),
                'periodo_meses': sus.periodo_siguiente,
                'desde': sus.cambio_en,
                'pagado': bool(sus.cambio_en),
            }
            if sus.plan_siguiente_id else None
        ),
        'no_renovara': sus.no_renovara,
        'funciones': funciones_de(escuela, now),
    }


def plan_dict(plan):
    if plan is None:
        return None
    return {
        'id': plan.id,
        'tier': plan.tier,
        'nombre': plan.nombre,
        'cupos': plan.cupos,
        'precio_mensual': plan.precio_mensual,
        'precio_semestral': plan.precio_semestral,
        'precio_anual': plan.precio_anual,
        'activo': plan.activo,
    }


# ---------------------------------------------------------------- espejo

def sincronizar_espejo(escuela_id, now=None):
    """Mantiene `Escuela.basic_*` (compatibilidad) derivados de la suscripción."""
    sus = suscripcion_de(escuela_id, now)
    if sus is None:
        valores = dict(basic_access=False, basic_seats_max=0, basic_seats_used=0, basic_access_until=None)
    else:
        valores = dict(
            basic_access=sus.con_acceso(now),
            basic_seats_max=sus.plan.cupos,
            basic_seats_used=cupos_activos(sus).count(),
            basic_access_until=sus.fin,
        )
    Escuela.objects.filter(pk=escuela_id).update(**valores)


# ---------------------------------------------------------------- precios

def _validar_periodo(meses):
    try:
        meses = int(meses)
    except (TypeError, ValueError):
        meses = None
    if meses not in PERIODOS_VALIDOS:
        raise SuscripcionError("El período debe ser mensual, semestral o anual.", 'periodo_invalido')
    return meses


def plan_activo(plan_id):
    plan = PlanSuscripcion.objects.filter(pk=plan_id, activo=True).first()
    if plan is None:
        raise SuscripcionError("Ese plan no está disponible.", 'plan_inactivo')
    return plan


def es_subida(actual, nuevo, meses):
    """True si pasar de `actual` a `nuevo` cuesta más (se cobra prorrateado)."""
    return nuevo.id != actual.id and nuevo.precio(meses) > actual.precio(meses)


def _validar_cupos(sus, plan):
    if sus is None:
        return
    usados = cupos_activos(sus).count()
    if plan.cupos < usados:
        raise SuscripcionError(
            f"El plan {plan.nombre} tiene {plan.cupos} cupos y hoy usas {usados}. "
            f"Libera {usados - plan.cupos} antes de cambiar.",
            'cupos_insuficientes',
        )


def cotizar(escuela, plan, meses, now=None):
    """Qué compra el director con (plan, meses) hoy y cuánto cuesta.

    Devuelve dict con `tipo` ∈ alta | renovacion | subida | cambio_renovacion,
    `monto` (CLP), y para la subida los días que quedan. Lanza
    `SuscripcionError` si la operación no corresponde.
    """
    now = now or timezone.now()
    meses = _validar_periodo(meses)
    sus = suscripcion_de(escuela, now)
    if sus is None or sus.estado(now) == 'vencida':
        return {'tipo': 'alta', 'monto': plan.precio(meses), 'plan': plan, 'meses': meses}
    _validar_cupos(sus, plan)
    if sus.estado(now) != 'vigente':
        # En gracia o suspendida: renovar parte hoy, con el plan que elija.
        return {'tipo': 'renovacion', 'monto': plan.precio(meses), 'plan': plan, 'meses': meses}
    if plan.id == sus.plan_id and meses == sus.periodo_meses:
        if sus.cambio_en:
            raise SuscripcionError(
                "Ya pagaste la próxima renovación con otro plan; podrás renovar cuando empiece.",
                'renovacion_pendiente',
            )
        return {'tipo': 'renovacion', 'monto': plan.precio(meses), 'plan': plan, 'meses': meses}
    if sus.cambio_en:
        raise SuscripcionError(
            "Ya tienes un cambio de plan pagado para la próxima renovación.", 'renovacion_pendiente',
        )
    if meses == sus.periodo_meses and es_subida(sus.plan, plan, meses):
        return {'tipo': 'subida', 'plan': plan, 'meses': meses, **prorrateo(sus, plan, now)}
    # Más barato o con otro período: se paga ahora y aplica desde el fin.
    return {'tipo': 'cambio_renovacion', 'monto': plan.precio(meses), 'plan': plan, 'meses': meses}


def prorrateo(sus, plan, now=None):
    """Diferencia a pagar por subir a `plan` por los días que quedan.

    Tarifa diaria pagada = precio_periodo / largo del tramo pagado; tarifa
    nueva = precio del plan nuevo / largo de un período. Se cobra la
    diferencia por los días restantes, redondeada a $10 hacia arriba.
    """
    now = now or timezone.now()
    total = _segundos(sus.periodo_inicio, sus.fin) or 1
    restante = _segundos(now, sus.fin)
    largo_periodo = _segundos(sus.periodo_inicio, sumar_meses(sus.periodo_inicio, sus.periodo_meses)) or 1
    tarifa_actual = sus.precio_periodo / total
    tarifa_nueva = plan.precio(sus.periodo_meses) / largo_periodo
    monto = max(0.0, (tarifa_nueva - tarifa_actual) * restante)
    return {
        'monto': int(math.ceil(monto / 10.0) * 10),
        'dias_restantes': int(restante // 86400),
    }


# ---------------------------------------------------------------- escritura

@transaction.atomic
def aplicar_compra(escuela, plan, meses, monto, *, venta=None, usuario=None, now=None):
    """Aplica un pago confirmado de suscripción (alta/renovación/subida/cambio).

    Recalcula el tipo con `cotizar` en el momento de aplicar; `monto` es lo
    efectivamente cobrado (ya validado por el flujo de pago).
    """
    now = now or timezone.now()
    Escuela.objects.select_for_update().get(pk=escuela.pk)
    cot = cotizar(escuela, plan, meses, now)
    meses = cot['meses']
    sus = suscripcion_de(escuela, now)
    tipo = cot['tipo']

    if tipo == 'alta':
        if sus is None:
            sus = Suscripcion(escuela=escuela, inicio=now)
        else:
            # Vencida: contrato nuevo sobre la misma fila; los cupos ya se liberaron.
            cupos_activos(sus).update(liberado_en=now)
            sus.inicio = now
        _nuevo_periodo(sus, plan, meses, now, monto)
        motivo = 'alta'
    elif tipo == 'renovacion':
        if sus.estado(now) == 'vigente':
            sus.fin = sumar_meses(sus.fin, meses)
            sus.precio_periodo += monto
        else:
            _nuevo_periodo(sus, plan, meses, now, monto)
        motivo = 'renovacion'
    elif tipo == 'subida':
        sus.plan = plan
        sus.precio_periodo += monto
        motivo = 'subida'
    else:  # cambio_renovacion: plan nuevo pagado desde el fin actual
        sus.plan_siguiente = plan
        sus.periodo_siguiente = meses
        sus.precio_siguiente = monto
        sus.cambio_en = sus.fin
        motivo = 'bajada' if plan.precio(meses) < sus.plan.precio(meses) else 'renovacion'
        inicio_cambio = sus.fin
        sus.fin = sumar_meses(sus.fin, meses)

    sus.no_renovara = False
    if tipo != 'cambio_renovacion' and sus.plan_siguiente_id and not sus.cambio_en:
        # Una renovación con el plan programado consume la bajada programada.
        if sus.plan_siguiente_id == plan.id:
            sus.plan_siguiente = None
            sus.periodo_siguiente = None
    sus.save()
    PeriodoSuscripcion.objects.create(
        suscripcion=sus, plan=plan, periodo_meses=meses,
        inicio=inicio_cambio if tipo == 'cambio_renovacion' else (now if tipo == 'subida' else sus.periodo_inicio),
        fin=sus.fin, monto=monto, venta=venta, motivo=motivo, usuario=usuario,
    )
    sincronizar_espejo(escuela.pk, now)
    return sus


def _nuevo_periodo(sus, plan, meses, now, monto):
    sus.plan = plan
    sus.periodo_meses = meses
    sus.periodo_inicio = now
    sus.fin = sumar_meses(now, meses)
    sus.precio_periodo = monto
    sus.cambio_en = None
    sus.precio_siguiente = None


@transaction.atomic
def programar_bajada(escuela, plan, meses, now=None):
    """Bajada sin pago: el plan más barato se usará al renovar."""
    now = now or timezone.now()
    meses = _validar_periodo(meses)
    sus = suscripcion_de(escuela, now)
    if sus is None or not sus.con_acceso(now):
        raise SuscripcionError("No tienes una suscripción activa.", 'sin_suscripcion')
    if sus.cambio_en:
        raise SuscripcionError("Ya tienes un cambio de plan pagado para la próxima renovación.",
                               'renovacion_pendiente')
    if plan.id == sus.plan_id and meses == sus.periodo_meses:
        raise SuscripcionError("Ese ya es tu plan.", 'mismo_plan')
    if es_subida(sus.plan, plan, meses) and meses == sus.periodo_meses:
        raise SuscripcionError("Ese plan es una subida: se paga la diferencia y aplica de inmediato.",
                               'es_subida')
    sus.plan_siguiente = plan
    sus.periodo_siguiente = meses
    sus.save(update_fields=['plan_siguiente', 'periodo_siguiente', 'actualizada'])
    return sus


@transaction.atomic
def cancelar_cambio(escuela, now=None):
    sus = suscripcion_de(escuela, now)
    if sus is None or not sus.plan_siguiente_id:
        raise SuscripcionError("No hay un cambio programado.", 'sin_cambio')
    if sus.cambio_en:
        raise SuscripcionError("Ese cambio ya está pagado y no se puede deshacer.", 'cambio_pagado')
    sus.plan_siguiente = None
    sus.periodo_siguiente = None
    sus.save(update_fields=['plan_siguiente', 'periodo_siguiente', 'actualizada'])
    return sus


def marcar_no_renovar(escuela, no_renovara, motivo=''):
    sus = suscripcion_de(escuela)
    if sus is None:
        raise SuscripcionError("No tienes una suscripción.", 'sin_suscripcion')
    sus.no_renovara = bool(no_renovara)
    sus.motivo_no_renovar = (motivo or '').strip()[:1000] if no_renovara else ''
    sus.save(update_fields=['no_renovara', 'motivo_no_renovar', 'actualizada'])
    return sus


@transaction.atomic
def cortesia(escuela, *, dias=None, plan=None, meses=None, usuario=None, now=None):
    """Regalo del admin: extiende `dias` o da un período de `plan` sin cobro."""
    from datetime import timedelta
    now = now or timezone.now()
    sus = suscripcion_de(escuela, now)
    if plan is not None:
        meses = _validar_periodo(meses or 1)
        if sus is None or sus.estado(now) == 'vencida':
            if sus is None:
                sus = Suscripcion(escuela=escuela, inicio=now)
            _nuevo_periodo(sus, plan, meses, now, 0)
        else:
            _validar_cupos(sus, plan)
            base = sus.fin if sus.estado(now) == 'vigente' else now
            if sus.estado(now) != 'vigente':
                sus.periodo_inicio = now
            sus.plan = plan
            sus.periodo_meses = meses
            sus.fin = sumar_meses(base, meses)
    else:
        try:
            dias = int(dias)
        except (TypeError, ValueError):
            dias = 0
        if dias <= 0:
            raise SuscripcionError("Indica los días a regalar.", 'dias_invalidos')
        if sus is None:
            raise SuscripcionError("La escuela no tiene suscripción; regala un plan.", 'sin_suscripcion')
        if sus.estado(now) == 'vigente':
            sus.fin = sus.fin + timedelta(days=dias)
        else:
            sus.periodo_inicio = now
            sus.fin = now + timedelta(days=dias)
    sus.save()
    PeriodoSuscripcion.objects.create(
        suscripcion=sus, plan=sus.plan, periodo_meses=sus.periodo_meses,
        inicio=now, fin=sus.fin, monto=0, motivo='cortesia', usuario=usuario,
    )
    sincronizar_espejo(escuela.pk, now)
    return sus


# ---------------------------------------------------------------- cupos

@transaction.atomic
def asignar_cupo(escuela, estudiante, *, por=None, now=None):
    """Da (o reutiliza) un cupo al estudiante. Lanza `SuscripcionError`."""
    now = now or timezone.now()
    Escuela.objects.select_for_update().get(pk=escuela.pk)
    sus = suscripcion_de(escuela, now)
    if sus is None or not sus.con_acceso(now):
        raise SuscripcionError("Tu escuela no tiene una suscripción activa.", 'sin_suscripcion')
    if estudiante.escuela_id not in (None, escuela.pk):
        raise SuscripcionError("El estudiante pertenece a otra escuela.", 'otra_escuela')
    existente = cupo_activo(estudiante)
    if existente is not None:
        if existente.suscripcion_id == sus.id:
            return existente
        raise SuscripcionError("El estudiante ya ocupa un cupo en otra escuela.", 'otra_escuela')
    if cupos_activos(sus).count() >= sus.plan.cupos:
        raise SuscripcionError("No te quedan cupos libres en tu plan.", 'sin_cupos')
    cupo = CupoSuscripcion.objects.create(suscripcion=sus, estudiante=estudiante, asignado_en=now, asignado_por=por)
    sincronizar_espejo(escuela.pk, now)
    return cupo


@transaction.atomic
def liberar_cupo(estudiante, now=None):
    """Libera el cupo del estudiante (pierde el acceso por cupo, conserva su avance)."""
    now = now or timezone.now()
    cupo = cupo_activo(estudiante)
    if cupo is None:
        return None
    cupo.liberado_en = now
    cupo.save(update_fields=['liberado_en'])
    AccessKey.objects.filter(
        origen='seat', status='active', estudiantecurso__estudiante_id=estudiante,
    ).update(status='revoked')
    sincronizar_espejo(cupo.suscripcion.escuela_id, now)
    return cupo


def liberar_cupos_vencidos(now=None):
    """Libera los cupos de suscripciones `vencida`. Devuelve los ids de escuela."""
    now = now or timezone.now()
    liberados = []
    for sus in Suscripcion.objects.select_related('plan').filter(cupos__liberado_en__isnull=True).distinct():
        normalizar(sus, now)
        if sus.estado(now) != 'vencida':
            continue
        for cupo in cupos_activos(sus).select_related('estudiante'):
            liberar_cupo(cupo.estudiante, now)
        liberados.append(sus.escuela_id)
    return liberados


def asegurar_inscripcion_cupo(estudiante, curso, now=None):
    """Crea (o reactiva) la inscripción del estudiante con cupo en `curso`.

    El avance y "mis cursos" se apoyan en `EstudianteCurso`; con cupo el
    estudiante tiene acceso a todos los cursos, así que la inscripción se
    crea la primera vez que abre uno. Devuelve la inscripción o None si el
    estudiante no tiene cupo con acceso.
    """
    if cupo_con_acceso(estudiante, now) is None:
        return None
    curso_id = curso if isinstance(curso, int) else curso.pk
    with transaction.atomic():
        ec = EstudianteCurso.objects.select_related('access_key_id').filter(
            estudiante_id=estudiante, curso_id_id=curso_id,
        ).first()
        if ec is not None and ec.access_key_id.is_valid():
            return ec
        key = AccessKey.objects.create(valid_until=None, origen='seat')
        if ec is not None:
            ec.access_key_id = key
            ec.save(update_fields=['access_key_id'])
            return ec
        return EstudianteCurso.objects.create(
            estudiante_id=estudiante, curso_id=Curso.objects.get(pk=curso_id), access_key_id=key,
        )


# ---------------------------------------------------------------- pago

PREFIJO_BUY_ORDER = 'sus'
PERIODOS_TRANSFERENCIA = (6, 12)
ESTADO_TRANSFERENCIA_PENDIENTE = 'pendiente'


def buy_order_suscripcion(plan_id, meses, user_id):
    """`sus_<plan>_<meses>_<usuario>` (≤ 26 caracteres, límite de Webpay)."""
    return f"{PREFIJO_BUY_ORDER}_{int(plan_id)}_{int(meses)}_{int(user_id)}"


def parse_buy_order(buy_order):
    """(plan_id, meses, user_id) de un buy_order de suscripción, o None."""
    if not isinstance(buy_order, str):
        return None
    partes = buy_order.split('_')
    if len(partes) != 4 or partes[0] != PREFIJO_BUY_ORDER:
        return None
    try:
        plan_id, meses, user_id = (int(x) for x in partes[1:])
    except (TypeError, ValueError):
        return None
    return plan_id, meses, user_id


def item_type_de(cotizacion):
    return 'subida' if cotizacion['tipo'] == 'subida' else 'suscripcion'


def registrar_intento(*, usuario, buy_order, token, cotizacion, pay_system):
    """Intento de pago de una suscripción: guarda el monto cotizado (en una
    subida es el prorrateo de ese momento) para validar la confirmación."""
    from .models import IntentoPago
    return IntentoPago.objects.create(
        buy_order=buy_order,
        token=token,
        usuario=usuario,
        escuela=getattr(usuario, 'escuela', None),
        item_type=item_type_de(cotizacion),
        plan_suscripcion=cotizacion['plan'],
        periodo_meses=cotizacion['meses'],
        monto=int(cotizacion['monto']),
        pay_system=(pay_system or '').upper(),
    )


@transaction.atomic
def registrar_venta_webpay(*, user, method, result):
    """Confirma un pago de suscripción autorizado por Webpay.

    El plan, el período y el comprador salen del buy_order autoritativo; el
    monto cobrado debe coincidir con el cotizado al iniciar (`IntentoPago`).
    Crea la Venta + TransbankTransaction y aplica el período. Lanza
    `SuscripcionError` si algo no cuadra.
    """
    from .models import IntentoPago, TransbankTransaction, Venta
    extra = result.get('extra_data') or {}
    datos = parse_buy_order(extra.get('buy_order'))
    if datos is None:
        raise SuscripcionError("buy_order de suscripción inválido.", 'buy_order_invalido')
    plan_id, meses, _ = datos
    intento = IntentoPago.objects.filter(token=extra.get('token')).first()
    monto = int(round(float(result.get('amount') or 0)))
    if intento is None or intento.plan_suscripcion_id != plan_id or intento.monto != monto:
        raise SuscripcionError("El monto cobrado no coincide con la cotización.", 'monto_distinto')
    plan = PlanSuscripcion.objects.get(pk=plan_id)
    venta = Venta.objects.create(
        usuario=user,
        escuela=user.escuela,
        monto_pagado=monto,
        pay_system=method.upper(),
        payment_status=result['status'],
        item_type=intento.item_type,
        plan_suscripcion=plan,
        periodo_meses=meses,
        buy_order=extra.get('buy_order'),
    )
    TransbankTransaction.objects.create(
        sale=venta,
        transaction_date=result.get('transaction_date'),
        payment_type_code=extra.get('payment_type_code'),
        token=extra.get('token'),
        buy_order=extra.get('buy_order'),
        status=result['status'],
        amount=result['amount'],
    )
    aplicar_compra(user.escuela, plan, meses, monto, venta=venta, usuario=user)
    return venta


def datos_bancarios():
    import os
    return os.getenv('TRANSFERENCIA_DATOS', '').replace('\\n', '\n').strip()


@transaction.atomic
def solicitar_transferencia(escuela, plan, meses, usuario, now=None):
    """Orden de pago por transferencia (semestral/anual): Venta `pendiente`.

    Exige datos de facturación. El período se activa cuando el admin confirma
    el pago (`confirmar_transferencia`).
    """
    from .models import DatosFacturacion, Venta
    meses = _validar_periodo(meses)
    if meses not in PERIODOS_TRANSFERENCIA:
        raise SuscripcionError("La transferencia es solo para planes semestrales o anuales.", 'periodo_transferencia')
    if not DatosFacturacion.objects.filter(escuela=escuela).exists():
        raise SuscripcionError("Completa los datos de facturación antes de pedir la transferencia.", 'sin_facturacion')
    if Venta.objects.filter(escuela=escuela, payment_status=ESTADO_TRANSFERENCIA_PENDIENTE,
                            pay_system='TRANSFERENCIA').exists():
        raise SuscripcionError("Ya tienes una transferencia pendiente. Anúlala para pedir otra.", 'transferencia_pendiente')
    cot = cotizar(escuela, plan, meses, now)
    venta = Venta.objects.create(
        usuario=usuario,
        escuela=escuela,
        monto_pagado=cot['monto'],
        pay_system='TRANSFERENCIA',
        payment_status=ESTADO_TRANSFERENCIA_PENDIENTE,
        item_type=item_type_de(cot),
        plan_suscripcion=plan,
        periodo_meses=meses,
    )
    venta.buy_order = f"tr_{venta.id}"
    venta.save(update_fields=['buy_order'])
    _avisar(
        usuario, "Instrucciones para pagar por transferencia",
        f"Pediste el plan {plan.nombre} ({meses} meses) por ${cot['monto']:,}.".replace(',', '.')
        + f"\n\nTransfiere ese monto e indica la referencia {venta.buy_order} en el comentario."
        + (f"\n\n{datos_bancarios()}" if datos_bancarios() else "")
        + "\n\nActivaremos tu plan apenas confirmemos el pago y te enviaremos la factura.",
        {'evento': 'transferencia_pedida', 'venta_id': venta.id},
    )
    return venta


@transaction.atomic
def confirmar_transferencia(venta, admin, folio='', now=None):
    """El admin registra el pago recibido: activa el período de la venta."""
    from .models import Venta
    venta = Venta.objects.select_for_update().get(pk=venta.pk)
    if venta.payment_status != ESTADO_TRANSFERENCIA_PENDIENTE:
        raise SuscripcionError("Esta venta no está pendiente.", 'no_pendiente')
    venta.payment_status = 'paid'
    venta.fecha_venta = now or timezone.now()
    if folio:
        venta.folio_factura = folio.strip()[:40]
    venta.save(update_fields=['payment_status', 'fecha_venta', 'folio_factura'])
    aplicar_compra(venta.escuela, venta.plan_suscripcion, venta.periodo_meses, int(venta.monto_pagado),
                   venta=venta, usuario=admin, now=now)
    if venta.usuario:
        _avisar(venta.usuario, "Recibimos tu transferencia",
                f"Tu plan {venta.plan_suscripcion.nombre} ya está activo.",
                {'evento': 'transferencia_confirmada', 'venta_id': venta.id})
    return venta


@transaction.atomic
def anular_transferencia(venta):
    from .models import Venta
    venta = Venta.objects.select_for_update().get(pk=venta.pk)
    if venta.payment_status != ESTADO_TRANSFERENCIA_PENDIENTE:
        raise SuscripcionError("Esta venta no está pendiente.", 'no_pendiente')
    venta.payment_status = 'anulada'
    venta.save(update_fields=['payment_status'])
    return venta


def avisar_compra(venta):
    """Comprobante por email (y campana) tras un pago confirmado."""
    if not venta.usuario:
        return
    meses = {1: 'mensual', 6: 'semestral', 12: 'anual'}.get(venta.periodo_meses, '')
    sus = suscripcion_de(venta.escuela)
    hasta = timezone.localtime(sus.fin).strftime('%d-%m-%Y') if sus else ''
    _avisar(
        venta.usuario, "Pago de tu suscripción confirmado",
        f"Pagaste ${int(venta.monto_pagado):,}".replace(',', '.')
        + f" por el plan {venta.plan_suscripcion.nombre} {meses}.\nTu suscripción está vigente hasta el {hasta}."
        + "\nPuedes descargar el comprobante desde Llaves y plan → Historial.",
        {'evento': 'pago_confirmado', 'venta_id': venta.id},
    )


def _avisar(usuario, titulo, mensaje, data):
    from accounts.notifications import notificar
    try:
        notificar(usuario, 'suscripcion', titulo, mensaje, data=data, email=True)
    except Exception:  # pragma: no cover - el aviso es best-effort
        import logging
        logging.getLogger(__name__).exception("No se pudo avisar al usuario %s", getattr(usuario, 'id', None))


# ---------------------------------------------------------------- tarea diaria

DIAS_AVISO = (7, 3, 1)


def directores_de(escuela_id):
    from accounts.models import Usuario
    return list(Usuario.objects.filter(is_director=True, escuela_id=escuela_id, is_active=True))


def _una_vez(sus, clave, enviar):
    """Ejecuta `enviar()` solo si el aviso `clave` no se mandó antes."""
    from django.db import IntegrityError
    from .models import AvisoSuscripcion
    try:
        with transaction.atomic():
            AvisoSuscripcion.objects.create(suscripcion=sus, clave=clave[:60])
    except IntegrityError:
        return False
    enviar()
    return True


def _a_directores(sus, titulo, mensaje, evento):
    for d in directores_de(sus.escuela_id):
        _avisar(d, titulo, mensaje, {'evento': evento, 'suscripcion_id': sus.id})


def tareas_diarias(now=None):
    """Avisos de vencimiento, gracia y suspensión; libera cupos de suscripciones
    vencidas. Idempotente:
    se puede correr varias veces al día sin duplicar correos.

    Devuelve un resumen {avisos, cupos_liberados}.
    """
    from datetime import timedelta
    now = now or timezone.now()
    hoy = timezone.localdate(now)
    avisos = 0
    for sus in Suscripcion.objects.select_related('plan', 'escuela'):
        normalizar(sus, now)
        estado = sus.estado(now)
        fin_txt = timezone.localtime(sus.fin).strftime('%d-%m-%Y')
        sello = sus.fin.strftime('%Y%m%d')
        if estado == 'vigente' and not sus.no_renovara and not sus.cambio_en:
            dias = (timezone.localdate(sus.fin) - hoy).days
            # El aviso de N días se manda el primer día en que faltan ≤ N (y
            # solo el más cercano si la tarea no corrió algunos días).
            umbral = next((d for d in sorted(DIAS_AVISO) if dias <= d), None)
            if umbral is not None:
                avisos += _una_vez(sus, f"vence_{umbral}_{sello}", lambda d=dias: _a_directores(
                    sus, "Tu suscripción AutoTest vence pronto",
                    f"Tu plan {sus.plan.nombre} vence el {fin_txt} ({'mañana' if d <= 1 else f'en {d} días'})."
                    "\nRenueva desde Llaves y plan → Suscripción para que tus alumnos no pierdan el acceso.",
                    'vence_pronto',
                ))
        elif estado == 'gracia':
            hasta = timezone.localtime(fin_acceso(sus)).strftime('%d-%m-%Y')
            avisos += _una_vez(sus, f"gracia_{sello}", lambda: _a_directores(
                sus, "Tu suscripción venció: tienes 7 días de gracia",
                f"Tu plan {sus.plan.nombre} venció el {fin_txt}. Tus alumnos siguen estudiando hasta el {hasta}."
                "\nRenueva antes de esa fecha para que no pierdan el acceso.",
                'gracia',
            ))
        elif estado == 'suspendida':
            avisos += _una_vez(sus, f"suspendida_{sello}", lambda: _a_directores(
                sus, "Tu suscripción está suspendida",
                "Tus alumnos con cupo ya no pueden estudiar. Guardamos sus cupos por 30 días:"
                " si renuevas antes, vuelven sin que tengas que asignarlos de nuevo.",
                'suspendida',
            ))
    liberadas = liberar_cupos_vencidos(now)
    for escuela_id in liberadas:
        sus = Suscripcion.objects.get(escuela_id=escuela_id)
        avisos += _una_vez(sus, f"liberados_{sus.fin.strftime('%Y%m%d')}", lambda s=sus: _a_directores(
            s, "Liberamos los cupos de tu suscripción",
            "Tu suscripción terminó hace más de un mes y sus cupos quedaron libres."
            " Puedes contratar un plan nuevo cuando quieras desde Llaves y plan.",
            'cupos_liberados',
        ))
    return {'avisos': avisos, 'cupos_liberados': len(liberadas)}
