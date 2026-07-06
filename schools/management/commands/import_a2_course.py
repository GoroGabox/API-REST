from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from content_pipeline.exporters.django_importer import import_a2_course
from content_pipeline.exporters.json_exporter import read_json

PROJECT_ROOT = Path(settings.BASE_DIR).parent


def project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


class Command(BaseCommand):
    help = "Importa el curso profesional A2 generado por el pipeline a la base de datos."

    def add_arguments(self, parser):
        parser.add_argument("--manifest", default="content_pipeline/manifests/a2_course_manifest.json")
        parser.add_argument("--lessons", default="data/output/lessons/a2_lessons.json")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        manifest_path = project_path(options["manifest"])
        lessons_path = project_path(options["lessons"])
        if not manifest_path.exists():
            raise CommandError(f"No existe manifest: {manifest_path}")
        if not lessons_path.exists():
            raise CommandError(f"No existe JSON de lecciones: {lessons_path}")
        try:
            summary = import_a2_course(read_json(manifest_path), read_json(lessons_path), dry_run=options["dry_run"])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        for line in summary.as_lines():
            self.stdout.write(line)
        self.stdout.write(self.style.SUCCESS("Importación A2 lista" if not options["dry_run"] else "Dry-run A2 listo"))
