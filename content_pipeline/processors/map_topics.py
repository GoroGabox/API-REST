from __future__ import annotations

import re
from collections import Counter
from typing import Sequence

from content_pipeline.processors.clean_text import extract_keywords, normalize_for_matching


def _topic_query(unidad: dict[str, object], tema: str) -> str:
    return " ".join(
        [
            str(unidad.get("nombre", "")),
            str(unidad.get("categoria", "")),
            tema,
            tema,
        ]
    )


def _segment_document(segment: dict[str, object]) -> str:
    keywords = " ".join(str(keyword) for keyword in segment.get("keywords", []))
    return " ".join(
        [
            str(segment.get("title", "")),
            keywords,
            keywords,
            str(segment.get("text", ""))[:14000],
        ]
    )


def _is_low_value_segment(segment: dict[str, object]) -> bool:
    text = _segment_document(segment)
    normalized = normalize_for_matching(text)
    dot_leaders = text.count("....")
    page_start = int(segment.get("page_start", 0) or 0)
    page_end = int(segment.get("page_end", 0) or 0)
    page_number_refs = len(re.findall(r"\b\d{1,3}\b", text))
    chapter_refs = normalized.count("capitulo")
    looks_like_index = "indice" in normalized or dot_leaders >= 5
    looks_like_catalog = (
        "libro del nuevo conductor profesional" in normalized
        and chapter_refs >= 3
        and page_number_refs >= 30
    )
    return (looks_like_index and page_start <= 15) or looks_like_catalog


def _fallback_scores(queries: Sequence[str], docs: Sequence[str]) -> list[list[float]]:
    doc_tokens = [set(extract_keywords(doc, max_keywords=300)) for doc in docs]
    matrix: list[list[float]] = []
    for query in queries:
        query_tokens = set(extract_keywords(query, max_keywords=40))
        row: list[float] = []
        for tokens in doc_tokens:
            if not query_tokens or not tokens:
                row.append(0.0)
                continue
            intersection = len(query_tokens & tokens)
            union = len(query_tokens | tokens)
            row.append(intersection / union if union else 0.0)
        matrix.append(row)
    return matrix


def _tfidf_scores(queries: Sequence[str], docs: Sequence[str]) -> list[list[float]]:
    if not queries or not docs:
        return [[0.0 for _doc in docs] for _query in queries]
    normalized = [normalize_for_matching(item) for item in list(queries) + list(docs)]
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore
        from sklearn.metrics.pairwise import cosine_similarity  # type: ignore
    except ImportError:
        return _fallback_scores(queries, docs)

    try:
        vectorizer = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, token_pattern=r"(?u)\b\w+\b")
        matrix = vectorizer.fit_transform(normalized)
    except ValueError:
        return _fallback_scores(queries, docs)
    query_matrix = matrix[: len(queries)]
    doc_matrix = matrix[len(queries) :]
    return cosine_similarity(query_matrix, doc_matrix).tolist()


def _root(token: str) -> str:
    for suffix in ("ciones", "cion", "mente", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _keyword_overlap(topic: str, segment: dict[str, object]) -> tuple[float, list[str]]:
    topic_keywords = set(extract_keywords(topic, max_keywords=20))
    segment_text = _segment_document(segment)
    segment_keywords = set(extract_keywords(segment_text, max_keywords=300))
    if not topic_keywords:
        return 0.0, []

    segment_roots = {_root(token) for token in segment_keywords}
    matches = []
    for keyword in sorted(topic_keywords):
        if keyword in segment_keywords or _root(keyword) in segment_roots:
            matches.append(keyword)
    return len(matches) / len(topic_keywords), matches


def _match_payload(
    segment: dict[str, object],
    score: float,
    matches: list[str],
    below_min_score: bool = False,
) -> dict[str, object]:
    reason_terms = ", ".join(matches[:6])
    reason = (
        f"Coinciden términos: {reason_terms}"
        if reason_terms
        else "Similitud lexical entre el tema del Anexo 1 y el segmento del libro"
    )
    if below_min_score:
        reason = f"Mejor coincidencia bajo el umbral; revisar manualmente. {reason}"
    payload: dict[str, object] = {
        "segment_id": segment["segment_id"],
        "score": round(score, 4),
        "page_start": segment["page_start"],
        "page_end": segment["page_end"],
        "reason": reason,
    }
    if below_min_score:
        payload["below_min_score"] = True
    return payload


def _provenance_lookup(
    provenance: list[dict[str, object]] | None,
) -> dict[tuple[int, str], list[str]]:
    lookup: dict[tuple[int, str], list[str]] = {}
    for entry in provenance or []:
        ids = [str(s) for s in (entry.get("segment_ids") or [])]
        if ids:
            lookup[(int(entry.get("unidad_orden", 0)), str(entry.get("tema", "")))] = ids
    return lookup


def _provenance_matches(
    seg_ids: list[str],
    segment_by_id: dict[str, dict[str, object]],
    top_k: int,
) -> list[dict[str, object]]:
    """Payloads de matched_segments a partir de los IDs declarados por el LLM.

    Solo usa IDs que existen realmente entre los segmentos (descarta alucinados).
    Score 1.0: es la fuente que el propio generador ancló al tema.
    """
    out: list[dict[str, object]] = []
    for sid in seg_ids[:top_k]:
        segment = segment_by_id.get(sid)
        if segment is None:
            continue
        out.append({
            "segment_id": sid,
            "score": 1.0,
            "page_start": segment.get("page_start", 0),
            "page_end": segment.get("page_end", 0),
            "reason": "Procedencia declarada por el generador al inferir la estructura",
        })
    return out


def map_topics_to_segments(
    manifest: dict[str, object],
    segments: list[dict[str, object]],
    min_score: float = 0.20,
    top_k: int = 5,
    provenance: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Asocia cada tema con sus segmentos fuente.

    Si ``provenance`` trae los IDs de segmentos que el generador ancló a un tema
    (ver ``manifest_from_content``), se usan DIRECTAMENTE para ese tema (evita el
    re-mapeo lexical que mandaba temas a la sección equivocada). Los temas sin
    procedencia válida caen al mapeo lexical/tfidf de siempre.
    """
    candidate_segments = [segment for segment in segments if not _is_low_value_segment(segment)]
    segment_by_id = {str(s.get("segment_id")): s for s in segments}
    prov_lookup = _provenance_lookup(provenance)
    topic_rows: list[tuple[dict[str, object], str]] = []
    for unidad in manifest.get("unidades", []):
        if not isinstance(unidad, dict):
            continue
        for tema in unidad.get("temas", []):
            topic_rows.append((unidad, str(tema)))

    queries = [_topic_query(unidad, tema) for unidad, tema in topic_rows]
    docs = [_segment_document(segment) for segment in candidate_segments]
    tfidf = _tfidf_scores(queries, docs)

    mappings: list[dict[str, object]] = []
    for topic_index, (unidad, tema) in enumerate(topic_rows):
        # Procedencia primero: si el generador declaró segmentos válidos, se usan.
        prov_ids = prov_lookup.get((int(unidad.get("orden", 0)), tema))
        if prov_ids:
            prov_scored = _provenance_matches(prov_ids, segment_by_id, top_k)
            if prov_scored:
                mappings.append({
                    "unidad_orden": unidad.get("orden"),
                    "unidad_nombre": unidad.get("nombre"),
                    "tema": tema,
                    "matched_segments": prov_scored,
                })
                continue  # no re-mapear lexical: la procedencia manda
        scored: list[dict[str, object]] = []
        below_threshold: list[dict[str, object]] = []
        for segment_index, segment in enumerate(candidate_segments):
            overlap_score, matches = _keyword_overlap(tema, segment)
            lexical_score = tfidf[topic_index][segment_index] if tfidf else 0.0
            score = (0.65 * lexical_score) + (0.35 * overlap_score)
            payload = _match_payload(segment, score, matches)
            if score >= min_score:
                scored.append(payload)
            else:
                below_threshold.append(_match_payload(segment, score, matches, below_min_score=True))
        scored.sort(key=lambda item: item["score"], reverse=True)
        below_threshold.sort(key=lambda item: item["score"], reverse=True)
        if scored:
            best_score = float(scored[0]["score"])
            dynamic_floor = max(min_score, best_score * 0.75, best_score - 0.08)
            scored = [item for item in scored if float(item["score"]) >= dynamic_floor]
        if not scored and below_threshold:
            scored = below_threshold[:1]
        mappings.append(
            {
                "unidad_orden": unidad.get("orden"),
                "unidad_nombre": unidad.get("nombre"),
                "tema": tema,
                "matched_segments": scored[:top_k],
            }
        )
    return mappings


def _topic_label(mapping: dict[str, object]) -> str:
    return f"U{mapping.get('unidad_orden')} · {mapping.get('tema')}"


def coverage_alert(mappings: list[dict[str, object]]) -> dict[str, object]:
    """Clasifica los temas por calidad de su anclaje a la fuente.

    Pensado para avisar durante la generación (a diferencia del reporte extenso
    `build_mapping_coverage_report`, que solo usan los comandos A2):

    - ``solid``     — temas con al menos un segmento POR SOBRE el umbral.
    - ``weak``      — temas cuyo ÚNICO match es forzado bajo umbral
                      (``below_min_score``): la lección se anclará a un segmento
                      poco relacionado. Señal de "revisar / posible tema del
                      temario ausente del libro".
    - ``uncovered`` — temas sin ningún segmento (no hay fuente en el libro).

    ``weak`` y ``uncovered`` son las categorías que ameritan alerta.
    """
    solid: list[str] = []
    weak: list[str] = []
    uncovered: list[str] = []
    for mapping in mappings:
        matched = mapping.get("matched_segments") or []
        if not matched:
            uncovered.append(_topic_label(mapping))
        elif all(bool(seg.get("below_min_score")) for seg in matched):
            weak.append(_topic_label(mapping))
        else:
            solid.append(_topic_label(mapping))
    return {
        "total": len(mappings),
        "solid": solid,
        "weak": weak,
        "uncovered": uncovered,
    }


def build_mapping_coverage_report(
    mappings: list[dict[str, object]],
    segments: list[dict[str, object]],
) -> str:
    used_counter: Counter[str] = Counter()
    low_confidence: list[tuple[dict[str, object], dict[str, object]]] = []
    for mapping in mappings:
        for matched in mapping.get("matched_segments", []):
            used_counter[str(matched.get("segment_id"))] += 1
            if matched.get("below_min_score"):
                low_confidence.append((mapping, matched))

    total_topics = len(mappings)
    covered_topics = sum(1 for mapping in mappings if mapping.get("matched_segments"))
    coverage = (covered_topics / total_topics * 100) if total_topics else 0.0
    all_segment_ids = {str(segment.get("segment_id")) for segment in segments}
    unused_segments = sorted(all_segment_ids - set(used_counter))
    duplicates = {segment_id: count for segment_id, count in used_counter.items() if count >= 4}

    lines = [
        "# Reporte de cobertura A2",
        "",
        f"- Temas regulatorios: {total_topics}",
        f"- Temas con segmentos encontrados: {covered_topics}",
        f"- Cobertura: {coverage:.1f}%",
        f"- Segmentos totales: {len(segments)}",
        f"- Segmentos usados: {len(used_counter)}",
        f"- Segmentos no usados: {len(unused_segments)}",
        f"- Coincidencias bajo umbral: {len(low_confidence)}",
        "",
        "## Temas sin cobertura",
    ]

    uncovered = [mapping for mapping in mappings if not mapping.get("matched_segments")]
    if uncovered:
        for mapping in uncovered:
            lines.append(f"- U{mapping.get('unidad_orden')} {mapping.get('unidad_nombre')}: {mapping.get('tema')}")
    else:
        lines.append("- Ninguno")

    lines.extend(["", "## Coincidencias bajo umbral"])
    if low_confidence:
        for mapping, matched in low_confidence:
            lines.append(
                f"- U{mapping.get('unidad_orden')} {mapping.get('tema')}: "
                f"{matched.get('segment_id')} score={matched.get('score')}"
            )
    else:
        lines.append("- Ninguna")

    lines.extend(["", "## Posibles duplicados"])
    if duplicates:
        for segment_id, count in sorted(duplicates.items(), key=lambda item: item[1], reverse=True):
            lines.append(f"- {segment_id}: usado en {count} temas")
    else:
        lines.append("- No se detectaron segmentos usados en 4 o más temas")

    lines.extend(["", "## Segmentos no usados"])
    if unused_segments:
        preview = unused_segments[:80]
        for segment_id in preview:
            lines.append(f"- {segment_id}")
        if len(unused_segments) > len(preview):
            lines.append(f"- ... {len(unused_segments) - len(preview)} segmentos adicionales")
    else:
        lines.append("- Ninguno")

    lines.append("")
    return "\n".join(lines)
