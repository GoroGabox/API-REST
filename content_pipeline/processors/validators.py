from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ValidationResult:
    errors: list[str]
    warnings: list[str]
    report: str

    @property
    def is_valid(self) -> bool:
        return not self.errors


def validate_expected_inputs(paths: list[Path]) -> None:
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        joined = "\n- ".join(missing)
        raise FileNotFoundError(f"Faltan archivos de entrada requeridos:\n- {joined}")


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    curso = manifest.get("curso", {})
    if not isinstance(curso, dict):
        return ["El manifest no contiene un objeto curso válido."]
    if curso.get("codigo") != "A2":
        errors.append("El curso del manifest debe tener codigo A2.")
    if not curso.get("nombre"):
        errors.append("El curso del manifest debe tener nombre.")
    unidades = manifest.get("unidades", [])
    if not isinstance(unidades, list) or len(unidades) != 7:
        errors.append("El manifest debe contener exactamente 7 unidades.")
    for unidad in unidades if isinstance(unidades, list) else []:
        if not isinstance(unidad, dict):
            errors.append("Cada unidad debe ser un objeto.")
            continue
        if not unidad.get("orden"):
            errors.append(f"La unidad {unidad.get('nombre', '(sin nombre)')} no tiene orden.")
        if not unidad.get("nombre"):
            errors.append("Hay una unidad sin nombre.")
        temas = unidad.get("temas", [])
        if not isinstance(temas, list) or not temas:
            errors.append(f"La unidad {unidad.get('nombre', '(sin nombre)')} no tiene temas.")
    return errors


def _content_is_present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list)):
        return bool(value)
    return value is not None


def _validate_quiz_content(value: Any, label: str, errors: list[str]) -> None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            errors.append(f"{label}: el contenido quiz no es JSON válido.")
            return
    if not isinstance(value, dict):
        errors.append(f"{label}: el contenido quiz debe ser un objeto JSON.")
        return
    questions = value.get("questions")
    if not isinstance(questions, list) or not questions:
        errors.append(f"{label}: el quiz debe tener preguntas.")
        return
    for index, question in enumerate(questions, start=1):
        if not isinstance(question, dict):
            errors.append(f"{label}: pregunta {index} inválida.")
            continue
        options = question.get("options")
        correct_index = question.get("correct_index")
        if not isinstance(options, list) or len(options) < 2:
            errors.append(f"{label}: pregunta {index} debe tener opciones.")
        if not isinstance(correct_index, int) or correct_index < 0 or correct_index >= len(options or []):
            errors.append(f"{label}: pregunta {index} tiene correct_index inválido.")


def validate_lessons(
    manifest: dict[str, Any],
    lessons: list[dict[str, Any]],
    tolerance: float = 0.20,
) -> ValidationResult:
    errors = validate_manifest(manifest)
    warnings: list[str] = []
    unidades = [unidad for unidad in manifest.get("unidades", []) if isinstance(unidad, dict)]

    lessons_by_unit: dict[int, list[dict[str, Any]]] = {}
    for lesson in lessons:
        lessons_by_unit.setdefault(int(lesson.get("unidad_orden", 0)), []).append(lesson)

    for unidad in unidades:
        orden = int(unidad.get("orden", 0))
        unidad_lessons = lessons_by_unit.get(orden, [])
        if not unidad_lessons:
            errors.append(f"U{orden}: no tiene lecciones generadas.")
            continue
        for tema in unidad.get("temas", []):
            has_topic = any(
                lesson.get("tipo") != "quiz" and lesson.get("tema_regulatorio") == tema
                for lesson in unidad_lessons
            )
            if not has_topic:
                errors.append(f"U{orden}: falta al menos una lección para el tema '{tema}'.")
        has_quiz = any(lesson.get("tipo") == "quiz" for lesson in unidad_lessons)
        if not has_quiz:
            errors.append(f"U{orden}: falta quiz de cierre de módulo.")

        target_minutes = int(unidad.get("horas_elearning", 0)) * 60
        total_minutes = sum(int(lesson.get("duracion_min") or 0) for lesson in unidad_lessons)
        lower = target_minutes * (1 - tolerance)
        upper = target_minutes * (1 + tolerance)
        if target_minutes and not lower <= total_minutes <= upper:
            warnings.append(
                f"U{orden}: duración {total_minutes} min fuera de tolerancia respecto de {target_minutes} min."
            )

    for lesson in lessons:
        label = f"U{lesson.get('unidad_orden')} P{lesson.get('posicion')} {lesson.get('nombre', '(sin nombre)')}"
        if not lesson.get("nombre"):
            errors.append(f"{label}: falta nombre.")
        if not lesson.get("descripcion"):
            errors.append(f"{label}: falta descripción.")
        if not _content_is_present(lesson.get("contenido")):
            errors.append(f"{label}: falta contenido.")
        if int(lesson.get("duracion_min") or 0) <= 0:
            errors.append(f"{label}: duración debe ser mayor a 0.")
        sources = lesson.get("fuentes")
        if not isinstance(sources, list) or not sources:
            errors.append(f"{label}: falta fuente trazable.")
        else:
            for source_index, source in enumerate(sources, start=1):
                page_start = int(source.get("pagina_inicio") or 0)
                page_end = int(source.get("pagina_fin") or 0)
                if not source.get("fuente_nombre"):
                    errors.append(f"{label}: fuente {source_index} no tiene nombre.")
                if page_start <= 0 or page_end <= 0 or page_end < page_start:
                    errors.append(f"{label}: fuente {source_index} tiene páginas inválidas.")
        if lesson.get("tipo") == "quiz":
            _validate_quiz_content(lesson.get("contenido"), label, errors)

    report = build_validation_report(manifest, lessons, errors, warnings)
    return ValidationResult(errors=errors, warnings=warnings, report=report)


def build_validation_report(
    manifest: dict[str, Any],
    lessons: list[dict[str, Any]],
    errors: list[str],
    warnings: list[str],
) -> str:
    unidades = [unidad for unidad in manifest.get("unidades", []) if isinstance(unidad, dict)]
    text_lessons = [lesson for lesson in lessons if lesson.get("tipo") == "texto"]
    quiz_lessons = [lesson for lesson in lessons if lesson.get("tipo") == "quiz"]
    total_minutes = sum(int(lesson.get("duracion_min") or 0) for lesson in lessons)
    lines = [
        "# Reporte de validación A2",
        "",
        f"- Curso: {manifest.get('curso', {}).get('codigo', '(sin código)')}",
        f"- Unidades manifest: {len(unidades)}",
        f"- Lecciones texto: {len(text_lessons)}",
        f"- Quizzes: {len(quiz_lessons)}",
        f"- Duración total generada: {total_minutes} min",
        f"- Estado: {'OK' if not errors else 'CON ERRORES'}",
        "",
        "## Errores",
    ]
    if errors:
        lines.extend(f"- {error}" for error in errors)
    else:
        lines.append("- Ninguno")
    lines.extend(["", "## Advertencias"])
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- Ninguna")
    lines.extend(["", "## Duración por unidad"])
    for unidad in unidades:
        orden = int(unidad.get("orden", 0))
        unit_minutes = sum(
            int(lesson.get("duracion_min") or 0)
            for lesson in lessons
            if int(lesson.get("unidad_orden", 0)) == orden
        )
        target = int(unidad.get("horas_elearning", 0)) * 60
        lines.append(f"- U{orden} {unidad.get('nombre')}: {unit_minutes} min / objetivo {target} min")
    lines.append("")
    return "\n".join(lines)
