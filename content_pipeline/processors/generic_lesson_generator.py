"""Generador de lecciones con prosa NEUTRAL (agnóstica al dominio).

`lesson_generator.py` está redactado para el curso de conducción Clase A2: su
andamiaje pedagógico habla de "conductor profesional", "vía", "pasajeros",
etc. Eso es correcto para A2 (y sus validadores incluso *exigen* términos
viales concretos), pero inapropiado para un curso generado a partir de PDFs
arbitrarios.

Este módulo reutiliza la *maquinaria* de extracción de `lesson_generator`
(scoring de oraciones, contexto de lección, fuentes trazables, quizzes) pero
reemplaza todo el texto redactado por prosa neutral construida alrededor del
`tema` y la `unidad`. El resultado sirve igual para un curso de cocina, de
primeros auxilios o de matemáticas.

Los términos de dominio que quedan en la maquinaria reutilizada (listas viales
para puntuar oraciones, ideas de fuente) solo afectan la *selección* de
oraciones del material fuente y el resumen interno de la fuente —nunca el texto
visible para el estudiante— así que no filtran vocabulario de conducción.
"""
from __future__ import annotations

from content_pipeline.processors.clean_text import shorten_text
from content_pipeline.processors.lesson_generator import (
    SOURCE_NAME,
    LessonContext,
    _clamp,
    _lesson_title,
    _mapping_lookup,
    _matched_segments,
    _quiz_sources,
    _scored_sentences,
    _source_markdown,
    _sources_for_segments,
    build_lesson_context,
)


def _cap(text: str) -> str:
    text = text.strip()
    return text[:1].upper() + text[1:] if text else text


def _lesson_description(tema: str, unidad_nombre: str) -> str:
    return shorten_text(
        f"Estudia {tema} dentro de {unidad_nombre} y aprende a aplicarlo en situaciones concretas.",
        240,
    )


def _intro(context: LessonContext) -> str:
    tema = context.tema
    unidad = context.unidad_nombre
    return (
        f"{_cap(tema)} es un tema clave dentro de {unidad.lower()}. Comprenderlo te da una base sólida "
        "para avanzar en el resto del curso y conectar estas ideas con lo que viene después.\n\n"
        f"Más que memorizar definiciones, el objetivo es entender {tema.lower()} en profundidad y saber "
        "aplicarlo, de modo que puedas usar lo aprendido cuando lo necesites."
    )


_GENERIC_DEVELOPMENT = (
    "Este tema se entiende mejor cuando se estudia paso a paso. Empieza por identificar los conceptos "
    "centrales, comprende qué significan y observa cómo se relacionan entre sí.\n\n"
    "Una vez claros los fundamentos, conviene ver cómo se aplican en situaciones concretas. Relacionar "
    "la teoría con ejemplos reales hace que el conocimiento sea más fácil de recordar y de usar.\n\n"
    "Por último, la práctica constante consolida lo aprendido: repasa, aplica lo estudiado y revisa tus "
    "resultados para afianzar el tema."
)


def _development(context: LessonContext) -> str:
    scored = _scored_sentences(
        context.tema,
        context.source_text,
        context.part_index,
        context.concepts,
        context.part_count,
    )
    if len(scored) < 3:
        return _GENERIC_DEVELOPMENT
    top = [sentence for _score, sentence in scored[:12]]
    target_paragraphs = 4
    per_paragraph = max(1, len(top) // target_paragraphs)
    paragraphs: list[str] = []
    for start in range(0, len(top), per_paragraph):
        chunk = top[start : start + per_paragraph]
        if chunk:
            paragraphs.append(" ".join(chunk))
        if len(paragraphs) == target_paragraphs:
            break
    if len(paragraphs) < 2:
        return _GENERIC_DEVELOPMENT
    lead = (
        f"En esta lección revisamos las ideas esenciales de {context.tema.lower()}. "
        "Los siguientes puntos, tomados del material del curso, resumen lo más importante."
    )
    body = "\n\n".join(paragraphs)
    closer = (
        "En síntesis: identifica los conceptos centrales, entiende cómo se relacionan y practica "
        "aplicarlos hasta que su uso te resulte natural."
    )
    return f"{lead}\n\n{body}\n\n{closer}"


def _application(context: LessonContext) -> str:
    tema = context.tema.lower()
    return (
        "Este conocimiento cobra sentido cuando lo llevas a la práctica. Al enfrentar una situación "
        f"relacionada con {tema}, podrás reconocer qué conceptos aplican y tomar decisiones mejor "
        "fundamentadas.\n\n"
        f"Cuanto más practiques, más natural será aplicar {tema}: lo que al principio requiere pensar "
        "paso a paso terminará saliéndote de forma casi automática."
    )


def _key_points(context: LessonContext) -> list[str]:
    tema = context.tema.lower()
    unidad = context.unidad_nombre.lower()
    return [
        f"{_cap(tema)} se comprende mejor con ejemplos y casos reales, no solo con definiciones.",
        "Identificar los conceptos centrales facilita recordar y aplicar el tema.",
        f"Relacionar este tema con lo visto en {unidad} refuerza la comprensión.",
        "La práctica constante convierte el conocimiento en habilidad.",
        "Revisar los errores frecuentes ayuda a evitarlos.",
    ]


_GENERIC_APPLIED_EXAMPLE = (
    "Supón que te encuentras con un caso donde este tema es determinante. Antes de actuar, recuerda los "
    "conceptos clave, analiza la situación con calma y elige la opción mejor fundamentada.\n\n"
    "Ese enfoque —observar, analizar y decidir— es el que distingue a quien realmente domina el tema de "
    "quien solo lo ha memorizado."
)


def _applied_example(context: LessonContext) -> str:
    scored = _scored_sentences(
        context.tema,
        context.source_text,
        context.part_index,
        context.concepts,
        context.part_count,
    )
    lines = [sentence for _score, sentence in scored]
    if not lines:
        return _GENERIC_APPLIED_EXAMPLE
    setup = lines[0]
    decision = lines[1] if len(lines) > 1 else None
    first = f"Imagina un caso real donde {context.tema.lower()} es relevante. {setup}"
    if decision:
        second = (
            f"Frente a esa situación, lo esperado es apoyarte en lo estudiado: {decision} "
            "Analizar antes de actuar te da mejores resultados."
        )
    else:
        second = (
            "Frente a esa situación, lo esperado es recordar los conceptos clave, analizar con calma y "
            "decidir con fundamento."
        )
    return f"{first}\n\n{second}"


def _frequent_errors(context: LessonContext) -> list[str]:
    tema = context.tema.lower()
    return [
        f"Estudiar {tema} de memoria sin comprender la lógica que hay detrás.",
        "Saltar los conceptos base y pasar directo a lo más avanzado.",
        "No conectar el tema con ejemplos concretos.",
        "Confiar en una sola lectura sin repasar.",
        "No practicar lo aprendido en situaciones reales.",
    ]


def _activity(context: LessonContext) -> str:
    return (
        f"Piensa en una situación real donde {context.tema.lower()} sea relevante. Describe qué "
        "observarías, qué conceptos aplicarías y qué decisión tomarías."
    )


def _summary(context: LessonContext) -> str:
    return (
        f"{_cap(context.tema)} es una pieza importante dentro de {context.unidad_nombre.lower()}. "
        "Repasa los conceptos centrales, conéctalos con ejemplos y practica su aplicación para "
        "consolidar lo aprendido y avanzar con seguridad."
    )


def _transcription(context: LessonContext) -> str:
    return (
        f"{_cap(context.tema)} es un tema central de {context.unidad_nombre.lower()}. En esta lección "
        "revisamos sus ideas principales, cómo se relacionan entre sí y de qué manera puedes aplicarlas. "
        "La meta es que, al terminar, puedas explicarlo con tus propias palabras y usarlo en la práctica."
    )


def render_generic_lesson(context: LessonContext, source_name: str = SOURCE_NAME) -> str:
    key_points = "\n".join(f"- {point}" for point in _key_points(context))
    frequent_errors = "\n".join(f"- {error}" for error in _frequent_errors(context))
    return f"""# {context.title}

## Objetivo
Al finalizar esta lección podrás explicar los conceptos centrales de {context.tema.lower()}, reconocer su importancia dentro de {context.unidad_nombre.lower()} y aplicarlos en situaciones concretas.

## Introducción
{_intro(context)}

## Desarrollo
{_development(context)}

## Aplicación práctica
{_application(context)}

## Puntos clave
{key_points}

## Ejemplo aplicado
{_applied_example(context)}

## Errores frecuentes
{frequent_errors}

## Actividad breve
{_activity(context)}

## Resumen
{_summary(context)}

## Fuente
{_source_markdown(context.sources, source_name)}
""".strip()


def _quiz_content(temas: list[str]) -> dict[str, object]:
    questions = []
    for tema in temas[:5]:
        questions.append(
            {
                "question": f"Respecto al tema '{tema}', ¿cuál es la mejor forma de abordarlo?",
                "options": [
                    "Memorizar la definición sin comprenderla",
                    "Comprender los conceptos y aplicarlos según el contexto",
                    "Esperar a que otra persona resuelva la situación",
                    "Evitar el tema si parece complicado",
                ],
                "correct_index": 1,
                "explanation": "Aprender de verdad implica comprender los conceptos y saber aplicarlos según cada situación.",
            }
        )
    return {"questions": questions, "passing_score": 75}


def generate_lessons_generic(
    manifest: dict[str, object],
    segments: list[dict[str, object]],
    mappings: list[dict[str, object]],
    source_name: str = SOURCE_NAME,
) -> list[dict[str, object]]:
    """Equivalente neutral de `generate_lessons` para cursos genéricos.

    Misma estructura de salida (una lección de texto por tema + un quiz de
    cierre por unidad) para ser compatible con `import_generated_course`, pero
    con prosa agnóstica al dominio.
    """
    segment_by_id = {str(segment.get("segment_id")): segment for segment in segments}
    mapping_by_topic = _mapping_lookup(mappings)
    lessons: list[dict[str, object]] = []

    for unidad in manifest.get("unidades", []):
        if not isinstance(unidad, dict):
            continue
        orden = int(unidad.get("orden", 0))
        unidad_nombre = str(unidad.get("nombre", ""))
        categoria = str(unidad.get("categoria", ""))
        temas = [str(tema) for tema in unidad.get("temas", [])]
        if not temas:
            continue

        target_minutes = int(unidad.get("horas_elearning", 0)) * 60
        quiz_minutes = 25
        minutes_per_topic = max(10, (target_minutes - quiz_minutes) / len(temas))
        position = 1
        unit_sources: list[dict[str, object]] = []

        for tema in temas:
            mapping = mapping_by_topic.get((orden, tema))
            matched_segments = _matched_segments(mapping, segment_by_id)
            sources = _sources_for_segments(matched_segments, tema, source_name)
            unit_sources.extend(sources)
            duration = _clamp(round(minutes_per_topic), 15, 60)
            title = _lesson_title(tema, 0, 1)
            context = build_lesson_context(
                title,
                tema,
                unidad_nombre,
                0,
                1,
                matched_segments,
                sources,
            )
            lessons.append(
                {
                    "unidad_orden": orden,
                    "unidad_nombre": unidad_nombre,
                    "categoria": categoria,
                    "tema_regulatorio": tema,
                    "nombre": title,
                    "posicion": position,
                    "tipo": "texto",
                    "descripcion": _lesson_description(tema, unidad_nombre),
                    "duracion_min": duration,
                    "contenido": render_generic_lesson(context, source_name),
                    "transcripcion": _transcription(context),
                    "fuentes": sources,
                }
            )
            position += 1

        lessons.append(
            {
                "unidad_orden": orden,
                "unidad_nombre": unidad_nombre,
                "categoria": categoria,
                "tema_regulatorio": f"Evaluación módulo {orden}",
                "nombre": f"Evaluación del módulo {orden}",
                "posicion": position,
                "tipo": "quiz",
                "descripcion": f"Evaluación de cierre de la unidad {unidad_nombre}.",
                "duracion_min": quiz_minutes,
                "contenido": _quiz_content(temas),
                "transcripcion": "",
                "fuentes": _quiz_sources(unit_sources, unidad_nombre, orden, source_name),
            }
        )

    return lessons
