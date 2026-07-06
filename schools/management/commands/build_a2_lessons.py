from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from content_pipeline.exporters.json_exporter import read_json, write_json
from content_pipeline.processors.lesson_generator import generate_lessons
from content_pipeline.processors.map_topics import build_mapping_coverage_report, map_topics_to_segments
from content_pipeline.processors.segment_book import segment_pages

PROJECT_ROOT = Path(settings.BASE_DIR).parent


def project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def ensure_exists(path: Path, label: str) -> None:
    if not path.exists():
        raise CommandError(f"No existe {label}: {path}")


class Command(BaseCommand):
    help = "Construye segmentos, mapeos y lecciones del curso profesional A2."

    def add_arguments(self, parser):
        parser.add_argument("--step", choices=["segment", "map", "lessons", "all"], required=True)
        parser.add_argument("--manifest", default="content_pipeline/manifests/a2_course_manifest.json")
        parser.add_argument("--pages", default="data/output/pages/book_pages.json")
        parser.add_argument("--segments", default="data/output/segments/book_segments.json")
        parser.add_argument("--mapping", default="data/output/mappings/topic_segment_mapping.json")
        parser.add_argument("--lessons", default="data/output/lessons/a2_lessons.json")
        parser.add_argument("--coverage-report", default="data/output/reports/a2_mapping_coverage.md")
        parser.add_argument("--min-score", type=float, default=0.20)
        parser.add_argument("--top-k", type=int, default=5)

    def handle(self, *args, **options):
        step = options["step"]
        manifest_path = project_path(options["manifest"])
        pages_path = project_path(options["pages"])
        segments_path = project_path(options["segments"])
        mapping_path = project_path(options["mapping"])
        lessons_path = project_path(options["lessons"])
        coverage_path = project_path(options["coverage_report"])

        try:
            if step in {"segment", "all"}:
                ensure_exists(pages_path, "JSON de páginas")
                pages = read_json(pages_path)
                segments = segment_pages(pages)
                write_json(segments_path, segments)
                self.stdout.write(self.style.SUCCESS(f"Segmentos generados: {len(segments)} -> {segments_path}"))

            if step in {"map", "all"}:
                ensure_exists(manifest_path, "manifest")
                ensure_exists(segments_path, "JSON de segmentos")
                manifest = read_json(manifest_path)
                segments = read_json(segments_path)
                mappings = map_topics_to_segments(
                    manifest,
                    segments,
                    min_score=options["min_score"],
                    top_k=options["top_k"],
                )
                write_json(mapping_path, mappings)
                coverage_path.parent.mkdir(parents=True, exist_ok=True)
                coverage_path.write_text(build_mapping_coverage_report(mappings, segments), encoding="utf-8")
                self.stdout.write(self.style.SUCCESS(f"Mapeos generados: {len(mappings)} -> {mapping_path}"))
                self.stdout.write(f"Reporte de cobertura: {coverage_path}")

            if step in {"lessons", "all"}:
                ensure_exists(manifest_path, "manifest")
                ensure_exists(segments_path, "JSON de segmentos")
                ensure_exists(mapping_path, "JSON de mapeo")
                manifest = read_json(manifest_path)
                segments = read_json(segments_path)
                mappings = read_json(mapping_path)
                lessons = generate_lessons(manifest, segments, mappings)
                write_json(lessons_path, lessons)
                self.stdout.write(self.style.SUCCESS(f"Lecciones generadas: {len(lessons)} -> {lessons_path}"))
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(str(exc)) from exc
