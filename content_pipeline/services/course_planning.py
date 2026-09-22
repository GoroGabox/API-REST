"""Planificación y validación del curso generado desde el LIBRO completo.

Cambio de modelo (2026-09): el curso ya NO se arma desde el temario, sino desde
TODO el contenido del libro. El temario pasa a ser un *checklist*: al final se
valida que sus temas estén representados en el curso generado. Esto evita cursos
recortados a la malla del Anexo y garantiza cubrir el libro entero.

Este módulo agrupa la lógica de:
  - dimensionar el curso al tamaño del libro sin topes que trunquen
    (`resolve_max_lecciones`, `truncation_notes`),
  - validar la presencia de los temas del temario en el curso
    (`extract_temario_topics`, `validate_topics_present`).
"""
from __future__ import annotations

from typing import Any

from content_pipeline.processors.clean_text import extract_keywords
from content_pipeline.processors.manifest_builder import build_manifest_from_temario
from content_pipeline.processors.manifest_from_content import _MAX_SEGMENTS
from content_pipeline.processors.manifest_llm import build_manifest_from_temario_llm
from content_pipeline.processors.map_topics import _tfidf_scores

# Techo duro de lecciones (coincide con el clamp del view/comando). Un curso más
# grande que esto se recorta y se avisa.
HARD_CEILING = 100
# Piso cuando se auto-dimensiona (libros muy chicos igual dan un curso mínimo).
AUTO_FLOOR = 8
# Umbral de presencia: un tema del temario está "presente" si su mejor
# coincidencia con los temas generados supera esto (misma escala que map_topics).
PRESENCE_MIN_SCORE = 0.18


def resolve_max_lecciones(n_segments: int, requested: int | None) -> tuple[int, str]:
    """Decide el objetivo de lecciones para cubrir TODO el libro.

    - Si el operador fija ``requested`` (>0): se respeta como TECHO (acotado al
      máximo duro).
    - Si no lo fija (``None``): se AUTO-dimensiona al volumen del libro (~1 tema
      por segmento de contenido), entre ``AUTO_FLOOR`` y ``HARD_CEILING``.

    Devuelve ``(max_lecciones, origen)`` donde origen ∈ {"operador", "auto"}.
    """
    if requested is not None and int(requested) > 0:
        return min(int(requested), HARD_CEILING), "operador"
    auto = min(HARD_CEILING, max(AUTO_FLOOR, int(n_segments)))
    return auto, "auto"


def truncation_notes(n_segments: int, max_lecciones: int, origen: str) -> list[str]:
    """Advertencias si algún tope impide cubrir el libro completo.

    Detecta los dos recortes hardcodeados: el techo de lecciones y el tope de
    segmentos que ve la inferencia de estructura.
    """
    notes: list[str] = []
    if n_segments > max_lecciones:
        limite = "límite del operador" if origen == "operador" else "máximo del sistema"
        notes.append(
            f"El libro tiene {n_segments} segmentos de contenido pero el curso se "
            f"limitó a {max_lecciones} lecciones ({limite}); parte del contenido "
            f"puede no quedar cubierta."
        )
    if n_segments > _MAX_SEGMENTS:
        notes.append(
            f"La inferencia de estructura solo considera los primeros {_MAX_SEGMENTS} "
            f"segmentos y el libro tiene {n_segments}: la parte final del libro "
            f"podría omitirse de la estructura."
        )
    return notes


def extract_temario_topics(
    pages: list[dict[str, Any]],
    *,
    nombre: str,
    codigo: str,
    is_profesional: bool,
    use_llm: bool,
    client=None,
    model: str | None = None,
) -> list[str]:
    """Parsea el temario SOLO para obtener su lista de temas esperados.

    Reutiliza los parsers existentes (IA con fallback heurístico) pero sin tope
    de lecciones (``max_lecciones`` alto) para no descartar temas del checklist.
    Devuelve la lista plana de temas, deduplicada, preservando orden.
    """
    manifest = None
    if use_llm:
        try:
            manifest = build_manifest_from_temario_llm(
                pages, nombre=nombre, codigo=codigo, is_profesional=is_profesional,
                max_lecciones=500, client=client, model=model,
            )
        except Exception:  # noqa: BLE001 — degradar a heurística
            manifest = None
    if manifest is None:
        manifest = build_manifest_from_temario(
            pages, nombre=nombre, codigo=codigo, is_profesional=is_profesional,
            max_lecciones=500,
        )

    topics: list[str] = []
    seen: set[str] = set()
    for unidad in manifest.get("unidades", []):
        if not isinstance(unidad, dict):
            continue
        for tema in unidad.get("temas", []):
            texto = str(tema).strip()
            key = texto.lower()
            if texto and key not in seen:
                seen.add(key)
                topics.append(texto)
    return topics


def _generated_topics(manifest: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for unidad in manifest.get("unidades", []):
        if isinstance(unidad, dict):
            out.extend(str(t) for t in unidad.get("temas", []) if str(t).strip())
    return out


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def validate_topics_present(
    expected: list[str],
    manifest: dict[str, Any],
    *,
    min_score: float = PRESENCE_MIN_SCORE,
) -> dict[str, Any]:
    """Valida qué temas del temario están representados en el curso generado.

    Matchea cada tema esperado contra los temas GENERADOS (derivados del libro)
    con la misma métrica lexical/tfidf que ``map_topics`` (0.65 tfidf + 0.35
    solapamiento de keywords). Devuelve ``{expected, present, missing}``.
    """
    generated = _generated_topics(manifest)
    if not expected:
        return {"expected": 0, "present": [], "missing": []}
    if not generated:
        return {"expected": len(expected), "present": [], "missing": list(expected)}

    tfidf = _tfidf_scores(expected, generated)
    gen_tokens = [set(extract_keywords(g, max_keywords=40)) for g in generated]
    present: list[str] = []
    missing: list[str] = []
    for i, topic in enumerate(expected):
        topic_tokens = set(extract_keywords(topic, max_keywords=40))
        best = 0.0
        for j in range(len(generated)):
            lexical = tfidf[i][j] if tfidf else 0.0
            score = 0.65 * lexical + 0.35 * _overlap(topic_tokens, gen_tokens[j])
            if score > best:
                best = score
        (present if best >= min_score else missing).append(topic)
    return {"expected": len(expected), "present": present, "missing": missing}
