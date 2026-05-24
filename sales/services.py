from django.utils import timezone
from django.db import transaction

from schools.models import Escuela
from .models import AccessKey, EstudianteCurso, Venta, TransbankTransaction


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
    elif producto.professional_access:
        escuela_locked.professional_access = True
    elif producto.cant_basic_key and producto.cant_basic_key > 0:
        escuela_locked.basic_key += producto.cant_basic_key
    elif producto.cant_professional_key and producto.cant_professional_key > 0:
        escuela_locked.professional_key += producto.cant_professional_key
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
