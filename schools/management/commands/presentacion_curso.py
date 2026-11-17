"""Agrega (o actualiza) la presentación inicial «Requisitos y exámenes» de un curso.

Crea la unidad 0 «Antes de empezar» con una lección de lectura armada desde
``content_pipeline/presentaciones/`` (parte de la licencia + declaración jurada +
salud y preguntas frecuentes). Sin IA e idempotente; no toca las demás lecciones.
A los alumnos que ya habían completado el resto del curso les marca también esta
lección, para que no pierdan la elegibilidad del examen final.

    # Una vez, desde local (sube los 2 PDF y guarda sus URL en presentaciones/medios.json):
    python manage.py presentacion_curso --subir-medios --storage s3 --pdf-dir out/cd/zip
    # Por curso (local o prod), primero con --dry-run:
    python manage.py presentacion_curso --curso B --dry-run
    python manage.py presentacion_curso --curso B
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_pipeline.processors import presentacion as P


class Command(BaseCommand):
    help = "Agrega la unidad 0 «Antes de empezar» (requisitos y exámenes) a un curso."

    def add_arguments(self, parser):
        parser.add_argument("--curso", action="append", default=[], help="Código del curso (repetible).")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--subir-medios", action="store_true",
                            help="Sube los PDF de las declaraciones y escribe presentaciones/medios.json.")
        parser.add_argument("--storage", default="", help="local|s3 (con --subir-medios).")
        parser.add_argument("--pdf-dir", default="out/cd/zip", help="Carpeta con los PDF (con --subir-medios).")
        parser.add_argument("--mostrar", action="store_true", help="Imprime el Markdown armado.")

    def handle(self, *args, **o):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
        if o["subir_medios"]:
            self._subir(o)
        if not o["curso"]:
            if not o["subir_medios"]:
                raise CommandError("Indica --curso (o --subir-medios).")
            return

        from content_pipeline.exporters.django_importer import importar_presentacion
        from schools.models import Curso

        try:
            urls = P.medios()
        except P.PresentacionError as exc:
            raise CommandError(str(exc)) from exc
        for codigo in o["curso"]:
            curso = Curso.objects.filter(codigo=codigo).first()
            if curso is None:
                raise CommandError(f"No existe el curso con código «{codigo}».")
            try:
                spec = P.componer(codigo, urls)
            except P.PresentacionError as exc:
                raise CommandError(f"{codigo}: {exc}") from exc
            if o["mostrar"]:
                self.stdout.write(spec["contenido"])
            summary = importar_presentacion(curso, spec, dry_run=o["dry_run"])
            self.stdout.write(f"{codigo} · {curso.nombre} (licencia {spec['licencia']}, "
                              f"{len(spec['contenido'])} caracteres):")
            for linea in summary.as_lines():
                self.stdout.write(f"  {linea}")

    def _subir(self, o):
        from content_pipeline.media.storage import get_storage

        st = get_storage(o["storage"] or None, kind="docs")
        datos = {}
        for clave, (archivo, _) in P.PDFS.items():
            path = Path(o["pdf_dir"]) / archivo
            if not path.exists():
                raise CommandError(f"No existe {path}.")
            nombre = f"presentacion/{archivo}"
            datos[clave] = st.save(nombre, path.read_bytes())
            self.stdout.write(f"  {archivo} → {datos[clave]}")
        (P.CARPETA / "medios.json").write_text(json.dumps(datos, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Escrito {P.CARPETA / 'medios.json'}."))
