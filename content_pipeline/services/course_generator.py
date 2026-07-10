"""Orquestador del generador de cursos.

Encadena el pipeline existente (extract -> segment -> map -> lessons ->
persist) y lo expone como un *stream* de eventos NDJSON pensado para consumo
directo por el frontend (`CourseGenerator.js`):

    {"event": "step",   "step": str, "message": str, "ts": int}
    {"event": "lesson", "lesson": LessonPreview,      "ts": int}
    {"event": "done",   "curso": {id, nombre, codigo}, "total": int, "ts": int}
    {"event": "error",  "message": str,                "ts": int}

Con ANTHROPIC_API_KEY usa el LLM para interpretar el temario
(`manifest_llm`) y redactar las lecciones ancladas a la fuente
(`llm_lesson_writer`). Sin API key cae al pipeline extractivo determinista
(`manifest_builder` + `generic_lesson_generator`). El evento ``done`` incluye
el desglose de tokens/costo de IA cuando aplica.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterator

from content_pipeline.exporters.django_importer import import_generated_course
from content_pipeline.extractors.pdf_text_extractor import extract_pdf_pages
from content_pipeline.llm.client import LLMClient, default_model, draft_model
from content_pipeline.processors.generic_lesson_generator import generate_lessons_generic
from content_pipeline.processors.llm_lesson_writer import generate_lessons_llm
from content_pipeline.processors.manifest_builder import build_manifest_from_temario
from content_pipeline.processors.manifest_llm import build_manifest_from_temario_llm
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

        # Modo IA vs extractivo. Sin API key/SDK -> fallback determinista.
        use_llm = LLMClient.is_available()
        llm_client = LLMClient() if use_llm else None
        final_model = default_model()
        lesson_model = final_model if modo == "final" else draft_model()
        if use_llm:
            yield _event(
                "step",
                step="modo",
                message=f"Generación asistida por IA · temario: {final_model} · lecciones: {lesson_model}.",
            )
        else:
            yield _event(
                "step",
                step="modo",
                message="IA no configurada (sin ANTHROPIC_API_KEY): generación heurística extractiva.",
            )

        yield _event("step", step="temario", message="Leyendo el temario…")
        temario_pages = extract_pdf_pages(Path(temario_path))

        manifest = None
        if use_llm:
            yield _event("step", step="temario_ia", message="Interpretando el temario con IA…")
            try:
                manifest = build_manifest_from_temario_llm(
                    temario_pages,
                    nombre=nombre,
                    codigo=codigo,
                    is_profesional=is_profesional,
                    max_lecciones=max_lecciones,
                    client=llm_client,
                    model=final_model,
                )
            except Exception as exc:  # noqa: BLE001 — degradar a heurística
                yield _event(
                    "step",
                    step="temario_warn",
                    message=f"La IA no pudo interpretar el temario ({exc}); uso heurística.",
                )
                manifest = None
        if manifest is None:
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

        lessons: list[dict[str, Any]] = []
        if use_llm:
            total = n_topics + n_units  # una lección por tema + un quiz por unidad
            yield _event("step", step="redactar", message=f"Redactando {total} lecciones con IA…")
            for index, lesson in enumerate(
                generate_lessons_llm(
                    manifest,
                    segments,
                    mappings,
                    source_name=source_name,
                    client=llm_client,
                    model=lesson_model,
                ),
                start=1,
            ):
                lessons.append(lesson)
                yield _event(
                    "step",
                    step="redactar_prog",
                    message=f"[{index}/{total}] {lesson.get('nombre', '')}",
                )
                yield _event("lesson", lesson=_lesson_preview(lesson))
        else:
            yield _event("step", step="redactar", message="Redactando lecciones…")
            lessons = generate_lessons_generic(manifest, segments, mappings, source_name=source_name)
            for lesson in lessons:
                yield _event("lesson", lesson=_lesson_preview(lesson))

        if use_llm and llm_client is not None:
            meter = llm_client.meter
            yield _event(
                "step",
                step="ia_costo",
                message=(
                    f"IA: {meter.calls} llamadas · "
                    f"{meter.input_tokens + meter.cache_read_tokens + meter.cache_creation_tokens} tok in / "
                    f"{meter.output_tokens} tok out · ~US${meter.cost_usd:.3f}."
                ),
            )

        yield _event("step", step="persistir", message="Guardando el curso y sus lecciones…")
        _summary, curso = import_generated_course(manifest, lessons)
        yield _event("step", step="persistir_ok", message=f"Curso #{curso.id} guardado.")

        done_payload: dict[str, Any] = {
            "curso": {"id": curso.id, "nombre": curso.nombre, "codigo": curso.codigo},
            "total": len(lessons),
        }
        if use_llm and llm_client is not None:
            done_payload["ia"] = llm_client.meter.as_dict()
        yield _event("done", **done_payload)
    except Exception as exc:  # noqa: BLE001 — todo fallo se reporta al cliente vía stream
        yield _event("error", message=str(exc))
