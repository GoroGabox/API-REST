"""Redacta lecciones y quizzes con un LLM, anclado a la fuente (RAG).

Reutiliza la maquinaria determinista de `lesson_generator` (mapeo tema→segmento,
fuentes trazables, títulos, duración) y reemplaza SOLO la redacción del cuerpo:
en vez de pegar oraciones del libro, el LLM escribe una lección pedagógica real
usando únicamente los segmentos mapeados de ese tema. La cita de páginas (##
Fuente) se agrega de forma determinista para garantizar trazabilidad exacta.

Si una llamada al LLM falla, esa lección cae al renderizador extractivo
(`generic_lesson_generator`) para que un fallo puntual no tumbe el curso.
"""
from __future__ import annotations

from typing import Any, Iterator

from content_pipeline.llm.client import LLMClient, default_model, parse_json_object
from content_pipeline.processors.clean_text import shorten_text
from content_pipeline.processors.generic_lesson_generator import (
    _quiz_content as _extractive_quiz,
    render_generic_lesson,
)
from content_pipeline.processors.lesson_generator import (
    SOURCE_NAME,
    _clamp,
    _combined_text,
    _lesson_title,
    _mapping_lookup,
    _matched_segments,
    _quiz_sources,
    _source_markdown,
    _sources_for_segments,
    build_lesson_context,
)

# Tope de fuente por lección (~2k tokens): mantiene el costo bajo y el foco.
_MAX_SOURCE_CHARS = 8_000

LESSON_SYSTEM = """\
Eres un redactor pedagógico experto en cursos de conducción en Chile. Escribes
lecciones e-learning claras, en español neutro, para estudiantes adultos.

Recibes: el tema de una lección, su unidad, los objetivos de aprendizaje y
extractos del material fuente oficial del curso.

Reglas estrictas:
- Enseña el tema con tus palabras; NO copies oraciones literales del material.
- Fundamenta el contenido ÚNICAMENTE en los extractos provistos. Si algo no
  está en la fuente, no lo inventes (especialmente cifras, normativa o
  sanciones). Si la fuente es insuficiente, redacta lo general y sé prudente.
- Tono didáctico y concreto; ejemplos aplicados a la conducción real.
- Escribe en Markdown EXACTAMENTE con estos encabezados y en este orden,
  sin agregar ni quitar secciones, y sin incluir "## Fuente":

# {titulo}

## Objetivo
(1-2 frases: qué podrá hacer el estudiante al terminar)

## Introducción
(2 párrafos: por qué importa el tema)

## Desarrollo
(3-4 párrafos que explican el tema a partir de la fuente)

## Aplicación práctica
(2 párrafos: cómo se usa al conducir)

## Puntos clave
(4-6 viñetas con "- ")

## Ejemplo aplicado
(2 párrafos con una situación concreta de tránsito)

## Errores frecuentes
(4-5 viñetas con "- ")

## Actividad breve
(1 pregunta o ejercicio de reflexión)

## Resumen
(1 párrafo de cierre)

Responde solo con el Markdown de la lección.
"""

LESSON_USER = """\
Tema: {tema}
Unidad: {unidad}
Objetivos de aprendizaje de la unidad:
{objetivos}

Extractos del material fuente (úsalos como base):
---
{fuente}
---
Redacta la lección "{titulo}".
"""

QUIZ_SYSTEM = """\
Eres un evaluador de cursos de conducción. Creas quizzes de opción múltiple en
español, fundamentados en el material fuente. No inventes datos que no estén en
la fuente. Responde SOLO con JSON válido, sin ```fences.

Formato exacto:
{
  "questions": [
    {
      "question": "…",
      "options": ["A", "B", "C", "D"],
      "correct_index": 0,
      "explanation": "por qué es correcta"
    }
  ],
  "passing_score": 75
}
"""

QUIZ_USER = """\
Unidad: {unidad}
Temas de la unidad: {temas}

Extractos del material fuente:
---
{fuente}
---
Crea entre 4 y 6 preguntas de opción múltiple (4 opciones cada una) que evalúen
la comprensión de esta unidad. Devuelve solo el JSON.
"""


def _objetivos_text(objetivos: list[str]) -> str:
    if not objetivos:
        return "(No se especificaron objetivos; guíate por el tema.)"
    return "\n".join(f"- {o}" for o in objetivos)


def _source_for_prompt(segments: list[dict[str, Any]]) -> str:
    text = _combined_text(segments).strip()
    return shorten_text(text, _MAX_SOURCE_CHARS) if text else ""


def _write_body(
    *,
    title: str,
    tema: str,
    unidad_nombre: str,
    objetivos: list[str],
    segments: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    source_name: str,
    client: LLMClient,
    model: str,
    context,
) -> str:
    """Redacta el cuerpo con LLM; ante fallo cae al renderizador extractivo."""
    fuente = _source_for_prompt(segments)
    if not fuente:
        fuente = "(Sin extractos mapeados para este tema en el material fuente.)"
    try:
        body = client.complete(
            system=LESSON_SYSTEM.replace("{titulo}", title),
            user=LESSON_USER.format(
                tema=tema,
                unidad=unidad_nombre,
                objetivos=_objetivos_text(objetivos),
                fuente=fuente,
                titulo=title,
            ),
            max_tokens=1800,
            model=model,
            temperature=0.5,
        )
        if not body.strip():
            raise ValueError("cuerpo vacío")
        # Cita de páginas determinista (trazabilidad exacta, no del LLM).
        return f"{body.rstrip()}\n\n## Fuente\n{_source_markdown(sources, source_name)}"
    except Exception:
        # Fallback: renderizador extractivo neutral (no rompe el curso).
        return render_generic_lesson(context, source_name)


def _write_quiz(
    *,
    unidad_nombre: str,
    temas: list[str],
    segments: list[dict[str, Any]],
    client: LLMClient,
    model: str,
) -> dict[str, Any]:
    fuente = _source_for_prompt(segments) or "(Sin extractos; evalúa lo general de la unidad.)"
    try:
        raw = client.complete(
            system=QUIZ_SYSTEM,
            user=QUIZ_USER.format(
                unidad=unidad_nombre, temas=", ".join(temas), fuente=fuente
            ),
            max_tokens=1500,
            model=model,
            temperature=0.3,
        )
        data = parse_json_object(raw)
        questions = data.get("questions")
        if isinstance(questions, list) and questions:
            return {"questions": questions, "passing_score": int(data.get("passing_score", 75))}
    except Exception:
        pass
    return _extractive_quiz(temas)


def generate_lessons_llm(
    manifest: dict[str, Any],
    segments: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    *,
    source_name: str = SOURCE_NAME,
    client: LLMClient | None = None,
    model: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Genera lecciones con LLM, emitiéndolas una a una (para streaming).

    Misma forma de salida que `generate_lessons_generic` (compatible con
    `import_generated_course`): una lección de texto por tema + un quiz por
    unidad.
    """
    client = client or LLMClient()
    model = model or default_model()
    segment_by_id = {str(s.get("segment_id")): s for s in segments}
    mapping_by_topic = _mapping_lookup(mappings)

    for unidad in manifest.get("unidades", []):
        if not isinstance(unidad, dict):
            continue
        orden = int(unidad.get("orden", 0))
        unidad_nombre = str(unidad.get("nombre", ""))
        categoria = str(unidad.get("categoria", ""))
        objetivos = [str(o) for o in unidad.get("objetivos", []) if str(o).strip()]
        temas = [str(t) for t in unidad.get("temas", [])]
        if not temas:
            continue

        target_minutes = int(unidad.get("horas_elearning", 0)) * 60
        quiz_minutes = 25
        minutes_per_topic = max(10, (target_minutes - quiz_minutes) / len(temas))
        position = 1
        unit_sources: list[dict[str, Any]] = []
        unit_segments: list[dict[str, Any]] = []

        for tema in temas:
            mapping = mapping_by_topic.get((orden, tema))
            matched_segments = _matched_segments(mapping, segment_by_id)
            unit_segments.extend(matched_segments)
            sources = _sources_for_segments(matched_segments, tema, source_name)
            unit_sources.extend(sources)
            duration = _clamp(round(minutes_per_topic), 15, 60)
            title = _lesson_title(tema, 0, 1)
            context = build_lesson_context(
                title, tema, unidad_nombre, 0, 1, matched_segments, sources
            )
            body = _write_body(
                title=title,
                tema=tema,
                unidad_nombre=unidad_nombre,
                objetivos=objetivos,
                segments=matched_segments,
                sources=sources,
                source_name=source_name,
                client=client,
                model=model,
                context=context,
            )
            yield {
                "unidad_orden": orden,
                "unidad_nombre": unidad_nombre,
                "categoria": categoria,
                "tema_regulatorio": tema,
                "nombre": title,
                "posicion": position,
                "tipo": "texto",
                "descripcion": shorten_text(
                    f"Estudia {tema} dentro de {unidad_nombre} y aplícalo en situaciones concretas.",
                    240,
                ),
                "duracion_min": duration,
                "contenido": body,
                "transcripcion": "",
                "fuentes": sources,
            }
            position += 1

        yield {
            "unidad_orden": orden,
            "unidad_nombre": unidad_nombre,
            "categoria": categoria,
            "tema_regulatorio": f"Evaluación módulo {orden}",
            "nombre": f"Evaluación del módulo {orden}",
            "posicion": position,
            "tipo": "quiz",
            "descripcion": f"Evaluación de cierre de la unidad {unidad_nombre}.",
            "duracion_min": quiz_minutes,
            "contenido": _write_quiz(
                unidad_nombre=unidad_nombre,
                temas=temas,
                segments=unit_segments,
                client=client,
                model=model,
            ),
            "transcripcion": "",
            "fuentes": _quiz_sources(unit_sources, unidad_nombre, orden, source_name),
        }
