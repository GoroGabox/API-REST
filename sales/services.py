import math
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone
from django.db import transaction
from django.db.models import Q

from schools.models import Escuela, Curso
from .models import AccessKey, EstudianteCurso, Producto, Venta, TransbankTransaction
from .utils import extract_ids_from_buy_order

# Días de acceso que habilita 1 llave. Otorgar N días cuesta ceil(N/7) llaves.
DIAS_POR_LLAVE = 7
# Días de acceso que otorga la compra individual de un curso (equivale a 1 llave).
DIAS_COMPRA_INDIVIDUAL = DIAS_POR_LLAVE


def llaves_para_dias(dias) -> int:
    """Nº de llaves que cuesta habilitar `dias` de acceso (1 llave = 7 días).

    Fracciones se redondean hacia arriba: 7 días = 1 llave, 8 días = 2 llaves,
    35 días = 5 llaves. `dias <= 0` cuesta 0.
    """
    d = int(dias or 0)
    if d <= 0:
        return 0
    return math.ceil(d / DIAS_POR_LLAVE)


def cursos_con_acceso_vigente(user) -> set:
    """Ids de cursos a los que `user` (estudiante) tiene acceso VIGENTE.

    Acceso vigente = existe un EstudianteCurso cuya AccessKey está 'active' y
    dentro de su rango temporal. Los cupos de suscripción (valid_until=None) no
    expiran; las llaves/compras vencen en su `valid_until`. Es la fuente de
    verdad para gatear el contenido premium (lecciones, unidades, ejercicios).
    """
    now = timezone.now()
    qs = (
        EstudianteCurso.objects
        .filter(estudiante_id=user, access_key_id__status='active')
        .filter(Q(access_key_id__valid_from__isnull=True) | Q(access_key_id__valid_from__lte=now))
        .filter(Q(access_key_id__valid_until__isnull=True) | Q(access_key_id__valid_until__gte=now))
    )
    return set(qs.values_list('curso_id_id', flat=True))


def tiene_acceso_a_curso(user, curso_id) -> bool:
    """True si el estudiante tiene acceso vigente al curso indicado."""
    try:
        cid = int(curso_id)
    except (TypeError, ValueError):
        return False
    return cid in cursos_con_acceso_vigente(user)


def precio_final_curso(curso: Curso) -> int:
    """Precio autoritativo (CLP entero) de un curso individual: su `costo`."""
    return int(curso.costo or 0)


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
      - La llave se marca como 'used' tras canjearse (un solo uso).

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

    if EstudianteCurso.objects.filter(estudiante_id=estudiante, curso_id=curso).exists():
        raise CanjeError("Ya estás inscrito en este curso.", 'already_enrolled')

    inscripcion = EstudianteCurso.objects.create(
        estudiante_id=estudiante,
        curso_id=curso,
        access_key_id=access_key,
    )
    access_key.status = 'used'
    access_key.save(update_fields=['status'])
    return inscripcion


def asignar_llave_y_curso(estudiante, curso, dias):
    with transaction.atomic():
        access_key = AccessKey.objects.create(
            valid_until=timezone.now() + timezone.timedelta(days=dias)
        )
        EstudianteCurso.objects.create(
            estudiante_id=estudiante,
            curso_id=curso,
            access_key_id=access_key,
        )
        return access_key


def _aplicar_efectos_a_escuela(escuela_id: int, producto, is_director: bool):
    """Aplica accesos / contadores de llaves a la escuela bajo lock de fila."""
    if not is_director or escuela_id is None:
        return
    escuela_locked = Escuela.objects.select_for_update().get(pk=escuela_id)
    if producto.basic_access:
        escuela_locked.basic_access = True
    elif producto.cant_basic_key and producto.cant_basic_key > 0:
        escuela_locked.basic_key += producto.cant_basic_key
    else:
        return
    escuela_locked.save()


@transaction.atomic
def registrar_venta_transbank(*, user, producto, escuela, result, token_ws, fecha_venta):
    """Persiste la Venta + TransbankTransaction y aplica efectos a la escuela."""
    venta = Venta.objects.create(
        usuario=user,
        escuela=escuela,
        producto=producto,
        monto_pagado=result['amount'],
        pay_system="WEBPAY",
        payment_status=result['status'],
        fecha_venta=fecha_venta,
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
def registrar_compra_curso_individual(*, user, method, result, fecha_venta,
                                      dias=DIAS_COMPRA_INDIVIDUAL):
    """Registra la compra individual de un curso por un estudiante.

    Otorga acceso por `dias` días (equivale a comprar 1 llave): crea una
    AccessKey(origen='purchase') con expiración, la inscripción del estudiante
    (EstudianteCurso) y la Venta (con `curso`, sin `producto`).

    Seguridad: el curso y el monto se derivan del `result['buy_order']`
    AUTORITATIVO devuelto por Transbank (no de datos que el cliente pueda
    manipular en la confirmación), y el monto pagado se revalida contra el
    precio del curso. Así no se puede pagar un curso barato y reclamar otro.

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

    # Defensa en profundidad: el monto cobrado debe coincidir con el precio.
    if int(round(float(result.get('amount') or 0))) != precio_final_curso(curso):
        raise CompraCursoError("El monto cobrado no coincide con el precio del curso.", 'price_mismatch')

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
def registrar_venta_unificada(*, user, producto, escuela, method, result, fecha_venta):
    """Variante del flujo unificado: persiste Venta + transacción específica."""
    venta = Venta.objects.create(
        usuario=user,
        escuela=escuela,
        producto=producto,
        monto_pagado=result["amount"],
        pay_system=method.upper(),
        payment_status=result["status"],
        fecha_venta=fecha_venta,
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
