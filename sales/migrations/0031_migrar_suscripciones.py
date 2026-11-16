"""Pasa las suscripciones viejas (campos `basic_*` de Escuela) al modelo nuevo.

- Cada escuela con `basic_access=True` recibe una `Suscripcion` con un plan
  Esencial del tamaño más cercano (≥ sus cupos), hasta su `basic_access_until`
  (sin fecha → un año desde hoy). Esos planes se crean inactivos si no
  existían: el admin carga y activa los precios reales desde Precios.
- Los cupos alumno × curso (`AccessKey.origen='seat'` activos) pasan a un
  `CupoSuscripcion` por alumno, sin duplicar alumnos.
- Los `Producto(tipo='suscripcion')` se desactivan: el catálogo nuevo es
  `PlanSuscripcion`.
"""
from datetime import timedelta

from django.db import migrations
from django.utils import timezone

TAMANOS = (25, 50, 100, 200)
# Precio mensual de referencia por tamaño (Esencial); semestral −10 %, anual −20 %.
PRECIO_MENSUAL = {25: 90000, 50: 160000, 100: 280000, 200: 480000}


def _plan_para(PlanSuscripcion, cupos):
    tam = next((t for t in TAMANOS if t >= cupos), TAMANOS[-1])
    m = PRECIO_MENSUAL[tam]
    plan, _ = PlanSuscripcion.objects.get_or_create(
        tier='esencial', cupos=tam,
        defaults={
            'precio_mensual': m,
            'precio_semestral': int(m * 6 * 0.9),
            'precio_anual': int(m * 12 * 0.8),
            'activo': False,
        },
    )
    return plan


def migrar(apps, schema_editor):
    Escuela = apps.get_model('schools', 'Escuela')
    Producto = apps.get_model('sales', 'Producto')
    PlanSuscripcion = apps.get_model('sales', 'PlanSuscripcion')
    Suscripcion = apps.get_model('sales', 'Suscripcion')
    PeriodoSuscripcion = apps.get_model('sales', 'PeriodoSuscripcion')
    CupoSuscripcion = apps.get_model('sales', 'CupoSuscripcion')
    EstudianteCurso = apps.get_model('sales', 'EstudianteCurso')

    now = timezone.now()
    for e in Escuela.objects.filter(basic_access=True):
        if Suscripcion.objects.filter(escuela=e).exists():
            continue
        alumnos = list(
            EstudianteCurso.objects.filter(
                estudiante_id__escuela_id=e.id,
                access_key_id__origen='seat',
                access_key_id__status='active',
            ).values_list('estudiante_id_id', flat=True).distinct()
        )
        plan = _plan_para(PlanSuscripcion, max(e.basic_seats_max, len(alumnos), 1))
        fin = e.basic_access_until or (now + timedelta(days=365))
        s = Suscripcion.objects.create(
            escuela=e, plan=plan, periodo_meses=1, inicio=now, periodo_inicio=now, fin=fin, precio_periodo=0,
        )
        PeriodoSuscripcion.objects.create(
            suscripcion=s, plan=plan, periodo_meses=1, inicio=now, fin=fin, monto=0, motivo='manual',
        )
        for est_id in alumnos:
            if not CupoSuscripcion.objects.filter(estudiante_id=est_id, liberado_en__isnull=True).exists():
                CupoSuscripcion.objects.create(suscripcion=s, estudiante_id=est_id, asignado_en=now)

    Producto.objects.filter(tipo='suscripcion').update(activo=False)


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0030_suscripciones'),
    ]

    operations = [
        migrations.RunPython(migrar, migrations.RunPython.noop),
    ]
