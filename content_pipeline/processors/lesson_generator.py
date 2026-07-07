from __future__ import annotations

import re
from dataclasses import dataclass
from math import ceil
from typing import Any

from content_pipeline.processors.clean_text import (
    extract_keywords,
    hash_text_fragment,
    shorten_text,
    unique_preserve_order,
)

SOURCE_NAME = "Libro del Nuevo Conductor Clase A2"
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

LOW_VALUE_CONCEPTS = {
    "capitulo",
    "clase",
    "conaset",
    "conductor",
    "conductores",
    "contenido",
    "debe",
    "deben",
    "generalidades",
    "libro",
    "mayor",
    "ninos",
    "niños",
    "tienen",
    "pueden",
    "distintas",
    "caracteristicas",
    "producen",
    "pagina",
    "paginas",
    "profesional",
    "profesionales",
    "referencia",
    "tema",
    "transito",
    "vehiculo",
    "vehiculos",
}

ROAD_CONTEXT_TERMS = {
    "accidente",
    "accidentes",
    "alcohol",
    "auxilios",
    "cinturon",
    "cruce",
    "distancia",
    "emergencia",
    "estacionamiento",
    "fatiga",
    "frenado",
    "incendio",
    "licencia",
    "luces",
    "maniobra",
    "pasajeros",
    "peatones",
    "riesgo",
    "rotonda",
    "señales",
    "siniestros",
    "velocidad",
    "via",
}


@dataclass(frozen=True)
class LessonContext:
    title: str
    tema: str
    unidad_nombre: str
    part_index: int
    part_count: int
    sources: list[dict[str, object]]
    concepts: list[str]
    source_ideas: list[str]
    source_text: str


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
    if part_count <= 1:
        return segments
    chunk_size = max(1, ceil(len(segments) / part_count))
    start = part_index * chunk_size
    end = start + chunk_size
    return segments[start:end] or segments[-1:]


def _combined_text(segments: list[dict[str, object]]) -> str:
    return "\n\n".join(str(segment.get("text", "")) for segment in segments)


def _sentence_candidates(segments: list[dict[str, object]]) -> list[str]:
    candidates: list[str] = []
    for segment in segments:
        text = re.sub(r"\s+", " ", str(segment.get("text", ""))).strip()
        for sentence in SENTENCE_SPLIT_RE.split(text):
            sentence = re.sub(r"\s+", " ", sentence).strip()
            if 65 <= len(sentence) <= 260 and not sentence.endswith(":"):
                candidates.append(sentence)
    return candidates


_HEADER_PATTERNS = [
    re.compile(r"\b\d{1,3}\s+Libro del Nuevo Conductor Profesional\s*-\s*CONASET\b", re.IGNORECASE),
    re.compile(r"\bCap[ií]tulo\s+\d+\b", re.IGNORECASE),
    re.compile(r"\bFuente:\s*[^.]+", re.IGNORECASE),
    re.compile(r"\bElaboraci[oó]n:\s*CONASET[^.]*", re.IGNORECASE),
    re.compile(r"\bCifras\s+promedio\s+a[nñ]os?\s+\d{4}\s*-\s*\d{4}\b", re.IGNORECASE),
    re.compile(
        r"\b\d{1,3}\s+(?:Generalidades de seguridad vial|Legislaci[oó]n de tr[aá]nsito|"
        r"Normativa\s+[a-zá-ú\s]{2,40}|Reglamentaci[oó]n[a-zá-ú\s]{0,40}|"
        r"Primeros Auxilios|El Conductor(?:\s+y su seguridad)?|El Veh[ií]culo)\b",
        re.IGNORECASE,
    ),
]


_INLINE_ALLCAPS_HEADER_RE = re.compile(
    r"\b[A-ZÁÉÍÓÚÑÜ]{3,}(?:\s+[A-ZÁÉÍÓÚÑÜ]{2,}){1,6}\b"
)


def _clean_sentence(sentence: str) -> str:
    sentence = re.sub(r"­\s*", "", sentence)
    sentence = sentence.replace("�", "")
    sentence = re.sub(r"(\w)[‐‑–-]\s+(\w)", r"\1\2", sentence)
    for pattern in _HEADER_PATTERNS:
        sentence = pattern.sub("", sentence)
    sentence = _INLINE_ALLCAPS_HEADER_RE.sub("", sentence)
    sentence = re.sub(r"•\s*", "", sentence)
    sentence = re.sub(r"\s+", " ", sentence).strip(" -•·")
    return sentence


def _concept_words(tema: str, segments: list[dict[str, object]]) -> list[str]:
    text = tema + " " + " ".join(str(segment.get("text", ""))[:3000] for segment in segments)
    candidates = extract_keywords(text, max_keywords=18)
    concepts = [
        keyword
        for keyword in candidates
        if keyword not in LOW_VALUE_CONCEPTS and len(keyword) >= 4
    ]
    topic_keywords = [
        keyword
        for keyword in extract_keywords(tema, max_keywords=8)
        if keyword not in LOW_VALUE_CONCEPTS and len(keyword) >= 4
    ]
    return unique_preserve_order(topic_keywords + concepts)[:7] or ["riesgo", "seguridad", "pasajeros"]


def _source_ideas(
    tema: str,
    segments: list[dict[str, object]],
    max_items: int = 3,
) -> list[str]:
    if not segments:
        return []
    terms = set(extract_keywords(tema, max_keywords=10))
    terms.update(_concept_words(tema, segments))
    scored: list[tuple[int, str]] = []
    for sentence in _sentence_candidates(segments):
        lowered = sentence.lower()
        matches = [term for term in terms if term in lowered]
        road_matches = [term for term in ROAD_CONTEXT_TERMS if term in lowered]
        if not matches and not road_matches:
            continue
        cleaned = _clean_sentence(sentence)
        if len(cleaned) < 45:
            continue
        score = (len(matches) * 12) + (len(road_matches) * 8) + min(len(cleaned), 160)
        scored.append((score, cleaned))
    scored.sort(key=lambda item: item[0], reverse=True)
    return unique_preserve_order([idea for _score, idea in scored])[:max_items]


def _sources_for_segments(segments: list[dict[str, object]], tema: str) -> list[dict[str, object]]:
    if not segments:
        return []
    page_start = min(int(segment.get("page_start", 0)) for segment in segments)
    page_end = max(int(segment.get("page_end", 0)) for segment in segments)
    text = _combined_text(segments)
    ideas = _source_ideas(tema, segments, max_items=2)
    if ideas:
        summary = f"Referencia tecnica usada para redactar el tema {tema}: {' '.join(ideas)}"
    else:
        summary = f"Referencia tecnica usada para redactar el tema {tema}."
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
        f"Estudia el tema de {tema.lower()} y aplica criterios preventivos en {unidad_nombre.lower()} para la conducción profesional Clase A2.",
        240,
    )


def _human_list(values: list[str], fallback: str) -> str:
    values = [value for value in values if value]
    if not values:
        return fallback
    if len(values) == 1:
        return values[0]
    return ", ".join(values[:-1]) + f" y {values[-1]}"


def _part_focus(tema: str, part_index: int, part_count: int) -> str:
    if part_count == 1:
        return f"comprender {tema.lower()} y aplicarlo antes de que el riesgo aumente"
    focuses = [
        f"reconocer situaciones donde aparece {tema.lower()}",
        "identificar factores que aumentan el riesgo en la vía",
        "elegir medidas preventivas durante el servicio",
        "revisar la responsabilidad del conductor con pasajeros y usuarios vulnerables",
    ]
    return focuses[min(part_index, len(focuses) - 1)]


def _intro_paragraphs(context: LessonContext) -> str:
    focus = _part_focus(context.tema, context.part_index, context.part_count)
    return (
        f"El tema de {context.tema.lower()} es importante dentro de {context.unidad_nombre.lower()} porque influye directamente en la forma de conducir, anticipar riesgos y proteger a otras personas en la vía.\n\n"
        f"Para un conductor profesional Clase A2, no basta con conocer la regla en abstracto. El trabajo diario exige {focus}, especialmente cuando se transportan pasajeros en calles con tráfico, paraderos, cruces peatonales y cambios constantes en el entorno."
    )


_GENERIC_DEVELOPMENT = (
    "Este tema debe entenderse como una herramienta para decidir mejor durante la conducción. En la práctica, se relaciona con la forma en que el conductor observa la vía, controla la velocidad, mantiene distancia y responde ante cambios del tránsito.\n\n"
    "La tarea del conductor profesional es observar antes de actuar. Eso significa mantener una velocidad compatible con el entorno, conservar una distancia suficiente, mirar más allá del vehículo de adelante y evitar maniobras bruscas que puedan afectar a pasajeros o peatones.\n\n"
    "Cuando el riesgo aumenta, la respuesta correcta no es improvisar. Primero se reduce la velocidad si corresponde, luego se asegura espacio, se comunica la maniobra con anticipación y se elige la alternativa que cause menos peligro para quienes viajan y para quienes comparten la vía."
)


_PART_FOCUS_TERMS = [
    {"riesgo", "aparece", "situacion", "situaciones", "peligro"},
    {"factor", "factores", "causa", "causas", "provoca", "aumenta", "aumentan"},
    {"medida", "medidas", "preventiv", "prevenir", "evitar", "prevencion"},
    {"responsabilidad", "responsable", "pasajero", "pasajeros", "vulnerable", "vulnerables", "peaton", "peatones"},
]


def _strip_accents(text: str) -> str:
    replacements = str.maketrans("áéíóúüÁÉÍÓÚÜñÑ", "aeiouuAEIOUUnN")
    return text.translate(replacements)


def _scored_sentences(
    tema: str,
    text: str,
    part_index: int,
    concepts: list[str],
    part_count: int = 1,
) -> list[tuple[float, str]]:
    if not text.strip():
        return []
    if part_count <= 1:
        focus_terms = set().union(*_PART_FOCUS_TERMS)
    else:
        focus_terms = _PART_FOCUS_TERMS[min(part_index, len(_PART_FOCUS_TERMS) - 1)]
    tema_terms = {
        _strip_accents(term).lower()
        for term in extract_keywords(tema, max_keywords=8)
        if len(term) >= 4 and term not in LOW_VALUE_CONCEPTS
    }
    concept_terms = {
        _strip_accents(term).lower()
        for term in concepts
        if len(term) >= 4 and term not in LOW_VALUE_CONCEPTS
    }
    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    normalized = re.sub(r"\s+", " ", text).strip()
    for sentence in SENTENCE_SPLIT_RE.split(normalized):
        sentence = sentence.strip(" -")
        if not (55 <= len(sentence) <= 320) or sentence.endswith(":"):
            continue
        if sentence.isupper():
            continue
        cleaned = _clean_sentence(sentence)
        if len(cleaned) < 50:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        lowered = _strip_accents(cleaned).lower()
        tema_matches = sum(1 for term in tema_terms if term in lowered)
        concept_matches = sum(1 for term in concept_terms if term in lowered)
        road_matches = sum(1 for term in ROAD_CONTEXT_TERMS if _strip_accents(term) in lowered)
        focus_matches = sum(1 for term in focus_terms if term in lowered)
        if tema_matches + concept_matches + road_matches + focus_matches == 0:
            continue
        score = (
            tema_matches * 14
            + focus_matches * 10
            + concept_matches * 6
            + road_matches * 5
            + min(len(cleaned), 200) * 0.05
        )
        scored.append((score, cleaned))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored


def _development_paragraphs(context: LessonContext) -> str:
    scored = _scored_sentences(
        context.tema,
        context.source_text,
        context.part_index,
        context.concepts,
        context.part_count,
    )
    if len(scored) < 3:
        return _GENERIC_DEVELOPMENT
    target_sentences = 12 if context.part_count <= 1 else 6
    target_paragraphs = 4 if context.part_count <= 1 else 3
    top = [sentence for _score, sentence in scored[:target_sentences]]
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
        f"En esta lección revisamos lo esencial de {context.tema.lower()}. "
        "Los siguientes puntos, tomados del libro, guían las decisiones del conductor profesional Clase A2."
    )
    body_paragraphs = "\n\n".join(paragraphs)
    closer = (
        "Traducido a la conducción diaria: observar antes de actuar, ajustar velocidad al entorno, "
        "mantener distancia suficiente y comunicar cada maniobra con anticipación para proteger a "
        "pasajeros y a otros usuarios de la vía."
    )
    return f"{lead}\n\n{body_paragraphs}\n\n{closer}"


def _professional_application(context: LessonContext) -> str:
    return (
        "En taxi, transporte escolar, transporte urbano o servicio interurbano, este conocimiento se aplica antes de llegar al punto crítico. Si hay pasajeros subiendo o bajando, peatones cerca de un cruce, vehículos detenidos, lluvia, mala visibilidad o tráfico denso, el conductor debe ajustar su conducción de inmediato.\n\n"
        "La conducción profesional se diferencia de una conducción improvisada porque anticipa. El conductor no espera a que aparezca una emergencia: lee la vía, calcula espacio, protege a sus pasajeros y evita decisiones que sorprendan a otros usuarios."
    )


def _key_points(context: LessonContext) -> list[str]:
    return [
        f"El tema de {context.tema.lower()} debe aplicarse a situaciones reales de tránsito, no memorizarse como una definición aislada.",
        "La velocidad, la distancia y la atención determinan cuánto margen tiene el conductor para reaccionar.",
        "Transportar pasajeros aumenta la responsabilidad: una frenada o maniobra brusca también puede causar daño dentro del vehículo.",
        "El conductor debe reconocer el riesgo con anticipación y actuar antes de que el peligro sea evidente.",
        "La prevención comienza con observar, reducir riesgos y mantener una conducción previsible.",
    ]


_GENERIC_APPLIED_EXAMPLE = (
    "Un conductor Clase A2 circula por una avenida con pasajeros a bordo. Más adelante ve un paradero con personas esperando, un vehículo detenido parcialmente sobre la pista y peatones que podrían cruzar entre autos. En lugar de mantener la velocidad o adelantar de inmediato, reduce gradualmente, aumenta la distancia con el vehículo de adelante y prepara una detención segura.\n\n"
    "Esa decisión protege a los pasajeros, evita una maniobra repentina y le da más tiempo para responder si un peatón cruza o si el vehículo detenido vuelve a incorporarse."
)


_SCENARIO_HINT_TERMS = {
    "conductor", "pasajero", "pasajeros", "peaton", "peatones", "cruce", "paradero",
    "avenida", "calle", "veh", "distancia", "velocidad", "maniobra", "detencion",
    "lluvia", "visibilidad", "trafico", "adelantar", "frenado", "freno",
}


def _applied_example(context: LessonContext) -> str:
    scored = _scored_sentences(
        context.tema,
        context.source_text,
        context.part_index,
        context.concepts,
        context.part_count,
    )
    scenario_lines = [
        cleaned for _score, cleaned in scored
        if sum(1 for term in _SCENARIO_HINT_TERMS if term in _strip_accents(cleaned).lower()) >= 2
    ]
    if not scenario_lines:
        return _GENERIC_APPLIED_EXAMPLE
    setup = scenario_lines[0]
    decision = scenario_lines[1] if len(scenario_lines) > 1 else None
    first_paragraph = (
        f"Un conductor profesional Clase A2 se encuentra con una situación relacionada con "
        f"{context.tema.lower()}. {setup}"
    )
    if decision:
        second_paragraph = (
            f"Frente a ese escenario, la conducta esperada del conductor se apoya en el libro: {decision} "
            "La decisión protege a pasajeros y a otros usuarios, y da más margen para responder si el "
            "riesgo se materializa."
        )
    else:
        second_paragraph = (
            "Frente a ese escenario, la respuesta esperada es reducir la velocidad si corresponde, "
            "aumentar la distancia con el vehículo de adelante y comunicar cualquier maniobra con "
            "anticipación. La decisión protege a pasajeros y da margen para reaccionar."
        )
    return f"{first_paragraph}\n\n{second_paragraph}"


def _frequent_errors(context: LessonContext) -> list[str]:
    return [
        "Confiarse porque se conoce la ruta o porque el tráfico parece habitual.",
        "Mantener la misma velocidad aunque cambien el clima, la visibilidad o la presencia de peatones.",
        "Reducir la distancia de seguimiento para avanzar más rápido en tráfico denso.",
        "Reaccionar tarde por mirar el teléfono, conversar demasiado o distraerse con pasajeros.",
        "Olvidar que una maniobra brusca puede lesionar o asustar a quienes viajan en el vehículo.",
    ]


def _activity(context: LessonContext) -> str:
    return (
        f"Imagina que conduces con pasajeros y aparece una situación relacionada con {context.tema.lower()} cerca de un cruce peatonal. ¿Qué observas primero, cómo ajustas la velocidad y qué maniobra evitarías para no aumentar el riesgo?"
    )


def _summary(context: LessonContext) -> str:
    return (
        f"El tema de {context.tema.lower()} se estudia para actuar mejor en la vía. Un conductor profesional Clase A2 debe anticipar riesgos, adaptar velocidad y distancia, proteger a sus pasajeros y responder con calma antes de que una situación se transforme en emergencia."
    )


def build_lesson_context(
    title: str,
    tema: str,
    unidad_nombre: str,
    part_index: int,
    part_count: int,
    segments: list[dict[str, object]],
    sources: list[dict[str, object]],
) -> LessonContext:
    return LessonContext(
        title=title,
        tema=tema,
        unidad_nombre=unidad_nombre,
        part_index=part_index,
        part_count=part_count,
        sources=sources,
        concepts=_concept_words(tema, segments),
        source_ideas=_source_ideas(tema, segments),
        source_text=_combined_text(segments),
    )


def render_student_lesson(context: LessonContext) -> str:
    key_points = "\n".join(f"- {point}" for point in _key_points(context))
    frequent_errors = "\n".join(f"- {error}" for error in _frequent_errors(context))
    return f"""# {context.title}

## Objetivo
Al finalizar esta lección, podrás reconocer el riesgo asociado a {context.tema.lower()}, explicar cómo influye en la seguridad vial y aplicar medidas preventivas durante la conducción profesional Clase A2.

## Introducción
{_intro_paragraphs(context)}

## Desarrollo
{_development_paragraphs(context)}

## Aplicación en la conducción profesional
{_professional_application(context)}

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
{_source_markdown(context.sources)}
""".strip()


def _transcription(context: LessonContext) -> str:
    return (
        f"El tema de {context.tema.lower()} no es solo un concepto del curso. Para un conductor profesional Clase A2, es una guía para anticipar riesgos, cuidar a los pasajeros y actuar con margen antes de que aparezca una emergencia. "
        "En esta clase veremos cómo observar la vía, ajustar velocidad y distancia, y elegir una respuesta segura frente a situaciones reales de tránsito."
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
                    "Anticipar riesgos y proteger a pasajeros y otros usuarios",
                    "Esperar a que otro usuario indique qué hacer",
                    "Ignorar el contexto si ya conoce la ruta",
                ],
                "correct_index": 1,
                "explanation": "La conducción profesional exige anticipar, aplicar la regla y proteger a pasajeros, peatones y otros usuarios.",
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
            sources = _sources_for_segments(matched_segments, tema)
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
                    "contenido": render_student_lesson(context),
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
                "fuentes": _quiz_sources(unit_sources, unidad_nombre, orden),
            }
        )

    return lessons


