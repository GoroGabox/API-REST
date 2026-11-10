"""Reconstruye (aproximado) el consumo de llaves anterior a `MovimientoLlaves`.

- Códigos generados por un director: `AccessKey` con `escuela` y `llaves` > 0,
  fechados en `valid_from` (anulados: además la devolución).
- Activaciones con llave (`origen='key'` sin escuela, ligadas a un alumno):
  `ceil(días de vigencia / 7)` llaves a la escuela del alumno, en `valid_from`.
  Las extensiones previas no dejaron rastro y no se pueden reconstruir.
"""
import math

from django.db import migrations


def rellenar(apps, schema_editor):
    AccessKey = apps.get_model("sales", "AccessKey")
    EstudianteCurso = apps.get_model("sales", "EstudianteCurso")
    Movimiento = apps.get_model("sales", "MovimientoLlaves")
    if Movimiento.objects.exists():
        return
    nuevos = []
    for ak in AccessKey.objects.filter(escuela__isnull=False, llaves__gt=0).iterator():
        nuevos.append(Movimiento(escuela_id=ak.escuela_id, cantidad=-ak.llaves, motivo="codigos", fecha=ak.valid_from))
        if ak.status == "revoked":
            nuevos.append(Movimiento(escuela_id=ak.escuela_id, cantidad=ak.llaves, motivo="anulacion", fecha=ak.valid_from))
    inscripciones = (
        EstudianteCurso.objects
        .filter(access_key_id__origen="key", access_key_id__escuela__isnull=True,
                estudiante_id__escuela__isnull=False)
        .values("estudiante_id__escuela_id", "access_key_id__valid_from", "access_key_id__valid_until")
    )
    for fila in inscripciones.iterator():
        desde, hasta = fila["access_key_id__valid_from"], fila["access_key_id__valid_until"]
        if not desde or not hasta:
            continue
        llaves = math.ceil(max((hasta - desde).days, 1) / 7)
        nuevos.append(Movimiento(
            escuela_id=fila["estudiante_id__escuela_id"], cantidad=-llaves, motivo="activacion", fecha=desde,
        ))
    Movimiento.objects.bulk_create(nuevos, batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0028_movimiento_llaves"),
    ]

    operations = [
        migrations.RunPython(rellenar, migrations.RunPython.noop),
    ]
