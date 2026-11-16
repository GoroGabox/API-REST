"""Ayudas para tests: crear suscripciones y ocupar cupos sin pasar por el pago."""
from datetime import timedelta

from django.utils import timezone

from accounts.models import Usuario

from .models import CupoSuscripcion, PlanSuscripcion, Suscripcion
from .suscripciones import sincronizar_espejo


def plan_de(cupos=5, tier='esencial', mensual=100000):
    plan, _ = PlanSuscripcion.objects.get_or_create(
        tier=tier, cupos=cupos,
        defaults=dict(
            precio_mensual=mensual,
            precio_semestral=int(mensual * 6 * 0.9),
            precio_anual=int(mensual * 12 * 0.8),
        ),
    )
    return plan


def dar_suscripcion(escuela, cupos=5, fin=None, tier='esencial', periodo_inicio=None, precio=None, meses=1):
    """Suscripción vigente (hasta `fin`, por defecto +30 días) para `escuela`."""
    now = timezone.now()
    plan = plan_de(cupos, tier)
    inicio = periodo_inicio or now
    sus = Suscripcion.objects.create(
        escuela=escuela, plan=plan, periodo_meses=meses, inicio=inicio, periodo_inicio=inicio,
        fin=fin or (now + timedelta(days=30)),
        precio_periodo=plan.precio(meses) if precio is None else precio,
    )
    sincronizar_espejo(escuela.pk)
    escuela.refresh_from_db()
    return sus


def dar_cupo(sus, estudiante):
    cupo = CupoSuscripcion.objects.create(suscripcion=sus, estudiante=estudiante)
    sincronizar_espejo(sus.escuela_id)
    return cupo


def llenar_cupos(sus, prefijo='relleno'):
    """Ocupa todos los cupos libres con estudiantes de relleno."""
    libres = sus.plan.cupos - CupoSuscripcion.objects.filter(suscripcion=sus, liberado_en__isnull=True).count()
    for i in range(libres):
        est = Usuario.objects.create_user(
            email=f"{prefijo}{sus.id}_{i}@x.com", nombre="R", apellido=str(i), password="x",
            is_estudiante=True,
        )
        est.escuela_id = sus.escuela_id
        est.save(update_fields=['escuela'])
        CupoSuscripcion.objects.create(suscripcion=sus, estudiante=est)
    sincronizar_espejo(sus.escuela_id)
    sus.escuela.refresh_from_db()
