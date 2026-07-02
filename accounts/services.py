"""Servicios de dominio para la app `accounts`.

Concentra la lógica de generación y presentación de pruebas para reusar entre
los endpoints autenticado (`GenerarPruebaView`) y gratis (`GenerarPruebaGratisView`).
"""
import random
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.utils import timezone

from schools.models import Categoria, Ejercicio
from .models import Certificado, Prueba, PruebaEjercicio


# Umbral de aprobación de una prueba (en %).
APROBACION_MIN_PCT = Decimal('70')


SIZES = {'completa': 35, 'rapida': 10, 'categoria': 20}
TIPOS_VALIDOS = tuple(SIZES.keys())


@dataclass
class ServiceError:
    detail: str
    status: int


def _round_robin_sample_by_category(qs, total_needed):
    """Balancea selección entre categorías sin usar order_by('?')."""
    bucket = defaultdict(list)
    for row in qs.values('id', 'categoria_id'):
        bucket[row['categoria_id']].append(row['id'])

    for cat_id in bucket:
        random.shuffle(bucket[cat_id])

    picked = []
    cat_ids = list(bucket.keys())
    random.shuffle(cat_ids)

    while len(picked) < total_needed and cat_ids:
        next_round = []
        for cat_id in cat_ids:
            if bucket[cat_id]:
                picked.append(bucket[cat_id].pop())
                if len(picked) == total_needed:
                    break
                if bucket[cat_id]:
                    next_round.append(cat_id)
        cat_ids = next_round

    return picked


def seleccionar_ejercicios(tipo: str, categoria_id: Optional[int] = None):
    """Devuelve (ejercicios_final, error). Si error no es None, abortar."""
    tipo = (tipo or '').lower().strip()
    if tipo not in TIPOS_VALIDOS:
        return None, ServiceError(
            f"tipo debe ser uno de {TIPOS_VALIDOS}.", status=400
        )

    total_needed = SIZES[tipo]
    ejercicios_qs = Ejercicio.objects.select_related('categoria', 'curso', 'leccion')

    if tipo == 'categoria':
        if not categoria_id:
            return None, ServiceError("categoria_id es requerido para tipo='categoria'.", 400)
        if not Categoria.objects.filter(pk=categoria_id).exists():
            return None, ServiceError("La categoría indicada no existe.", 404)
        ejercicios_qs = ejercicios_qs.filter(categoria_id=categoria_id)

        total_disp = ejercicios_qs.count()
        if total_disp < total_needed:
            return None, ServiceError(
                f"No hay suficientes ejercicios en la categoría: {total_disp} disponibles, se requieren {total_needed}.",
                400,
            )

        ids = list(ejercicios_qs.values_list('id', flat=True))
        random.shuffle(ids)
        picked_ids = ids[:total_needed]
    else:
        total_disp = ejercicios_qs.count()
        if total_disp < total_needed:
            return None, ServiceError(
                f"No hay suficientes ejercicios en total: {total_disp} disponibles, se requieren {total_needed}.",
                400,
            )
        picked_ids = _round_robin_sample_by_category(ejercicios_qs, total_needed)
        if len(picked_ids) < total_needed:
            return None, ServiceError(
                f"No fue posible balancear suficientes preguntas (obtuvo {len(picked_ids)} de {total_needed}).",
                400,
            )

    ejercicios_map = {
        e.id: e for e in Ejercicio.objects
            .filter(id__in=picked_ids)
            .select_related('categoria', 'curso', 'leccion')
    }
    ejercicios_final = [ejercicios_map[i] for i in picked_ids if i in ejercicios_map]
    return ejercicios_final, None


def serializar_preguntas_publicas(ejercicios):
    """Serializa ejercicios sin exponer la respuesta correcta."""
    return [
        {
            "id": e.id,
            "pregunta": e.pregunta,
            "imagen": e.imagen,
            "opciones": {
                "a": e.opcion_a, "b": e.opcion_b, "c": e.opcion_c,
                "d": e.opcion_d, "e": e.opcion_e, "f": e.opcion_f,
            },
            "categoria_id": e.categoria_id,
            "curso_id": e.curso_id,
            "leccion_id": e.leccion_id,
        }
        for e in ejercicios
    ]


@transaction.atomic
def crear_prueba_con_ejercicios(
    estudiante,
    ejercicios,
    *,
    tipo: str = 'rapida',
    modalidad: str = 'practica',
    curso=None,
):
    """Crea una Prueba persistida con sus PruebaEjercicio asociados.

    `modalidad='practica'` (default) entrena sin gating; `'evaluacion'` puede
    emitir certificado si `tipo='completa'` y la prueba se aprueba.
    """
    prueba = Prueba.objects.create(
        estudiante=estudiante,
        tipo=tipo,
        modalidad=modalidad,
        curso=curso,
    )
    items = [
        PruebaEjercicio(prueba=prueba, ejercicio=e, respuesta_estudiante="")
        for e in ejercicios
    ]
    PruebaEjercicio.objects.bulk_create(items, batch_size=100)
    return prueba


def _opcion_correcta_para(ejercicio) -> Optional[str]:
    """Mapea la respuesta correcta del Ejercicio a su key (a..f).

    El modelo guarda `respuesta` como el texto de la opción correcta;
    matcheamos contra opcion_a..f para devolver la key.
    """
    if not ejercicio.respuesta:
        return None
    correcta_norm = ejercicio.respuesta.strip().lower()
    for key in ('a', 'b', 'c', 'd', 'e', 'f'):
        valor = getattr(ejercicio, f'opcion_{key}', None)
        if valor and valor.strip().lower() == correcta_norm:
            return key
    # Si `respuesta` ya es una key (a..f), devolver tal cual.
    if correcta_norm in {'a', 'b', 'c', 'd', 'e', 'f'}:
        return correcta_norm
    return None


@transaction.atomic
def submit_prueba(prueba: Prueba, respuestas: dict) -> dict:
    """Corrige una prueba con las respuestas enviadas y persiste resultados.

    `respuestas` mapea `pregunta_id -> opcion_key` (string 'a'..'f').

    Returns dict con:
        - aprobado, score, total_correctas, total
        - detalles: lista de {pregunta_id, correcta, opcion_correcta, respuesta_estudiante}
    """
    if prueba.completada_en is not None:
        raise ValueError("La prueba ya fue completada y no se puede reenviar.")

    items = list(
        PruebaEjercicio.objects
        .filter(prueba=prueba)
        .select_related('ejercicio')
    )
    total = len(items)
    if total == 0:
        raise ValueError("La prueba no tiene preguntas.")

    detalles = []
    correctas = 0
    actualizar = []
    for item in items:
        respuesta_enviada = (respuestas.get(item.ejercicio_id) or '').strip().lower()
        opcion_correcta = _opcion_correcta_para(item.ejercicio)
        es_correcta = bool(respuesta_enviada) and respuesta_enviada == opcion_correcta
        item.respuesta_estudiante = respuesta_enviada
        item.correcta = es_correcta
        actualizar.append(item)
        if es_correcta:
            correctas += 1
        detalles.append({
            "pregunta_id": item.ejercicio_id,
            "pregunta": item.ejercicio.pregunta,
            "correcta": es_correcta,
            "opcion_correcta": opcion_correcta,
            "respuesta_estudiante": respuesta_enviada,
            "explicacion": item.ejercicio.explicacion or "",
        })

    PruebaEjercicio.objects.bulk_update(actualizar, ['respuesta_estudiante', 'correcta'], batch_size=100)

    score = (Decimal(correctas) / Decimal(total) * Decimal(100)).quantize(Decimal('0.01'))
    aprobado = score >= APROBACION_MIN_PCT

    prueba.total_correctas = correctas
    prueba.score = score
    prueba.aprobado = aprobado
    prueba.completada_en = timezone.now()
    prueba.save(update_fields=['total_correctas', 'score', 'aprobado', 'completada_en'])

    # ---- Gamificación: hearts, XP, streak, logros ----
    from . import gamification
    user = prueba.estudiante
    incorrectas = total - correctas

    xp_ganado = 0
    # Sólo evaluaciones de tipo no-rápido consumen corazones por error.
    # Rápida = entrenamiento ligero, no debe penalizar vidas.
    consume_corazones = (
        prueba.modalidad == 'evaluacion'
        and prueba.tipo != 'rapida'
    )
    if prueba.modalidad == 'evaluacion':
        if consume_corazones and incorrectas > 0:
            gamification.consumir_corazones(user, incorrectas)
        xp_ganado = correctas * gamification.XP_POR_CORRECTA_EVALUACION
        if aprobado:
            xp_ganado += gamification.XP_BONUS_APROBAR_EVALUACION
    else:  # practica
        xp_ganado = correctas * gamification.XP_POR_CORRECTA_PRACTICA

    if xp_ganado:
        gamification.otorgar_xp(user, xp_ganado, source=f'prueba:{prueba.id}')
        user.refresh_from_db(fields=['xp'])

    gamification.actualizar_streak(user)
    nuevos_logros = gamification.chequear_y_otorgar_logros(user, contexto={'ultima_prueba': prueba})

    return {
        "aprobado": aprobado,
        "score": float(score),
        "total_correctas": correctas,
        "total": total,
        "detalles": detalles,
        "xp_ganado": xp_ganado,
        "corazones_restantes": user.hearts,
        "streak_actual": user.streak_current,
        "logros_nuevos": nuevos_logros,
    }


def emitir_certificado_si_corresponde(prueba: Prueba) -> Optional[Certificado]:
    """Si la prueba es evaluación-final-aprobada con curso, emite Certificado.

    Llamado por signal post_save de Prueba (ver `accounts/signals.py`).
    Idempotente: si ya existe Certificado para (estudiante, curso), no lo duplica.
    """
    if not prueba.aprobado:
        return None
    if prueba.modalidad != 'evaluacion':
        return None
    if prueba.tipo != 'completa':  # solo el examen final emite certificado
        return None
    if prueba.curso_id is None:
        return None

    cert, _ = Certificado.objects.get_or_create(
        estudiante=prueba.estudiante,
        curso_id=prueba.curso_id,
        defaults={'prueba': prueba},
    )
    return cert
