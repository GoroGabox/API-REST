from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from content_pipeline.exporters.json_exporter import read_json
from content_pipeline.processors.validators import validate_lessons

PROJECT_ROOT = Path(settings.BASE_DIR).parent


def project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


class Command(BaseCommand):
    help = "Valida el JSON de lecciones del curso A2 contra el manifest oficial."

    def add_arguments(self, parser):
        parser.add_argument("--manifest", default="content_pipeline/manifests/a2_course_manifest.json")
        parser.add_argument("--lessons", default="data/output/lessons/a2_lessons.json")
        parser.add_argument("--out", default="data/output/reports/a2_validation_report.md")
        parser.add_argument("--tolerance", type=float, default=0.20)

    def handle(self, *args, **options):
        manifest_path = project_path(options["manifest"])
        lessons_path = project_path(options["lessons"])
        out_path = project_path(options["out"])
        if not manifest_path.exists():
            raise CommandError(f"No existe manifest: {manifest_path}")
        if not lessons_path.exists():
            raise CommandError(f"No existe JSON de lecciones: {lessons_path}")
        manifest = read_json(manifest_path)
        lessons = read_json(lessons_path)
        result = validate_lessons(manifest, lessons, tolerance=options["tolerance"])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(result.report, encoding="utf-8")
        self.stdout.write(f"Reporte de validación: {out_path}")
        if result.warnings:
            self.stdout.write(self.style.WARNING(f"Advertencias: {len(result.warnings)}"))
        if not result.is_valid:
            raise CommandError(f"Validación con errores: {len(result.errors)}")
        self.stdout.write(self.style.SUCCESS("Validación A2 OK"))
