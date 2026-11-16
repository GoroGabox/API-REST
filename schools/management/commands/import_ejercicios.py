"""Importa a la BD el banco de ejercicios generado por `build_ejercicios_json`.

Paso ONLINE (no usa LLM): pensado para correr contra la Postgres desplegada.
Crea filas ``Ejercicio`` (idempotente por enunciado + alternativas). Con
``--curso`` (o ``curso`` en el JSON) vincula cada ítem con ``unidad_orden`` a esa
unidad del curso (quiz de unidad). Sin curso quedan en el pool común. Omite los
ítems ``diferir=true``.

Uso::

    python manage.py import_ejercicios --file ejercicios_clase_b.json --dry-run
    python manage.py import_ejercicios --file ejercicios_clase_b.json
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_pipeline.exporters.django_importer import import_ejercicios


class Command(BaseCommand):
    help = "Importa el banco de ejercicios (JSON de build_ejercicios_json) a la BD."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="JSON de build_ejercicios_json.")
        parser.add_argument("--dry-run", action="store_true", help="No escribe; reporta qué haría.")
        parser.add_argument(
            "--curso", default="",
            help="Código del curso: vincula cada ítem con `unidad_orden` a esa unidad del curso.",
        )

    def handle(self, *args, **opts):
        import sys
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass

        path = Path(opts["file"])
        if not path.exists():
            raise CommandError(f"No existe el archivo: {path}")

        data = json.loads(path.read_text(encoding="utf-8"))
        ejercicios = data.get("ejercicios") if isinstance(data, dict) else data
        if not isinstance(ejercicios, list):
            raise CommandError("El JSON debe traer una lista 'ejercicios'.")

        # Fuente del archivo para los ítems que no la traen (p. ej. el PDF de CONASET).
        fuente = data.get("fuente") if isinstance(data, dict) else ""
        if fuente:
            ejercicios = [{**e, "fuente": e.get("fuente") or fuente} for e in ejercicios]

        accion = "DRY-RUN" if opts["dry_run"] else "IMPORT"
        self.stdout.write(f"Ejercicios en archivo: {len(ejercicios)} · {accion}")

        curso = None
        codigo = opts["curso"] or (data.get("curso") if isinstance(data, dict) else "")
        if codigo:
            from schools.models import Curso
            curso = Curso.objects.filter(codigo=codigo).first()
            if curso is None:
                raise CommandError(f"No existe el curso con código {codigo!r}.")
            self.stdout.write(f"Curso: {curso.codigo} · {curso.nombre} (#{curso.id})")
        try:
            summary = import_ejercicios(ejercicios, dry_run=opts["dry_run"], curso=curso)
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        for line in summary.as_lines():
            self.stdout.write(f"  {line}")

        if opts["dry_run"]:
            self.stdout.write(self.style.SUCCESS("Dry-run listo (sin cambios en la BD)."))
        else:
            self.stdout.write(self.style.SUCCESS("Importación de ejercicios lista."))
