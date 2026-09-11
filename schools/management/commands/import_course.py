"""Importa a la base de datos un curso generado por `generate_course`.

Lee un JSON `{manifest, lessons}` (o `--manifest`/`--lessons` por separado) y hace
un upsert idempotente por código de curso vía `import_a2_course` (el importador es
genérico pese al nombre). Pensado para correr en el entorno DESPLEGADO —no
requiere PDFs, IA ni el worker— apuntando a la Postgres de producción.

Uso típico:
    python manage.py import_course --file curso.json --dry-run   # muestra el plan
    python manage.py import_course --file curso.json             # aplica

`--dry-run` reporta qué crearía/actualizaría sin escribir; úsalo siempre antes de
aplicar en producción.
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_pipeline.exporters.django_importer import import_a2_course
from content_pipeline.exporters.json_exporter import read_json
from schools.models import Curso


class Command(BaseCommand):
    help = (
        "Importa a la BD un curso generado por `generate_course` (JSON con "
        "{manifest, lessons}). Upsert idempotente por código de curso."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", help="JSON combinado {manifest, lessons} (salida de generate_course).")
        parser.add_argument("--manifest", help="JSON de manifest (alternativa a --file).")
        parser.add_argument("--lessons", help="JSON de lecciones (alternativa a --file).")
        parser.add_argument("--dry-run", action="store_true", help="No escribe; solo reporta qué haría.")
        parser.add_argument(
            "--prune",
            action="store_true",
            help="Elimina lecciones del curso que no estén en el JSON "
                 "(destructivo: borra progresos vinculados por CASCADE).",
        )

    def handle(self, *args, **opts):
        if opts.get("file"):
            data = read_json(Path(opts["file"]))
            if not isinstance(data, dict) or "manifest" not in data or "lessons" not in data:
                raise CommandError("El --file debe ser un JSON con las claves 'manifest' y 'lessons'.")
            manifest = data["manifest"]
            lessons = data["lessons"]
        elif opts.get("manifest") and opts.get("lessons"):
            manifest = read_json(Path(opts["manifest"]))
            lessons = read_json(Path(opts["lessons"]))
        else:
            raise CommandError("Indica --file, o bien --manifest y --lessons juntos.")

        codigo = (manifest.get("curso") or {}).get("codigo", "?")
        accion = "DRY-RUN" if opts["dry_run"] else "IMPORT"
        self.stdout.write(f"Curso: {codigo} · {len(lessons)} lecciones · {accion}")

        try:
            summary = import_a2_course(
                manifest, lessons, dry_run=opts["dry_run"], prune=opts["prune"],
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        for line in summary.as_lines():
            self.stdout.write(f"  {line}")

        if opts["dry_run"]:
            self.stdout.write(self.style.SUCCESS("Dry-run listo (sin cambios en la BD)."))
            return

        curso = Curso.objects.filter(codigo=codigo).first()
        if curso:
            self.stdout.write(self.style.SUCCESS(
                f"Importado: curso #{curso.id} «{curso.nombre}» ({curso.codigo})."
            ))
        else:
            self.stdout.write(self.style.SUCCESS("Importación lista."))
