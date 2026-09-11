"""Importa a la BD el banco de ejercicios generado por `build_ejercicios_json`.

Paso ONLINE (no usa LLM): pensado para correr contra la Postgres desplegada.
Crea filas ``Ejercicio`` con ``curso=NULL`` y ``leccion=NULL`` (pool general +
categoría), idempotente por texto de pregunta. Omite los ítems ``diferir=true``.

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

        accion = "DRY-RUN" if opts["dry_run"] else "IMPORT"
        self.stdout.write(f"Ejercicios en archivo: {len(ejercicios)} · {accion}")

        try:
            summary = import_ejercicios(ejercicios, dry_run=opts["dry_run"])
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        for line in summary.as_lines():
            self.stdout.write(f"  {line}")

        if opts["dry_run"]:
            self.stdout.write(self.style.SUCCESS("Dry-run listo (sin cambios en la BD)."))
        else:
            self.stdout.write(self.style.SUCCESS("Importación de ejercicios lista."))
