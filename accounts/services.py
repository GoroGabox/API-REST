"""Servicios de dominio para la app `accounts`.

Concentra la lógica de generación y presentación de pruebas para reusar entre
los endpoints autenticado (`GenerarPruebaView`) y gratis (`GenerarPruebaGratisView`).
"""
import random
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from schools.models import Categoria, Ejercicio
from .models import Certificado, Prueba, PruebaEjercicio


# Umbral de aprobación (en %). El examen final de un curso exige más que una
# práctica cualquiera del Gimnasio.
APROBACION_MIN_PCT = Decimal('70')
APROBACION_EXAMEN_FINAL_PCT = Decimal('80')


SIZES = {'completa': 35, 'rapida': 10, 'categoria': 15}
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


def _ejercicios_de_curso_qs(curso):
    """Ejercicios del curso: asociados directamente (`curso`) o a una de sus
    lecciones (`leccion__curso`)."""
    return (
        Ejercicio.objects
        .select_related('categoria', 'curso', 'leccion')
        .filter(Q(curso=curso) | Q(leccion__curso=curso))
        .distinct()
    )


def total_preguntas_examen_final(curso) -> int:
    """Nº real de preguntas que tendrá el examen final (tope SIZES['completa'])."""
    return min(SIZES['completa'], _ejercicios_de_curso_qs(curso).count())


def seleccionar_ejercicios_de_curso(curso, total_needed: int = SIZES['completa']):
    """Selecciona preguntas SOLO del curso, para el examen final.

    Si el curso tiene menos de `total_needed`, usa todas las disponibles (el
    examen se ajusta al tamaño real y el % se calcula sobre ese total).
    Devuelve (ejercicios, error).
    """
    qs = _ejercicios_de_curso_qs(curso)
    total_disp = qs.count()
    if total_disp == 0:
        return None, ServiceError(
            "Este curso aún no tiene preguntas para el examen final.", 400
        )

    n = min(total_needed, total_disp)
    # Balancea por categoría dentro del curso; completa con aleatorio si faltara.
    picked_ids = _round_robin_sample_by_category(qs, n)
    if len(picked_ids) < n:
        resto = [i for i in qs.values_list('id', flat=True) if i not in set(picked_ids)]
        random.shuffle(resto)
        picked_ids += resto[: n - len(picked_ids)]

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
            # El cliente usa esto para renderizar checkboxes (multi) vs radio.
            "multiple": bool(getattr(e, 'multiple', False)),
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


_KEYS_VALIDAS = {'a', 'b', 'c', 'd', 'e', 'f'}


def _claves_correctas(ejercicio) -> set:
    """Conjunto de keys correctas (a..f) de un ejercicio.

    Soporta selección múltiple: si `respuestas_correctas` trae keys, esas son la
    verdad; si no, cae a la respuesta única (`_opcion_correcta_para`).
    """
    crudas = getattr(ejercicio, 'respuestas_correctas', None) or []
    if crudas:
        return {str(k).strip().lower() for k in crudas if str(k).strip().lower() in _KEYS_VALIDAS}
    unica = _opcion_correcta_para(ejercicio)
    return {unica} if unica else set()


def _normalizar_seleccion(valor) -> set:
    """Normaliza la respuesta enviada por el estudiante a un set de keys.

    Acepta 'a' (única), ['a','b'] (lista JSON) o 'a,b' / 'a b' (separadores).
    """
    if valor is None:
        return set()
    if isinstance(valor, (list, tuple, set)):
        items = valor
    else:
        items = re.split(r'[\s,;/]+', str(valor))
    return {str(x).strip().lower() for x in items if str(x).strip().lower() in _KEYS_VALIDAS}


def corregir_seleccion_libre(respuestas: dict) -> dict:
    """Corrige respuestas SIN persistir — práctica pública sin login.

    `respuestas` mapea ejercicio_id -> 'a' | ['a','b'] (una key o varias). La
    corrección es idéntica a la de una prueba real (set exacto), pero no crea
    `Prueba` ni toca gamificación, y la clave nunca sale del servidor.

    Devuelve `{aprobado, score, total_correctas, total, detalles}` con el mismo
    shape de detalle que `submit_prueba`.
    """
    ids = []
    for raw_id in (respuestas or {}):
        try:
            ids.append(int(raw_id))
        except (TypeError, ValueError):
            continue
    ejercicios = {
        e.id: e
        for e in Ejercicio.objects.filter(id__in=ids).select_related('categoria')
    }

    detalles = []
    correctas = 0
    total = 0
    for raw_id, sel in (respuestas or {}).items():
        try:
            eid = int(raw_id)
        except (TypeError, ValueError):
            continue
        ejercicio = ejercicios.get(eid)
        if ejercicio is None:
            continue
        total += 1
        seleccion = _normalizar_seleccion(sel)
        correctas_set = _claves_correctas(ejercicio)
        es_correcta = bool(correctas_set) and seleccion == correctas_set
        if es_correcta:
            correctas += 1
        correctas_ord = sorted(correctas_set)
        detalles.append({
            "pregunta_id": ejercicio.id,
            "pregunta": ejercicio.pregunta,
            "correcta": es_correcta,
            "opcion_correcta": correctas_ord[0] if len(correctas_ord) == 1 else None,
            "opciones_correctas": correctas_ord,
            "multiple": bool(getattr(ejercicio, 'multiple', False)),
            "respuesta_estudiante": ",".join(sorted(seleccion)),
            "explicacion": ejercicio.explicacion or "",
        })

    score = round((correctas / total) * 100, 2) if total else 0
    return {
        "aprobado": score >= float(APROBACION_MIN_PCT),
        "score": score,
        "total_correctas": correctas,
        "total": total,
        "detalles": detalles,
    }


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
        seleccion = _normalizar_seleccion(respuestas.get(item.ejercicio_id))
        correctas_set = _claves_correctas(item.ejercicio)
        # Correcta si seleccionó EXACTAMENTE el conjunto correcto (para single
        # es el caso de 1 elemento; para multi exige acertar todas sin sobrar).
        es_correcta = bool(correctas_set) and seleccion == correctas_set
        # Persistimos la selección como texto ordenado ('a' o 'a,b').
        respuesta_enviada = ",".join(sorted(seleccion))
        item.respuesta_estudiante = respuesta_enviada
        item.correcta = es_correcta
        actualizar.append(item)
        if es_correcta:
            correctas += 1
        correctas_ord = sorted(correctas_set)
        detalles.append({
            "pregunta_id": item.ejercicio_id,
            "pregunta": item.ejercicio.pregunta,
            "correcta": es_correcta,
            # Back-compat: `opcion_correcta` expone la key única (None si multi).
            "opcion_correcta": correctas_ord[0] if len(correctas_ord) == 1 else None,
            "opciones_correctas": correctas_ord,
            "multiple": bool(getattr(item.ejercicio, 'multiple', False)),
            "respuesta_estudiante": respuesta_enviada,
            "explicacion": item.ejercicio.explicacion or "",
        })

    PruebaEjercicio.objects.bulk_update(actualizar, ['respuesta_estudiante', 'correcta'], batch_size=100)

    score = (Decimal(correctas) / Decimal(total) * Decimal(100)).quantize(Decimal('0.01'))
    # El examen final del curso (evaluación + completa) exige 80%; el resto 70%.
    es_examen_final = prueba.modalidad == 'evaluacion' and prueba.tipo == 'completa'
    umbral = APROBACION_EXAMEN_FINAL_PCT if es_examen_final else APROBACION_MIN_PCT
    aprobado = score >= umbral

    prueba.total_correctas = correctas
    prueba.score = score
    prueba.aprobado = aprobado
    prueba.completada_en = timezone.now()
    prueba.save(update_fields=['total_correctas', 'score', 'aprobado', 'completada_en'])

    # ---- Gamificación: XP, streak, logros ----
    from . import gamification
    user = prueba.estudiante

    xp_ganado = 0
    if prueba.modalidad == 'evaluacion':
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


def elegibilidad_examen_final(user, curso) -> dict:
    """Determina si `user` puede rendir (o volver a rendir) el examen final de `curso`.

    Reglas (en este orden):
    - Si ya tiene certificado del curso → curso finalizado, no hay más intentos.
    - Si el acceso al curso venció (AccessKey.valid_until pasado) → sin intentos.
    - Si no completó TODAS las lecciones del curso → lecciones pendientes.
    - En cualquier otro caso → puede rendir (sin espera entre intentos).

    Devuelve dict:
        {
          'puede': bool,
          'razon': 'ok'|'curso_completado'|'plazo_vencido'|'lecciones_pendientes',
          'expira_en': datetime|None,        # fin de plazo del curso (None = sin límite)
          'ultimo_intento': datetime|None,
          'lecciones_total': int,
          'lecciones_completadas': int,
          'total_preguntas': int,            # nº real de preguntas del examen
        }
    """
    from sales.models import EstudianteCurso
    from schools.models import Leccion
    from .models import EstudianteLeccion

    now = timezone.now()

    # Plazo del curso: expiración de la llave/cupo con que se inscribió.
    expira_en = None
    ec = (
        EstudianteCurso.objects
        .filter(estudiante_id=user, curso_id=curso)
        .select_related('access_key_id')
        .first()
    )
    if ec and ec.access_key_id:
        expira_en = ec.access_key_id.valid_until

    lecciones_total = Leccion.objects.filter(curso=curso).count()
    lecciones_completadas = (
        EstudianteLeccion.objects
        .filter(estudiante=user, leccion__curso=curso)
        .values('leccion_id').distinct().count()
    )

    base = {
        'puede': False,
        'razon': 'ok',
        'expira_en': expira_en,
        'ultimo_intento': None,
        'lecciones_total': lecciones_total,
        'lecciones_completadas': lecciones_completadas,
        'total_preguntas': total_preguntas_examen_final(curso),
    }

    # Curso ya aprobado (certificado emitido) → finalizado.
    if Certificado.objects.filter(estudiante=user, curso=curso).exists():
        return {**base, 'razon': 'curso_completado'}

    # Plazo del curso vencido → sin más intentos.
    if expira_en is not None and now > expira_en:
        return {**base, 'razon': 'plazo_vencido'}

    # Requisito: todas las lecciones del curso completadas (un curso sin
    # lecciones no tiene nada pendiente).
    if lecciones_completadas < lecciones_total:
        return {**base, 'razon': 'lecciones_pendientes'}

    # Última entrega del examen final (informativo, no restringe reintentos).
    ultima = (
        Prueba.objects
        .filter(
            estudiante=user, curso=curso,
            tipo='completa', modalidad='evaluacion',
            completada_en__isnull=False,
        )
        .order_by('-completada_en')
        .first()
    )
    if ultima and ultima.completada_en:
        base['ultimo_intento'] = ultima.completada_en

    return {**base, 'puede': True, 'razon': 'ok'}


# ============================================================
# Invitación "configura tu contraseña"
# ============================================================

def construir_link_password(user):
    """Devuelve el enlace de reset/definición de contraseña para `user`.

    Mismo formato que consume el front (/change-password?uidb64=..&token=..).
    """
    import os
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.http import urlsafe_base64_encode
    from django.utils.encoding import force_bytes

    uid = urlsafe_base64_encode(force_bytes(user.id))
    token = default_token_generator.make_token(user)
    frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:3000').rstrip('/')
    return f'{frontend_url}/change-password?uidb64={uid}&token={token}'


def enviar_invitacion_password(user):
    """Envía el correo de "configura tu contraseña" a `user` (best-effort).

    Reutilizado por el alta 1×1 (director/admin) y el alta masiva. Devuelve el
    enlace generado. No propaga errores de envío (fail_silently).
    """
    from django.conf import settings
    from django.core.mail import send_mail

    link = construir_link_password(user)
    send_mail(
        subject='Configura tu contraseña — AutoTest',
        message=(
            f'Hola {user.nombre or ""},\n\n'
            f'Tu cuenta AutoTest ha sido creada. Define tu contraseña '
            f'accediendo al siguiente enlace:\n\n{link}\n\n'
            f'Si no reconoces esta invitación, ignora este correo.'
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=True,
    )
    return link
