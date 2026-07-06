from __future__ import annotations

import re
from math import ceil
from typing import Any

from content_pipeline.processors.clean_text import (
    extract_keywords,
    hash_text_fragment,
    normalize_for_matching,
    shorten_text,
    unique_preserve_order,
)

SOURCE_NAME = "Libro del Nuevo Conductor Clase A2"
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def _mapping_lookup(mappings: list[dict[str, object]]) -> dict[tuple[int, str], dict[str, object]]:
    lookup: dict[tuple[int, str], dict[str, object]] = {}
    for mapping in mappings:
        lookup[(int(mapping.get("unidad_orden", 0)), str(mapping.get("tema", "")))] = mapping
    return lookup


def _matched_segments(
    mapping: dict[str, object] | None,
    segment_by_id: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    if not mapping:
        return []
    selected = []
    for matched in mapping.get("matched_segments", []):
        segment_id = str(matched.get("segment_id"))
        segment = segment_by_id.get(segment_id)
        if segment:
            selected.append(segment)
    selected.sort(key=lambda item: (int(item.get("page_start", 0)), str(item.get("segment_id", ""))))
    return selected


def _segments_for_part(
    segments: list[dict[str, object]],
    part_index: int,
    part_count: int,
) -> list[dict[str, object]]:
    if not segments:
        return []
    if len(segments) <= part_count:
        return segments
    chunk_size = ceil(len(segments) / part_count)
    start = part_index * chunk_size
    end = start + chunk_size
    return segments[start:end] or segments[:1]


def _sources_for_segments(segments: list[dict[str, object]], tema: str) -> list[dict[str, object]]:
    if not segments:
        return []
    page_start = min(int(segment.get("page_start", 0)) for segment in segments)
    page_end = max(int(segment.get("page_end", 0)) for segment in segments)
    text = "\n\n".join(str(segment.get("text", "")) for segment in segments)
    keywords = extract_keywords(text, max_keywords=8)
    key_text = ", ".join(keywords[:6]) if keywords else tema
    source_ideas = "; ".join(_source_ideas(tema, segments, max_items=2))
    summary = f"Material de referencia asociado a {tema}; conceptos destacados: {key_text}."
    if source_ideas:
        summary = f"{summary} Señales usadas para la lección: {source_ideas}"
    return [
        {
            "fuente_nombre": SOURCE_NAME,
            "pagina_inicio": page_start,
            "pagina_fin": page_end,
            "tema_regulatorio": tema,
            "fragmento_resumen": shorten_text(summary, 500),
            "hash_fragmento": hash_text_fragment(text),
        }
    ]


def _source_markdown(sources: list[dict[str, object]]) -> str:
    if not sources:
        return f"{SOURCE_NAME}, páginas por validar."
    first_page = min(int(source.get("pagina_inicio", 0)) for source in sources)
    last_page = max(int(source.get("pagina_fin", 0)) for source in sources)
    return f"{SOURCE_NAME}, páginas {first_page}-{last_page}."


def _lesson_title(tema: str, part_index: int, part_count: int) -> str:
    if part_count == 1:
        return shorten_text(tema, 95)
    return shorten_text(f"{tema}: parte {part_index + 1}", 95)


def _lesson_description(tema: str, unidad_nombre: str) -> str:
    return shorten_text(
        f"Explica {tema.lower()} en el contexto de {unidad_nombre.lower()} para conductores profesionales Clase A2.",
        240,
    )


def _concept_words(tema: str, segments: list[dict[str, object]]) -> list[str]:
    text = tema + " " + " ".join(str(segment.get("text", ""))[:3000] for segment in segments)
    keywords = extract_keywords(text, max_keywords=8)
    return keywords or extract_keywords(tema, max_keywords=5) or ["seguridad", "norma", "conducción"]


def _segment_titles(segments: list[dict[str, object]]) -> list[str]:
    titles = [
        shorten_text(str(segment.get("title", "")).strip(), 70)
        for segment in segments
        if str(segment.get("title", "")).strip()
    ]
    return unique_preserve_order(titles)[:3]


def _sentence_candidates(segments: list[dict[str, object]]) -> list[str]:
    candidates: list[str] = []
    for segment in segments:
        text = re.sub(r"\s+", " ", str(segment.get("text", ""))).strip()
        for sentence in SENTENCE_SPLIT_RE.split(text):
            sentence = re.sub(r"\s+", " ", sentence).strip()
            if 45 <= len(sentence) <= 260 and not sentence.endswith(":"):
                candidates.append(sentence)
    return candidates


def _source_ideas(
    tema: str,
    segments: list[dict[str, object]],
    max_items: int = 4,
) -> list[str]:
    if not segments:
        return []
    terms = set(extract_keywords(tema, max_keywords=10))
    terms.update(_concept_words(tema, segments)[:8])
    scored: list[tuple[int, str]] = []
    for sentence in _sentence_candidates(segments):
        normalized = normalize_for_matching(sentence)
        matches = [term for term in terms if term in normalized]
        if not matches:
            continue
        score = (len(matches) * 10) + min(len(sentence), 180)
        matched_terms = ", ".join(matches[:3])
        idea = (
            f"Relaciona {matched_terms} con una conducta observable: "
            f"{shorten_text(sentence, 150)}"
        )
        scored.append((score, idea))
    scored.sort(key=lambda item: item[0], reverse=True)
    return unique_preserve_order([idea for _score, idea in scored])[:max_items]


def _learning_points(tema: str, concepts: list[str], source_ideas: list[str]) -> list[str]:
    main = concepts[:5] or extract_keywords(tema, max_keywords=5) or ["seguridad"]
    points = [
        "Ubica el tema en una decisión concreta de conducción: qué observar, cuándo actuar y qué evitar.",
        f"Conecta {', '.join(main[:3])} con el riesgo para pasajeros, peatones y otros usuarios.",
        "Aplica la regla antes de ejecutar la maniobra, no después de detectar el conflicto.",
    ]
    if source_ideas:
        points.append("Contrasta la explicación con las páginas fuente y registra el criterio operativo que se repite.")
    else:
        points.append("Cuando la fuente sea escasa, estudia el tema como criterio mínimo de seguridad profesional.")
    return points


def _professional_example(tema: str, concepts: list[str], source_ideas: list[str]) -> str:
    concept = concepts[0] if concepts else tema.lower()
    source_hint = (
        "La situación debe resolverse aplicando la señal principal de la fuente y verificando el entorno inmediato."
        if source_ideas
        else "La situación debe resolverse con criterio preventivo y cumplimiento normativo."
    )
    return (
        f"Durante un servicio de transporte de pasajeros, aparece una condición vinculada con {concept}. "
        "El conductor reduce la improvisación: observa la vía, anticipa la reacción de terceros, comunica su maniobra "
        f"y elige la opción más segura antes de que el riesgo aumente. {source_hint}"
    )


def _frequent_errors(tema: str, concepts: list[str]) -> list[str]:
    concept = concepts[0] if concepts else tema.lower()
    return [
        f"Tratar {concept} como una definición aislada y no como una decisión en ruta.",
        "Esperar a que el riesgo sea evidente antes de ajustar velocidad, distancia o posición.",
        "Olvidar que en Clase A2 la maniobra afecta también a pasajeros y usuarios vulnerables.",
    ]


def _lesson_markdown(
    title: str,
    tema: str,
    unidad_nombre: str,
    part_index: int,
    part_count: int,
    segments: list[dict[str, object]],
    sources: list[dict[str, object]],
) -> str:
    concepts = _concept_words(tema, segments)
    concept_phrase = ", ".join(concepts[:5])
    titles = _segment_titles(segments)
    title_phrase = ", ".join(titles) if titles else "las páginas trazadas del libro"
    source_ideas = _source_ideas(tema, segments)
    source_block = "\n".join(f"- {idea}" for idea in source_ideas) or (
        "- La fuente disponible se usa como respaldo de páginas y conceptos; se recomienda revisión manual si el mapeo fue de baja confianza."
    )
    key_points = "\n".join(f"- {point}" for point in _learning_points(tema, concepts, source_ideas))
    frequent_errors = "\n".join(f"- {error}" for error in _frequent_errors(tema, concepts))
    focus = (
        "identificar la regla, aplicarla en la vía y anticipar riesgos para pasajeros y terceros"
        if part_count == 1
        else f"profundizar el aspecto {part_index + 1} de {part_count}, conectando la norma con decisiones de conducción profesional"
    )
    return f"""# {title}

## Objetivo
Al finalizar esta lección, el estudiante podrá explicar {tema.lower()} y usarlo para tomar decisiones seguras durante la conducción profesional Clase A2.

## Explicación
Este contenido pertenece a la unidad {unidad_nombre}. La idea central es {focus}. En la práctica, el conductor debe relacionar la norma con el entorno real: condición de la vía, pasajeros, peatones, estado del vehículo y capacidad de reacción.

Los conceptos de referencia para estudiar este tema son: {concept_phrase}. El mapeo tomó como base {title_phrase}. No basta con memorizar una definición; el objetivo es reconocer cuándo aparece la situación, qué conducta exige y qué consecuencia puede tener una decisión tardía o incorrecta.

## Señales desde la fuente
{source_block}

## Puntos clave
{key_points}

## Ejemplo aplicado a Clase A2
{_professional_example(tema, concepts, source_ideas)}

## Errores frecuentes
{frequent_errors}

## Actividad breve
Piensa en una situación real de conducción profesional donde aparezca {tema.lower()}. Anota tres señales que observarías antes de actuar, la decisión más segura para tus pasajeros y la conducta que evitarías.

## Resumen
{tema} debe estudiarse como una herramienta práctica: permite anticipar riesgos, cumplir la normativa y sostener una conducción profesional responsable a partir de evidencias trazables.

## Fuente
{_source_markdown(sources)}
""".strip()


def _transcription(title: str, tema: str) -> str:
    return (
        f"En esta lección revisarás {title}. La meta es comprender {tema.lower()} "
        "y llevarlo a decisiones concretas de conducción profesional Clase A2."
    )


def _quiz_sources(unit_sources: list[dict[str, object]], unidad_nombre: str, orden: int) -> list[dict[str, object]]:
    if not unit_sources:
        return []
    page_start = min(int(source.get("pagina_inicio", 0)) for source in unit_sources)
    page_end = max(int(source.get("pagina_fin", 0)) for source in unit_sources)
    hash_seed = "".join(str(source.get("hash_fragmento", "")) for source in unit_sources)
    return [
        {
            "fuente_nombre": SOURCE_NAME,
            "pagina_inicio": page_start,
            "pagina_fin": page_end,
            "tema_regulatorio": f"Evaluación módulo {orden}",
            "fragmento_resumen": f"Evaluación basada en los temas de la unidad {unidad_nombre}.",
            "hash_fragmento": hash_text_fragment(hash_seed or unidad_nombre),
        }
    ]


def _quiz_content(temas: list[str]) -> dict[str, Any]:
    questions = []
    for tema in temas[:5]:
        questions.append(
            {
                "question": f"¿Qué debe priorizar un conductor Clase A2 al aplicar el tema: {tema}?",
                "options": [
                    "Memorizar la frase sin conectarla con la vía",
                    "Aplicar la norma con anticipación y criterio de seguridad",
                    "Esperar a que otro usuario indique qué hacer",
                    "Ignorar el contexto si ya conoce la ruta",
                ],
                "correct_index": 1,
                "explanation": "La conducción profesional exige anticipar, aplicar la norma y proteger a pasajeros y terceros.",
            }
        )
    return {"questions": questions, "passing_score": 75}


def generate_lessons(
    manifest: dict[str, object],
    segments: list[dict[str, object]],
    mappings: list[dict[str, object]],
) -> list[dict[str, object]]:
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
            part_count = _clamp(ceil(minutes_per_topic / 35), 1, 4)
            duration = _clamp(round(minutes_per_topic / part_count), 10, 35)

            for part_index in range(part_count):
                part_segments = _segments_for_part(matched_segments, part_index, part_count)
                sources = _sources_for_segments(part_segments, tema)
                unit_sources.extend(sources)
                title = _lesson_title(tema, part_index, part_count)
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
                        "contenido": _lesson_markdown(
                            title,
                            tema,
                            unidad_nombre,
                            part_index,
                            part_count,
                            part_segments,
                            sources,
                        ),
                        "transcripcion": _transcription(title, tema),
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
                "fuentes": _quiz_sources(unit_sources, unidad_nombre, orden),
            }
        )

    return lessons
