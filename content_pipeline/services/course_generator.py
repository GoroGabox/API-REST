"""Orquestador del generador de cursos.

Encadena el pipeline existente (extract -> segment -> map -> lessons ->
persist) y lo expone como un *stream* de eventos NDJSON pensado para consumo
directo por el frontend (`CourseGenerator.js`):

    {"event": "step",   "step": str, "message": str, "ts": int}
    {"event": "lesson", "lesson": LessonPreview,      "ts": int}
    {"event": "done",   "curso": {id, nombre, codigo}, "total": int, "ts": int}
    {"event": "error",  "message": str,                "ts": int}

Se apoya en los procesadores de `content_pipeline` sin duplicar lógica; la
única pieza nueva es `manifest_builder`, que deriva la estructura del curso a
partir del PDF de temario.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterator

from content_pipeline.exporters.django_importer import import_generated_course
from content_pipeline.extractors.pdf_text_extractor import extract_pdf_pages
from content_pipeline.processors.generic_lesson_generator import generate_lessons_generic
from content_pipeline.processors.manifest_builder import build_manifest_from_temario
from content_pipeline.processors.map_topics import map_topics_to_segments
from content_pipeline.processors.segment_book import segment_pages


def _event(kind: str, **data: Any) -> dict[str, Any]:
    return {"event": kind, "ts": int(time.time() * 1000), **data}


def _lesson_preview(lesson: dict[str, Any]) -> dict[str, Any]:
    """Proyecta una lección generada a la forma LessonPreview del frontend."""
    return {
        "unidad": lesson.get("unidad_orden"),
        "posicion": lesson.get("posicion"),
        "tipo": lesson.get("tipo"),
        "nombre": lesson.get("nombre"),
        "duracion_min": lesson.get("duracion_min"),
        "categoria_nombre": lesson.get("categoria"),
    }


def generate_course_stream(
    *,
    temario_path: str | Path,
    contenido_path: str | Path,
    nombre: str,
    codigo: str,
    is_profesional: bool = False,
    max_lecciones: int = 20,
    idioma: str = "es",
    modo: str = "draft",
    source_name: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Genera un curso completo emitiendo eventos de progreso.

    Cualquier excepción se captura y se emite como evento ``error`` para que el
    cliente pueda mostrarla en el log en vez de recibir un stream truncado.
    """
    try:
        source_name = source_name or f"Contenido: {nombre}"

        yield _event("step", step="temario", message="Leyendo el temario…")
        temario_pages = extract_pdf_pages(Path(temario_path))
        manifest = build_manifest_from_temario(
            temario_pages,
            nombre=nombre,
            codigo=codigo,
            is_profesional=is_profesional,
            max_lecciones=max_lecciones,
        )
        n_units = len(manifest["unidades"])
        n_topics = sum(len(unit["temas"]) for unit in manifest["unidades"])
        yield _event(
            "step",
            step="temario_ok",
            message=f"Temario interpretado: {n_units} unidades · {n_topics} temas.",
        )

        yield _event("step", step="contenido", message="Extrayendo el contenido fuente…")
        content_pages = extract_pdf_pages(Path(contenido_path))
        yield _event(
            "step",
            step="contenido_ok",
            message=f"{len(content_pages)} páginas de contenido extraídas.",
        )

        yield _event("step", step="segmentar", message="Segmentando el contenido por temas…")
        segments = segment_pages(content_pages)
        yield _event("step", step="segmentar_ok", message=f"{len(segments)} segmentos generados.")

        yield _event("step", step="mapear", message="Asociando cada tema con su fuente…")
        mappings = map_topics_to_segments(manifest, segments)
        covered = sum(1 for mapping in mappings if mapping.get("matched_segments"))
        yield _event(
            "step",
            step="mapear_ok",
            message=f"{covered}/{len(mappings)} temas con fuente encontrada.",
        )

        yield _event("step", step="redactar", message="Redactando lecciones…")
        lessons = generate_lessons_generic(manifest, segments, mappings, source_name=source_name)

        for lesson in lessons:
            yield _event("lesson", lesson=_lesson_preview(lesson))

        yield _event("step", step="persistir", message="Guardando el curso y sus lecciones…")
        _summary, curso = import_generated_course(manifest, lessons)
        yield _event("step", step="persistir_ok", message=f"Curso #{curso.id} guardado.")

        yield _event(
            "done",
            curso={"id": curso.id, "nombre": curso.nombre, "codigo": curso.codigo},
            total=len(lessons),
        )
    except Exception as exc:  # noqa: BLE001 — todo fallo se reporta al cliente vía stream
        yield _event("error", message=str(exc))
