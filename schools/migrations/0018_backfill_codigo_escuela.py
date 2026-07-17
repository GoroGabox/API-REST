import secrets

from django.db import migrations

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _codigo_unico(Escuela, length=6):
    while True:
        codigo = "".join(secrets.choice(_ALPHABET) for _ in range(length))
        if not Escuela.objects.filter(codigo=codigo).exists():
            return codigo


def backfill(apps, schema_editor):
    Escuela = apps.get_model("schools", "Escuela")
    for escuela in Escuela.objects.filter(codigo__isnull=True):
        escuela.codigo = _codigo_unico(Escuela)
        escuela.save(update_fields=["codigo"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("schools", "0017_escuela_codigo"),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
