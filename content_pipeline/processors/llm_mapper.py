"""Mapeo tema→segmentos con el LLM (procedencia), en una llamada aparte.

Embeber la procedencia dentro del JSON grande de estructura (76 temas como
objetos con arrays de IDs) rompía el JSON constantemente. Aquí se resuelve en una
llamada dedicada con JSON PLANO por índice numérico —mucho más robusto—:

    {"1": ["seg_0012", "seg_0013"], "2": ["seg_0020"], ...}

Devuelve la misma forma de ``provenance`` que consume
``map_topics.map_topics_to_segments(provenance=...)``. Si la llamada falla, el
llamador cae al mapeo lexical de siempre.
"""
from __future__ import annotations

from typing import Any

from content_pipeline.llm.client import LLMClient, default_model, parse_json_object
from content_pipeline.processors.manifest_from_content import _segments_digest

_MAX_IDS_POR_TEMA = 4

MAP_SYSTEM = """\
Eres un documentalista experto. Recibes (1) una lista numerada de TEMAS de un
curso y (2) una lista de SEGMENTOS de un libro fuente, cada uno con un ID
[segxxxx], su página y palabras clave.

Tu tarea: para CADA tema, indicar de qué segmentos del libro proviene su contenido
(su fuente real). Elige 1 a 4 IDs por tema, los más pertinentes. Copia los IDs
EXACTOS de la lista; NO inventes IDs ni uses IDs ausentes. Si un tema no tiene
fuente clara en los segmentos, devuelve una lista vacía para ese número.

Responde SOLO con un objeto JSON plano que mapea el número del tema (string) a la
lista de IDs, sin ```fences ni texto adicional. Ejemplo:
{"1": ["seg_0012", "seg_0013"], "2": ["seg_0020"], "3": []}
"""

MAP_USER = """\
TEMAS:
{temas}

SEGMENTOS DEL LIBRO:
---
{digest}
---
Devuelve solo el JSON {{numero: [ids]}}.
"""


def _topic_rows(manifest: dict[str, Any]) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for unidad in manifest.get("unidades", []):
        if not isinstance(unidad, dict):
            continue
        orden = int(unidad.get("orden", 0))
        for tema in unidad.get("temas", []):
            rows.append((orden, str(tema)))
    return rows


def map_topics_llm(
    manifest: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = 4000,
) -> list[dict[str, Any]]:
    """Devuelve provenance [{unidad_orden, tema, segment_ids}] vía LLM.

    Valida que los IDs existan entre los segmentos (descarta alucinados) y acota a
    ``_MAX_IDS_POR_TEMA``. Lanza excepción si el LLM/JSON falla (el llamador
    degrada a mapeo lexical).
    """
    rows = _topic_rows(manifest)
    if not rows:
        return []
    client = client or LLMClient()
    temas_block = "\n".join(f"{i}. [U{orden}] {tema}" for i, (orden, tema) in enumerate(rows, 1))
    raw = client.complete(
        system=MAP_SYSTEM,
        user=MAP_USER.format(temas=temas_block, digest=_segments_digest(segments)),
        max_tokens=max_tokens,
        model=model or default_model(),
        temperature=0.0,
    )
    data = parse_json_object(raw)
    valid_ids = {str(s.get("segment_id")) for s in segments}

    provenance: list[dict[str, Any]] = []
    for i, (orden, tema) in enumerate(rows, 1):
        raw_ids = data.get(str(i)) or data.get(i) or []
        if not isinstance(raw_ids, list):
            continue
        seg_ids: list[str] = []
        for x in raw_ids:
            sid = str(x).strip()
            if sid in valid_ids and sid not in seg_ids:
                seg_ids.append(sid)
            if len(seg_ids) >= _MAX_IDS_POR_TEMA:
                break
        if seg_ids:
            provenance.append({"unidad_orden": orden, "tema": tema, "segment_ids": seg_ids})
    return provenance
