from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from content_pipeline.exporters.json_exporter import write_json
from content_pipeline.extractors.pdf_text_extractor import extract_pdf_pages

PROJECT_ROOT = Path(settings.BASE_DIR).parent


def project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


class Command(BaseCommand):
    help = "Extrae texto paginado del Libro del Nuevo Conductor Clase A2."

    def add_arguments(self, parser):
        parser.add_argument("--pdf", default="data/input/libro_nuevo_conductor_a2.pdf")
        parser.add_argument("--out", default="data/output/pages/book_pages.json")

    def handle(self, *args, **options):
        pdf_path = project_path(options["pdf"])
        out_path = project_path(options["out"])
        try:
            pages = extract_pdf_pages(pdf_path)
            write_json(out_path, pages)
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Páginas extraídas: {len(pages)} -> {out_path}"))
