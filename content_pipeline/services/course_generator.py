"""Orquestador del generador de cursos.

Encadena el pipeline existente (extract -> segment -> map -> lessons ->
persist) y lo expone como un *stream* de eventos NDJSON pensado para consumo
directo por el frontend (`CourseGenerator.js`):

    {"event": "step",   "step": str, "message": str, "ts": int}
    {"event": "warn",   "step": str, "message": str, ..., "ts": int}
    {"event": "lesson", "lesson": LessonPreview,      "ts": int}
    {"event": "done",   "curso": {id, nombre, codigo}, "total": int, "ts": int}
    {"event": "error",  "message": str,                "ts": int}

El curso se arma desde el CONTENIDO del libro completo (`manifest_from_content`),
no desde el temario. El temario se usa como *checklist*: al final se valida que
sus temas estén representados en el curso generado y se avisa de los faltantes.
El curso se dimensiona al tamaño del libro (`course_planning`) para no truncarlo.

Con ANTHROPIC_API_KEY usa el LLM para inferir la estructura y redactar las
lecciones ancladas a la fuente (`llm_lesson_writer`). Sin API key cae al pipeline
extractivo determinista (`generic_lesson_generator`). El evento ``done`` incluye
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
from content_pipeline.processors.manifest_from_content import (
    build_manifest_from_content,
    build_manifest_from_content_llm,
)
from content_pipeline.licenses import orientation_for
from content_pipeline.processors.faithfulness import audit_lessons
from content_pipeline.processors.map_topics import coverage_alert, map_topics_to_segments
from content_pipeline.processors.segment_book import segment_pages
from content_pipeline.services.course_planning import (
    extract_temario_topics,
    resolve_max_lecciones,
    truncation_notes,
    validate_topics_present,
)


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
        "fuente_debil": bool(lesson.get("fuente_debil")),
    }


def generate_course_stream(
    *,
    temario_path: str | Path | None,
    contenido_path: str | Path,
    nombre: str,
    codigo: str,
    costo: int,
    is_profesional: bool = False,
    max_lecciones: int | None = None,
    idioma: str = "es",
    modo: str = "draft",
    source_name: str | None = None,
    orientacion: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Genera un curso completo emitiendo eventos de progreso.

    ``orientacion`` enfoca el curso hacia una licencia (útil cuando A2/A4/A5
    salen del mismo libro): si no se pasa, se deriva del ``codigo``.

    Cualquier excepción se captura y se emite como evento ``error`` para que el
    cliente pueda mostrarla en el log en vez de recibir un stream truncado.
    """
    try:
        source_name = source_name or f"Contenido: {nombre}"
        orientacion = orientation_for(codigo, orientacion)

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

        if orientacion:
            yield _event("step", step="orientacion",
                         message=f"Orientación del curso ({codigo}): {orientacion}")

        # 1. CONTENIDO primero: es la fuente de la estructura y de las lecciones.
        yield _event("step", step="contenido", message="Extrayendo el contenido fuente…")
        content_pages = extract_pdf_pages(Path(contenido_path))
        yield _event(
            "step",
            step="contenido_ok",
            message=f"{len(content_pages)} páginas de contenido extraídas.",
        )

        yield _event("step", step="segmentar", message="Segmentando el contenido…")
        segments = segment_pages(content_pages)
        yield _event("step", step="segmentar_ok", message=f"{len(segments)} segmentos generados.")

        # 2. Dimensionar el curso al tamaño del libro (evita recortes hardcodeados).
        max_lec, origen = resolve_max_lecciones(len(segments), max_lecciones)
        yield _event(
            "step",
            step="dimension",
            message=(
                f"Objetivo: hasta {max_lec} lecciones para cubrir el libro "
                f"({'fijado por el operador' if origen == 'operador' else 'auto-dimensionado'})."
            ),
        )
        for nota in truncation_notes(len(segments), max_lec, origen):
            yield _event("warn", step="truncamiento", message=f"⚠ {nota}")

        # 3. ESTRUCTURA desde el libro COMPLETO (no desde el temario).
        yield _event("step", step="estructura", message="Infiriendo la estructura desde el libro…")
        manifest = None
        if use_llm:
            try:
                manifest = build_manifest_from_content_llm(
                    segments, nombre=nombre, codigo=codigo,
                    is_profesional=is_profesional, max_lecciones=max_lec,
                    orientacion=orientacion, client=llm_client, model=final_model,
                )
            except Exception as exc:  # noqa: BLE001 — degradar a heurística
                yield _event(
                    "step",
                    step="estructura_warn",
                    message=f"La IA no pudo inferir la estructura ({exc}); uso heurística.",
                )
                manifest = None
        if manifest is None:
            manifest = build_manifest_from_content(
                segments, nombre=nombre, codigo=codigo,
                is_profesional=is_profesional, max_lecciones=max_lec,
            )

        # El costo lo define el operador (obligatorio, > 0): sobrescribe el
        # placeholder del generador de estructura.
        manifest["curso"]["costo"] = int(costo)

        n_units = len(manifest["unidades"])
        n_topics = sum(len(unit["temas"]) for unit in manifest["unidades"])
        yield _event(
            "step",
            step="estructura_ok",
            message=f"Estructura del libro: {n_units} unidades · {n_topics} temas.",
        )

        # 4. Mapear cada tema generado con su fuente (para la redacción). La
        # procedencia (segmentos que el LLM ancló a cada tema) se usa primero;
        # el resto cae al mapeo lexical. Se saca del manifest para no persistirla.
        provenance = manifest.pop("_provenance", None)
        yield _event("step", step="mapear", message="Asociando cada tema con su fuente…")
        mappings = map_topics_to_segments(manifest, segments, provenance=provenance)
        cobertura = coverage_alert(mappings)
        n_prov = sum(
            1 for m in mappings
            if (m.get("matched_segments") or [{}])[0].get("reason", "").startswith("Procedencia")
        )
        yield _event(
            "step",
            step="mapear_ok",
            message=(
                f"{len(cobertura['solid'])}/{cobertura['total']} temas con fuente sólida "
                f"({n_prov} por procedencia del generador)."
            ),
        )
        # Alerta: temas mal anclados (solo match débil o sin fuente). Sus lecciones
        # se degradan al extractivo neutral (no alucinan) y deben revisarse: suele
        # ser un tema real del libro mapeado a la sección equivocada.
        debiles = list(cobertura["weak"]) + list(cobertura["uncovered"])
        if debiles:
            detalle = "; ".join(debiles[:12])
            if len(debiles) > 12:
                detalle += f"; … (+{len(debiles) - 12} más)"
            yield _event(
                "warn",
                step="mapeo_debil",
                message=(
                    f"⚠ {len(debiles)} tema(s) sin fuente sólida en el libro "
                    f"({len(cobertura['weak'])} match débil · {len(cobertura['uncovered'])} sin fuente). "
                    f"Su lección se degrada (sin inventar) — revisar el mapeo: {detalle}"
                ),
                temas_debiles=list(cobertura["weak"]),
                temas_sin_fuente=list(cobertura["uncovered"]),
            )

        # 5. TEMARIO como checklist: validar que sus temas estén en el curso.
        if temario_path is not None:
            yield _event("step", step="temario", message="Leyendo el temario para validación…")
            temario_pages = extract_pdf_pages(Path(temario_path))
            expected = extract_temario_topics(
                temario_pages, nombre=nombre, codigo=codigo,
                is_profesional=is_profesional, use_llm=use_llm,
                client=llm_client, model=final_model,
            )
            validacion = validate_topics_present(expected, manifest)
            presentes = len(validacion["present"])
            yield _event(
                "step",
                step="temario_ok",
                message=f"Temario: {presentes}/{validacion['expected']} temas presentes en el curso.",
            )
            faltantes = validacion["missing"]
            if faltantes:
                detalle = "; ".join(faltantes[:12])
                if len(faltantes) > 12:
                    detalle += f"; … (+{len(faltantes) - 12} más)"
                yield _event(
                    "warn",
                    step="temario_faltante",
                    message=(
                        f"⚠ {len(faltantes)} tema(s) del temario no están representados en "
                        f"el curso generado del libro. Revisar: {detalle}"
                    ),
                    temas_faltantes=list(faltantes),
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
                    orientacion=orientacion,
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

        # Auditoría de fidelidad (reporte, no bloquea): cifras sin respaldo en la
        # fuente + lecciones con bajo anclaje al libro.
        audit = audit_lessons(lessons, segments, mappings)
        yield _event(
            "step",
            step="fidelidad_ok",
            message=(
                f"Fidelidad: {audit['auditadas']} lecciones auditadas · "
                f"{len(audit['figuras'])} con cifras sin respaldo · "
                f"{len(audit['anclaje_bajo'])} con bajo anclaje."
            ),
        )
        if audit["figuras"]:
            det = "; ".join(f"{f['leccion']} [{', '.join(f['cifras'])}]" for f in audit["figuras"][:10])
            if len(audit["figuras"]) > 10:
                det += f"; … (+{len(audit['figuras']) - 10} más)"
            yield _event(
                "warn",
                step="fidelidad_cifras",
                message=(
                    f"⚠ {len(audit['figuras'])} lección(es) con cifras que NO aparecen en la "
                    f"fuente (posible dato inventado): {det}"
                ),
                lecciones=audit["figuras"],
            )
        if audit["anclaje_bajo"]:
            det = "; ".join(f"{a['leccion']} ({a['anclaje']})" for a in audit["anclaje_bajo"][:10])
            if len(audit["anclaje_bajo"]) > 10:
                det += f"; … (+{len(audit['anclaje_bajo']) - 10} más)"
            yield _event(
                "warn",
                step="fidelidad_anclaje",
                message=(
                    f"⚠ {len(audit['anclaje_bajo'])} lección(es) con bajo anclaje al libro "
                    f"(< {audit['anchor_min']}): {det}"
                ),
                lecciones=audit["anclaje_bajo"],
            )

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
