"""Generación de un curso DESDE un plan aprobado, como stream de eventos (web).

Misma lógica que ``generate_course --from-plan`` pero expuesta como stream NDJSON
para ``CourseGenerateView`` / ``CourseGenerator.js``:

    {"event": "step",      "step": str, "message": str, "ts": int}
    {"event": "warn",      "step": str, "message": str, ..., "ts": int}
    {"event": "lesson",    "lesson": LessonPreview, "ts": int}
    {"event": "bloqueado", "bloqueos": [str], "curso_json": {...}, "ts": int}
    {"event": "generado",  "curso_json": {...}, "total": int, "ts": int}   ← persist=False
    {"event": "done",      "curso": {id, nombre, codigo}, "total": int, "curso_json": {...}, "ts": int}
    {"event": "error",     "message": str, "ts": int}

Diferencia clave con el flujo legacy: la estructura NO se infiere (viene del plan
que el operador revisó y aprobó), cada lección se redacta desde su fuente exacta
y el curso NO se importa si la validación o el juez encuentran bloqueos
(lecciones sin redactar, críticas de fidelidad, claves de quiz dudosas): en ese
caso el evento ``bloqueado`` entrega el curso generado para revisarlo (Brújula) o
importarlo explícitamente con ``POST courses/import/`` + ``forzar``.
"""
from __future__ import annotations

from typing import Any, Iterator

from content_pipeline.exporters.django_importer import import_generated_course
from content_pipeline.licenses import orientation_for
from content_pipeline.llm.client import LLMClient, default_model, draft_model
from content_pipeline.processors.faithfulness import (
    COVERAGE_MIN,
    JUDGE_MIN,
    audit_lessons_from_plan,
    build_lesson_sources_from_plan,
    judge_lessons_llm,
    judge_quiz_llm,
)
from content_pipeline.processors.llm_lesson_writer import generate_lessons_from_plan
from content_pipeline.processors.validators import import_blockers, validate_generated_course
from content_pipeline.services.course_generator import _event, _lesson_preview


def is_plan(data: Any) -> bool:
    """Forma mínima de un plan.json (salida de ``plan_course``)."""
    return (isinstance(data, dict) and isinstance(data.get("unidades"), list)
            and any(isinstance(u, dict) and isinstance(u.get("lecciones"), list) for u in data["unidades"]))


def manifest_from_plan(plan: dict[str, Any], *, nombre: str, codigo: str, costo: int,
                       is_profesional: bool = False) -> dict[str, Any]:
    """Manifest importable desde el plan (una unidad por capítulo, temas = lecciones)."""
    curso = plan.get("curso", {}) or {}
    return {
        "curso": {
            "nombre": nombre, "codigo": codigo,
            "descripcion": curso.get("descripcion", f"Curso generado de {nombre}."),
            "is_profesional": bool(is_profesional) or bool(curso.get("is_profesional")),
            "costo": costo,
        },
        "unidades": [
            {"orden": u["orden"], "nombre": u["nombre"], "categoria": u.get("categoria", "General"),
             "horas_elearning": 0, "temas": [l["nombre"] for l in u.get("lecciones", [])]}
            for u in plan.get("unidades", [])
        ],
    }


def combine_judge(parts: list[dict[str, Any]], *, judge_min: float = JUDGE_MIN,
                  coverage_min: float = COVERAGE_MIN) -> dict[str, Any]:
    """Une resultados parciales de ``judge_lessons_llm`` (una llamada por lección)."""
    def cat(key: str) -> list[Any]:
        return [x for p in parts for x in (p.get(key) or [])]

    ev = sum(p.get("evaluadas", 0) for p in parts)
    scores = [(p["promedio"], p["evaluadas"]) for p in parts if p.get("promedio") is not None]
    covs = [(p["promedio_cobertura"], p["evaluadas"]) for p in parts if p.get("promedio_cobertura") is not None]

    def wavg(pairs: list[tuple[float, int]]) -> float | None:
        n = sum(w for _, w in pairs)
        return round(sum(v * w for v, w in pairs) / n, 3) if n else None

    return {
        "evaluadas": ev,
        "promedio": wavg(scores),
        "judge_min": judge_min,
        "criticas": sorted(cat("criticas"), key=lambda x: x["score"]),
        "con_reparos": sorted(cat("con_reparos"), key=lambda x: x["score"]),
        "promedio_cobertura": wavg(covs),
        "coverage_min": coverage_min,
        "cobertura_baja": sorted(cat("cobertura_baja"), key=lambda x: x["coverage"]),
        "errores": cat("errores"),
    }


def _join(items: list[str], limit: int = 8) -> str:
    return "; ".join(items[:limit]) + (f"; … (+{len(items) - limit} más)" if len(items) > limit else "")


def generate_course_from_plan_stream(
    plan: dict[str, Any],
    *,
    nombre: str,
    codigo: str,
    costo: int,
    is_profesional: bool = False,
    source_name: str | None = None,
    orientacion: str | None = None,
    judge: bool = True,
    lesson_model: str | None = None,
    judge_model: str | None = None,
    client: LLMClient | None = None,
    persist: bool = True,
) -> Iterator[dict[str, Any]]:
    """Redacta, audita, valida y (si no hay bloqueos) importa el curso del plan."""
    try:
        if not is_plan(plan):
            raise ValueError("El plan no tiene unidades con lecciones (¿es un plan.json de plan_course?).")
        use_llm = client is not None or LLMClient.is_available()
        if not use_llm:
            raise ValueError("La redacción desde plan requiere IA (ANTHROPIC_API_KEY no configurada).")
        client = client or LLMClient()
        lesson_model = lesson_model or draft_model()          # redacción: Haiku por defecto
        judge_model = judge_model or default_model()          # juez: Sonnet por defecto
        source_name = source_name or f"Libro: {nombre}"
        orientacion = orientation_for(codigo, orientacion)

        n_u = len(plan["unidades"])
        n_l = sum(len(u.get("lecciones", [])) for u in plan["unidades"])
        total = n_l + n_u
        yield _event("step", step="plan", message=f"Plan aprobado: {n_u} unidades · {n_l} lecciones · redacción con {lesson_model}.")
        if orientacion:
            yield _event("step", step="orientacion", message=f"Orientación ({codigo}): {orientacion}")
        visuales = [(u, i, l) for u in plan["unidades"] for i, l in enumerate(u.get("lecciones", []), 1)
                    if (l.get("visual") or {}).get("nivel") == "alta"]
        if visuales:
            yield _event("warn", step="revision_visual", message=(
                "⚠ Revisión visual necesaria (el libro las explica con imágenes que el texto no captura): "
                + _join([f"U{u['orden']}.{i} {l.get('nombre', '')}" for u, i, l in visuales])))

        lessons: list[dict[str, Any]] = []
        for index, lesson in enumerate(generate_lessons_from_plan(
                plan, source_name=source_name, orientacion=orientacion, client=client, model=lesson_model),
                start=1):
            lessons.append(lesson)
            yield _event("step", step="redactar_prog", message=f"[{index}/{total}] {lesson.get('nombre', '')}")
            yield _event("lesson", lesson=_lesson_preview(lesson))
            if lesson.get("fuente_debil"):
                que = "quiz sin preguntas válidas" if lesson.get("tipo") == "quiz" else "no se pudo redactar"
                yield _event("warn", step="stub", message=f"⚠ U{lesson.get('unidad_orden')} · {lesson.get('nombre')}: {que} (queda para revisión).")
            meta = lesson.get("quiz_meta")
            if meta:
                sin = meta.get("lecciones_sin_preguntas") or []
                yield _event("step" if not sin else "warn", step="quiz",
                             message=(f"Quiz U{lesson.get('unidad_orden')}: {meta['preguntas']} preguntas de "
                                      f"{meta['lecciones']} lecciones · {meta['descartadas']} descartadas"
                                      + (f" · sin preguntas: {', '.join(sin)}" if sin else "")))

        audit = audit_lessons_from_plan(lessons)
        yield _event("step", step="fidelidad_ok", message=(
            f"Fidelidad: {audit['auditadas']} auditadas · {len(audit['figuras'])} con cifras sin respaldo · "
            f"{len(audit['anclaje_bajo'])} con bajo anclaje."))
        if audit["figuras"]:
            det = _join([f"{f['leccion']} [{', '.join(f['cifras'])}]" for f in audit["figuras"]])
            yield _event("warn", step="fidelidad_cifras",
                         message=f"⚠ Cifras que su fuente no respalda: {det}", lecciones=audit["figuras"])

        auditoria: dict[str, Any] = dict(audit)
        auditoria["quiz"] = [{"unidad": l.get("unidad_orden"), **(l.get("quiz_meta") or {})}
                             for l in lessons if l.get("tipo") == "quiz"]

        if judge:
            pairs = build_lesson_sources_from_plan(lessons)
            yield _event("step", step="juez", message=f"Juez de fidelidad ({judge_model}) · {len(pairs)} lecciones…")
            parts = []
            for k, pair in enumerate(pairs, start=1):
                parts.append(judge_lessons_llm([pair], client=client, model=judge_model))
                yield _event("step", step="juez_prog", message=f"Juez [{k}/{len(pairs)}] {pair[0].get('nombre', '')}")
            juez = combine_judge(parts)
            auditoria["juez"] = juez
            yield _event("step", step="juez_ok", message=(
                f"Juez: fidelidad {juez['promedio']} · {len(juez['criticas'])} críticas · "
                f"{len(juez['con_reparos'])} con reparos · cobertura {juez['promedio_cobertura']}."))
            if juez["criticas"]:
                yield _event("warn", step="juez_criticas",
                             message="⚠ Fidelidad crítica: " + _join([f"{c['leccion']} ({c['score']})" for c in juez["criticas"]]),
                             lecciones=juez["criticas"])
            if juez["cobertura_baja"]:
                yield _event("warn", step="juez_cobertura",
                             message="⚠ Omiten datos importantes: " + _join([f"{c['leccion']} ({c['coverage']})" for c in juez["cobertura_baja"]]),
                             lecciones=juez["cobertura_baja"])
            quizzes = [l for l in lessons if l.get("tipo") == "quiz"]
            jq = judge_quiz_llm(quizzes, client=client, model=judge_model)
            auditoria["juez_quiz"] = jq
            yield _event("step" if not jq["problemas"] else "warn", step="juez_quiz", message=(
                f"Juez de quiz: {jq['evaluadas']} preguntas · {len(jq['problemas'])} con clave dudosa."))

        manifest = manifest_from_plan(plan, nombre=nombre, codigo=codigo, costo=costo, is_profesional=is_profesional)
        for l in lessons:
            l.pop("_source_text", None)
        payload: dict[str, Any] = {"manifest": manifest, "lessons": lessons, "auditoria": auditoria}
        payload["validacion"] = validate_generated_course(manifest, lessons)
        payload["ia"] = client.meter.as_dict() if getattr(client, "meter", None) else None
        v = payload["validacion"]
        yield _event("step", step="validacion", message=(
            f"Validación: {len(v['errores'])} errores · {len(v['stubs'])} sin redactar · {len(v['advertencias'])} advertencias."))
        if payload["ia"]:
            yield _event("step", step="ia_costo",
                         message=f"IA: {payload['ia']['llamadas']} llamadas · ~US${payload['ia']['costo_usd']:.3f}.")

        bloqueos, _ = import_blockers(payload)
        if bloqueos:
            yield _event("bloqueado", bloqueos=bloqueos, curso_json=payload, total=len(lessons))
            return
        if not persist:
            # "Solo generar": el curso no toca la BD; el JSON se descarga para revisarlo
            # (Brújula) o importarlo donde corresponda (p. ej. producción con import_course).
            yield _event("generado", curso_json=payload, total=len(lessons), ia=payload["ia"])
            return

        yield _event("step", step="persistir", message="Guardando el curso y sus lecciones…")
        _summary, curso = import_generated_course(manifest, lessons)
        yield _event("step", step="persistir_ok", message=f"Curso #{curso.id} guardado.")
        yield _event("done", curso={"id": curso.id, "nombre": curso.nombre, "codigo": curso.codigo},
                     total=len(lessons), ia=payload["ia"], curso_json=payload)
    except Exception as exc:  # noqa: BLE001 — todo fallo se reporta al cliente vía stream
        yield _event("error", message=str(exc))
