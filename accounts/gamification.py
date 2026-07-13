"""Servicios de gamificación: hearts, XP, streak, logros.

Diseño:
- Hearts: economía basada en actividad, NO en tiempo. Se gana 1 corazón al
  completar una lección (hasta `MAX_HEARTS`) y se pierde 1 por cada respuesta
  incorrecta en tests/pruebas/ejercicios FUERA de las lecciones (los quizzes
  dentro de una lección no descuentan). La antigua regeneración por tiempo
  quedó desactivada: `regenerar_recursos` es un no-op que se conserva por
  compatibilidad con los callers (serializer/stats view).
- XP es acumulado; level se deriva con `nivel_para_xp(xp)`.
- Streak: actividad cuenta cuando el user envía una prueba (submit) en un día.
  Si el último día activo fue ayer → +1. Si fue hoy → no cambia. Si saltó días
  (sin frozen) → resetea a 1.
- Logros: catalog-driven. `chequear_y_otorgar_logros` se llama tras eventos
  significativos y emite los que correspondan.
- ENERGY: removida del modelo. Los clientes pagan por acceso al generador de
  pruebas, así que no tiene sentido un recurso limitado para iniciarlas.
"""
from datetime import timedelta
from decimal import Decimal
from typing import List

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import Logro, Usuario, UsuarioLogro


# Vidas: máximo, y válvula de escape por tiempo. Se regenera 1 vida cada
# HEART_REGEN_MINUTES aunque el estudiante no complete lecciones, para que
# llegar a 0 no sea un callejón sin salida (ej. ya completó todas las lecciones).
MAX_HEARTS = 5
HEART_REGEN_MINUTES = 120  # +1 vida cada 2 horas

# Reintento del examen final: horas mínimas entre entregas del mismo curso.
FINAL_EXAM_RETRY_HOURS = 24

XP_POR_CORRECTA_PRACTICA = 5
XP_POR_CORRECTA_EVALUACION = 10
XP_BONUS_APROBAR_EVALUACION = 50

# Logros catalogados (creados con get_or_create al chequear).
LOGROS_CATALOGO = {
    'racha-7':     {'nombre': 'Racha 7',     'descripcion': '7 días seguidos de actividad',  'icono': '🔥'},
    'racha-30':    {'nombre': 'Racha 30',    'descripcion': '30 días seguidos de actividad', 'icono': '🏆'},
    'pleno':       {'nombre': 'Pleno',       'descripcion': '100% en una evaluación',         'icono': '💯'},
    'primera-aprobacion': {'nombre': 'Primera aprobación', 'descripcion': 'Aprobaste tu primera evaluación', 'icono': '🎓'},
    'nivel-5':     {'nombre': 'Nivel 5',     'descripcion': 'Alcanzaste nivel 5',              'icono': '⭐'},
    'nivel-10':    {'nombre': 'Nivel 10',    'descripcion': 'Alcanzaste nivel 10',             'icono': '🌟'},
}


# ----------------- Hearts (vidas) -----------------

def _aplicar_regen(actual: int, max_: int, next_at, minutos: int):
    """Calcula cuántas vidas regenerar desde 'next_at' hasta ahora.

    Retorna (nuevo_valor, nuevo_next_at). Regeneración perezosa: se evalúa al
    leer/consumir, sin jobs cron.
    """
    now = timezone.now()
    if actual >= max_:
        return max_, None
    if next_at is None:
        # Sin refill programado: agenda el próximo (p.ej. tras un consumo previo
        # a esta lógica). No regenera todavía.
        return actual, now + timedelta(minutes=minutos)
    if now < next_at:
        return actual, next_at
    elapsed = (now - next_at).total_seconds() / 60
    pasos = 1 + int(elapsed // minutos)
    nuevo = min(max_, actual + pasos)
    if nuevo >= max_:
        return nuevo, None
    return nuevo, next_at + timedelta(minutes=minutos * pasos)


def regenerar_recursos(user: Usuario, save: bool = True) -> Usuario:
    """Aplica la regeneración pasiva de vidas según el tiempo transcurrido.

    Válvula de escape: aunque el estudiante no complete lecciones, recupera 1
    vida cada HEART_REGEN_MINUTES hasta el máximo. Perezosa: la llaman el
    serializer de perfil, `MeStatsView` y antes de consumir/gate.
    """
    h, h_next = _aplicar_regen(user.hearts, MAX_HEARTS, user.next_heart_regen_at, HEART_REGEN_MINUTES)
    if (h, h_next) != (user.hearts, user.next_heart_regen_at):
        user.hearts = h
        user.next_heart_regen_at = h_next
        if save:
            user.save(update_fields=['hearts', 'next_heart_regen_at'])
    return user


def consumir_corazones(user: Usuario, cantidad: int) -> int:
    """Resta hearts (saturando en 0). Se llama al fallar tests/pruebas externos.

    Programa el próximo refill pasivo si baja del máximo y no había uno agendado.
    """
    if cantidad <= 0:
        return user.hearts
    regenerar_recursos(user, save=False)
    nuevo = max(0, user.hearts - cantidad)
    changed = nuevo != user.hearts
    user.hearts = nuevo
    if user.next_heart_regen_at is None and user.hearts < MAX_HEARTS:
        user.next_heart_regen_at = timezone.now() + timedelta(minutes=HEART_REGEN_MINUTES)
        changed = True
    if changed:
        user.save(update_fields=['hearts', 'next_heart_regen_at'])
    return user.hearts


def ganar_corazones(user: Usuario, cantidad: int = 1) -> int:
    """Suma hearts saturando en MAX_HEARTS. Se llama al completar una lección."""
    if cantidad <= 0:
        return user.hearts
    regenerar_recursos(user, save=False)
    nuevo = min(MAX_HEARTS, user.hearts + cantidad)
    if nuevo != user.hearts:
        user.hearts = nuevo
        # Al tope, no hay refill pendiente.
        if user.hearts >= MAX_HEARTS:
            user.next_heart_regen_at = None
        user.save(update_fields=['hearts', 'next_heart_regen_at'])
    return user.hearts


# ----------------- XP / Nivel -----------------

def nivel_para_xp(xp: int) -> dict:
    """Niveles cuadráticos: nivel N requiere N*100 XP cumulativo (1->100, 2->300, 3->600...).

    Fórmula: total para llegar a nivel N = 50 * N * (N+1). Inverso aproximado:
    N = floor((-1 + sqrt(1 + 8*xp/100)) / 2).
    """
    import math
    if xp <= 0:
        return {'level': 1, 'xp_para_nivel_actual': 0, 'xp_para_proximo_nivel': 100, 'progreso_pct': 0}
    n = int((-1 + math.sqrt(1 + 8 * xp / 100)) / 2)
    n = max(1, n)
    xp_inicio = 50 * n * (n - 1)
    xp_proximo = 50 * (n + 1) * n
    rango = max(1, xp_proximo - xp_inicio)
    progreso = max(0, min(100, int((xp - xp_inicio) * 100 / rango)))
    return {
        'level': n,
        'xp_para_nivel_actual': xp_inicio,
        'xp_para_proximo_nivel': xp_proximo,
        'progreso_pct': progreso,
    }


def otorgar_xp(user: Usuario, cantidad: int, source: str = '') -> int:
    if cantidad <= 0:
        return user.xp
    Usuario.objects.filter(pk=user.pk).update(xp=F('xp') + cantidad)
    user.refresh_from_db(fields=['xp'])
    return user.xp


# ----------------- Streak -----------------

def actualizar_streak(user: Usuario) -> int:
    """Llamar cuando hay actividad relevante (submit de prueba)."""
    hoy = timezone.localdate()
    last = user.last_active_date

    if last == hoy:
        return user.streak_current

    nuevo_current = 1
    if last is not None:
        gap = (hoy - last).days
        if gap == 1:
            nuevo_current = user.streak_current + 1
        elif gap > 1 and user.streak_frozen_until and user.streak_frozen_until >= hoy:
            nuevo_current = user.streak_current + 1
        else:
            nuevo_current = 1

    user.streak_current = nuevo_current
    user.streak_longest = max(user.streak_longest, nuevo_current)
    user.last_active_date = hoy
    user.save(update_fields=['streak_current', 'streak_longest', 'last_active_date'])
    return user.streak_current


# ----------------- Logros -----------------

@transaction.atomic
def _otorgar_logro(user: Usuario, slug: str) -> bool:
    """Crea (si no existe) el Logro del catálogo y lo asigna al user."""
    meta = LOGROS_CATALOGO.get(slug)
    if not meta:
        return False
    logro, _ = Logro.objects.get_or_create(
        slug=slug,
        defaults={'nombre': meta['nombre'], 'descripcion': meta['descripcion'], 'icono': meta['icono']},
    )
    _, created = UsuarioLogro.objects.get_or_create(usuario=user, logro=logro)
    return created


def chequear_y_otorgar_logros(user: Usuario, contexto: dict = None) -> List[str]:
    """Evalúa todas las condiciones y otorga los logros nuevos. Idempotente."""
    nuevos = []
    contexto = contexto or {}

    if user.streak_current >= 7 and _otorgar_logro(user, 'racha-7'):
        nuevos.append('racha-7')
    if user.streak_current >= 30 and _otorgar_logro(user, 'racha-30'):
        nuevos.append('racha-30')

    info_nivel = nivel_para_xp(user.xp)
    if info_nivel['level'] >= 5 and _otorgar_logro(user, 'nivel-5'):
        nuevos.append('nivel-5')
    if info_nivel['level'] >= 10 and _otorgar_logro(user, 'nivel-10'):
        nuevos.append('nivel-10')

    ultima = contexto.get('ultima_prueba')
    if ultima is not None and ultima.modalidad == 'evaluacion' and ultima.aprobado:
        if _otorgar_logro(user, 'primera-aprobacion'):
            nuevos.append('primera-aprobacion')
        if Decimal(ultima.score) >= Decimal('100'):
            if _otorgar_logro(user, 'pleno'):
                nuevos.append('pleno')

    return nuevos
