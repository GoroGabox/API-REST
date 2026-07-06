from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from content_pipeline.exporters.json_exporter import read_json, write_json
from content_pipeline.extractors.pdf_text_extractor import extract_pdf_pages
from content_pipeline.processors.lesson_generator import generate_lessons
from content_pipeline.processors.map_topics import build_mapping_coverage_report, map_topics_to_segments
from content_pipeline.processors.segment_book import segment_pages
from content_pipeline.processors.validators import validate_expected_inputs, validate_lessons

PROJECT_ROOT = Path(settings.BASE_DIR).parent


def project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


class Command(BaseCommand):
    help = "Ejecuta extract -> segment -> map -> lessons -> validate para el curso A2."

    def add_arguments(self, parser):
        parser.add_argument("--pdf", default="data/input/libro_nuevo_conductor_a2.pdf")
        parser.add_argument("--anexo", default="data/input/anexo_1_a2.pdf")
        parser.add_argument("--manifest", default="content_pipeline/manifests/a2_course_manifest.json")
        parser.add_argument("--pages", default="data/output/pages/book_pages.json")
        parser.add_argument("--segments", default="data/output/segments/book_segments.json")
        parser.add_argument("--mapping", default="data/output/mappings/topic_segment_mapping.json")
        parser.add_argument("--lessons", default="data/output/lessons/a2_lessons.json")
        parser.add_argument("--coverage-report", default="data/output/reports/a2_mapping_coverage.md")
        parser.add_argument("--validation-report", default="data/output/reports/a2_validation_report.md")
        parser.add_argument("--min-score", type=float, default=0.20)
        parser.add_argument("--top-k", type=int, default=5)

    def handle(self, *args, **options):
        pdf_path = project_path(options["pdf"])
        anexo_path = project_path(options["anexo"])
        manifest_path = project_path(options["manifest"])
        pages_path = project_path(options["pages"])
        segments_path = project_path(options["segments"])
        mapping_path = project_path(options["mapping"])
        lessons_path = project_path(options["lessons"])
        coverage_path = project_path(options["coverage_report"])
        validation_path = project_path(options["validation_report"])

        try:
            validate_expected_inputs([pdf_path, anexo_path, manifest_path])
            manifest = read_json(manifest_path)

            pages = extract_pdf_pages(pdf_path)
            write_json(pages_path, pages)
            self.stdout.write(f"extract: {len(pages)} páginas")

            segments = segment_pages(pages)
            write_json(segments_path, segments)
            self.stdout.write(f"segment: {len(segments)} segmentos")

            mappings = map_topics_to_segments(manifest, segments, options["min_score"], options["top_k"])
            write_json(mapping_path, mappings)
            coverage_path.parent.mkdir(parents=True, exist_ok=True)
            coverage_path.write_text(build_mapping_coverage_report(mappings, segments), encoding="utf-8")
            self.stdout.write(f"map: {len(mappings)} temas")

            lessons = generate_lessons(manifest, segments, mappings)
            write_json(lessons_path, lessons)
            self.stdout.write(f"lessons: {len(lessons)} lecciones")

            validation = validate_lessons(manifest, lessons)
            validation_path.parent.mkdir(parents=True, exist_ok=True)
            validation_path.write_text(validation.report, encoding="utf-8")
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        topic_count = sum(len(unit.get("temas", [])) for unit in manifest.get("unidades", []))
        covered = sum(1 for mapping in mappings if mapping.get("matched_segments"))
        coverage = (covered / topic_count * 100) if topic_count else 0.0
        quiz_count = sum(1 for lesson in lessons if lesson.get("tipo") == "quiz")
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Pipeline A2 completado"))
        self.stdout.write("Curso: A2")
        self.stdout.write(f"Unidades: {len(manifest.get('unidades', []))}")
        self.stdout.write(f"Temas regulatorios: {topic_count}")
        self.stdout.write(f"Lecciones generadas: {len(lessons)}")
        self.stdout.write(f"Quizzes generados: {quiz_count}")
        self.stdout.write(f"Cobertura: {coverage:.1f}%")
        self.stdout.write(f"Reporte: {validation_path}")
        if not validation.is_valid:
            raise CommandError(f"Validación con errores: {len(validation.errors)}")
