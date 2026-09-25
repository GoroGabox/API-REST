"""Auditoría de fidelidad del contenido generado respecto de la fuente.

El generador ancla la redacción en los segmentos del libro (RAG) y el prompt
prohíbe inventar cifras, pero NADA verificaba el resultado. Aquí se auditan las
lecciones ya generadas contra el texto fuente que se usó para redactarlas:

  1. **Guard de cifras** (`unsupported_figures`): extrae los datos duros del
     contenido (porcentajes, velocidades, montos, plazos, edades) y marca los
     que NO aparecen en la fuente — el error más peligroso (una cifra inventada
     en un curso de licencias).
  2. **Anclaje lexical** (`anchoring_score`): fracción del vocabulario del
     contenido presente en la fuente. Un valor bajo señala una lección poco
     respaldada por el libro.

Todo es REPORTE, no bloqueo: se emite como advertencia y el operador revisa.
Determinista (sin costo de IA).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from content_pipeline.processors.clean_text import extract_keywords, shorten_text
from content_pipeline.processors.lesson_generator import (
    _combined_text,
    _mapping_lookup,
    _matched_segments,
)

# Anclaje mínimo aceptable (fracción de keywords del contenido presentes en la
# fuente). Bajo esto se marca la lección para revisión. Conservador: el contenido
# pedagógico agrega marco propio, así que solo cae lo claramente poco anclado.
ANCHOR_MIN = 0.28

# Unidades de datos duros verificables en el dominio (conducción, Chile).
_UNIT = (
    r"%|km\s*/?\s*h|km\s+por\s+hora|kil[oó]metros?\s+por\s+hora|"
    r"a[nñ]os?|meses|mes|d[ií]as?|horas?|hora|minutos?|utm|uf|pesos"
)
_FIG_RE = re.compile(r"(?<![\w.,])(\d[\d.,]*)\s*(" + _UNIT + r")", re.IGNORECASE)
_MONEY_RE = re.compile(r"\$\s?(\d[\d.,]*)")
_ANY_NUM_RE = re.compile(r"\d[\d.,]*")


def _num_core(token: str) -> str:
    """Normaliza un número a solo dígitos (quita separadores de miles/decimales)."""
    return re.sub(r"[.,]", "", token).lstrip("0") or "0"


def _source_number_cores(source: str) -> set[str]:
    return {_num_core(m) for m in _ANY_NUM_RE.findall(source)}


def _flag_figures(content: str, src_cores: set[str]) -> list[str]:
    """Cifras cualificadas del contenido cuyo número no está en ``src_cores``."""
    flagged: list[str] = []
    seen: set[str] = set()

    def _consider(numero: str, etiqueta: str) -> None:
        core = _num_core(numero)
        if core == "0":
            return  # el "0" es demasiado común para ser señal fiable
        if core not in src_cores and etiqueta.lower() not in seen:
            seen.add(etiqueta.lower())
            flagged.append(etiqueta)

    for numero, unidad in _FIG_RE.findall(content):
        _consider(numero, f"{numero} {unidad}".strip())
    for numero in _MONEY_RE.findall(content):
        _consider(numero, f"${numero}")
    return flagged


def unsupported_figures(content: str, source: str) -> list[str]:
    """Cifras del contenido cuyo número NO aparece en la fuente.

    Solo considera cifras *cualificadas* (con unidad o símbolo de dinero): son
    las afirmaciones verificables y de riesgo. Devuelve etiquetas legibles,
    deduplicadas y en orden de aparición.
    """
    if not content or not source:
        return []
    return _flag_figures(content, _source_number_cores(source))


def anchoring_score(content: str, source: str) -> float:
    """Fracción del vocabulario clave del contenido presente en la fuente.

    1.0 = todo el vocabulario del contenido está en la fuente; ~0 = contenido
    desconectado del libro. Devuelve 1.0 si no hay contenido con keywords.
    """
    content_kw = set(extract_keywords(content, max_keywords=120))
    if not content_kw:
        return 1.0
    source_kw = set(extract_keywords(source, max_keywords=400))
    if not source_kw:
        return 0.0
    return len(content_kw & source_kw) / len(content_kw)


def _label(lesson: dict[str, Any]) -> str:
    return f"U{lesson.get('unidad_orden')} · {lesson.get('nombre', '(sin nombre)')}"


def audit_lessons(
    lessons: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    *,
    anchor_min: float = ANCHOR_MIN,
) -> dict[str, Any]:
    """Audita las lecciones de texto contra su fuente. Dos chequeos con alcance
    distinto:

    - **Cifras**: se comparan contra TODO el libro (no solo los segmentos
      anclados). Con la procedencia, la fuente por-lección es estrecha y marcaba
      como "inventada" cualquier cifra real que estuviera en otra sección del
      libro (falsos positivos). Preguntar "¿este número existe en el libro?" caza
      las alucinaciones reales sin ese ruido.
    - **Anclaje lexical**: se mide contra la fuente por-lección (su punto es medir
      cuán conectada está la lección con SU fuente, no con el libro entero).

    Reconstruye la fuente por-lección por su ``(unidad_orden, tema)`` —la misma
    clave que usó el redactor—. Las lecciones sin fuente mapeada se omiten (lo
    cubre la alerta de cobertura). Devuelve listas de hallazgos para reportar.
    """
    seg_by_id = {str(s.get("segment_id")): s for s in segments}
    mapping_by_topic = _mapping_lookup(mappings)
    # Números presentes en TODO el libro (una vez): base del guard de cifras.
    book_number_cores = _source_number_cores(
        "\n".join(str(s.get("text", "")) for s in segments)
    )

    figuras: list[dict[str, Any]] = []
    anclaje_bajo: list[dict[str, Any]] = []
    auditadas = 0

    for lesson in lessons:
        if lesson.get("tipo") != "texto":
            continue
        key = (int(lesson.get("unidad_orden", 0)), str(lesson.get("tema_regulatorio", "")))
        matched = _matched_segments(mapping_by_topic.get(key), seg_by_id)
        source = _combined_text(matched) if matched else ""
        if not source:
            continue  # sin fuente: lo cubre la alerta de cobertura, no la de fidelidad
        auditadas += 1
        content = str(lesson.get("contenido") or "")

        figs = _flag_figures(content, book_number_cores)  # cifras vs TODO el libro
        if figs:
            figuras.append({"leccion": _label(lesson), "cifras": figs})

        score = anchoring_score(content, source)
        if score < anchor_min:
            anclaje_bajo.append({"leccion": _label(lesson), "anclaje": round(score, 2)})

    return {
        "auditadas": auditadas,
        "anchor_min": anchor_min,
        "figuras": figuras,
        "anclaje_bajo": anclaje_bajo,
    }


def audit_generated_json(lessons: list[dict[str, Any]]) -> dict[str, Any]:
    """Triage de cifras de un JSON YA generado, SIN el libro fuente.

    Usa el ``fragmento_resumen`` embebido en cada ``fuente`` como proxy. Ese
    resumen está RECORTADO (~200 chars): sirve para el guard de cifras (una cifra
    ausente del extracto es candidata a revisión), pero NO para el anclaje
    lexical —contra 200 chars todo saldría "bajo anclaje"—, así que aquí el
    anclaje no se calcula. Para el anclaje real, correr la auditoría con el PDF
    del contenido (``audit_lessons``). Hay falsos positivos: confirmar en el libro.
    """
    figuras: list[dict[str, Any]] = []
    auditadas = 0
    for lesson in lessons:
        if lesson.get("tipo") != "texto":
            continue
        source = "\n".join(
            str(f.get("fragmento_resumen") or "") for f in (lesson.get("fuentes") or [])
        ).strip()
        if not source:
            continue
        auditadas += 1
        figs = unsupported_figures(str(lesson.get("contenido") or ""), source)
        if figs:
            figuras.append({"leccion": _label(lesson), "cifras": figs})
    return {"auditadas": auditadas, "figuras": figuras}


# ---------------------------------------------------------------------------
# #3 · Juez LLM de fidelidad (opt-in, con costo)
# ---------------------------------------------------------------------------

# Fidelidad por debajo de la cual se marca la lección para revisión.
JUDGE_MIN = 0.7
_JUDGE_SOURCE_CHARS = 8_000

JUDGE_SYSTEM = """\
Eres un auditor de fidelidad de contenido educativo para licencias de conducir en
Chile. Recibes un EXTRACTO FUENTE (material oficial del curso) y una LECCIÓN
redactada a partir de él.

Evalúa qué tan fielmente la lección se apega a la fuente. Enfócate SOLO en
afirmaciones factuales verificables: cifras, porcentajes, velocidades, plazos,
edades, montos, normativa, sanciones y definiciones. IGNORA el marco pedagógico
genérico (introducciones, motivación, ejemplos ilustrativos hipotéticos, consejos
de sentido común) — eso no es una afirmación factual sobre la fuente.

Una afirmación NO está respaldada si la fuente no la contiene o la contradice.

En unsupported_claims incluye ÚNICAMENTE afirmaciones factuales NO respaldadas o
contradichas por la fuente. NO incluyas:
- afirmaciones que SÍ están respaldadas por la fuente (aunque comentes que lo
  están), ni reformulaciones o paráfrasis fieles, ni conocimiento general obvio;
- el ENMARCADO hacia la licencia objetivo: aplicar un principio general al tipo
  de vehículo del curso (p. ej. "tu camión requiere más distancia") es válido y
  esperado mientras no invente un dato nuevo — NO lo marques;
- discrepancias en los NÚMEROS DE PÁGINA citados (la cita de páginas es
  aparte; no evalúes la numeración).

Responde SOLO con JSON válido, sin ```fences:
{
  "faithfulness": 0.0,
  "unsupported_claims": ["afirmación factual no respaldada por la fuente", "..."]
}
faithfulness = 1.0 si toda afirmación factual está respaldada; baja proporcional
a la cantidad/gravedad de afirmaciones inventadas o contradictorias. Si la lección
está bien pero tiene 1-2 matices no respaldados, mantén un puntaje alto (≥ 0.85).
"""

JUDGE_USER = """\
EXTRACTO FUENTE:
---
{fuente}
---

LECCIÓN A AUDITAR:
---
{leccion}
---
Devuelve solo el JSON.
"""


def build_lesson_sources(
    lessons: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], str]]:
    """Pares (lección de texto, texto fuente completo) por su (unidad, tema)."""
    seg_by_id = {str(s.get("segment_id")): s for s in segments}
    mapping_by_topic = _mapping_lookup(mappings)
    pairs: list[tuple[dict[str, Any], str]] = []
    for lesson in lessons:
        if lesson.get("tipo") != "texto":
            continue
        key = (int(lesson.get("unidad_orden", 0)), str(lesson.get("tema_regulatorio", "")))
        matched = _matched_segments(mapping_by_topic.get(key), seg_by_id)
        source = _combined_text(matched) if matched else ""
        if source:
            pairs.append((lesson, source))
    return pairs


def build_lesson_sources_from_fuentes(
    lessons: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], str]]:
    """Pares (lección, fuente) usando el ``fragmento_resumen`` embebido (proxy)."""
    pairs: list[tuple[dict[str, Any], str]] = []
    for lesson in lessons:
        if lesson.get("tipo") != "texto":
            continue
        source = "\n".join(
            str(f.get("fragmento_resumen") or "") for f in (lesson.get("fuentes") or [])
        ).strip()
        if source:
            pairs.append((lesson, source))
    return pairs


def _judge_one(content: str, source: str, *, client, model: str) -> dict[str, Any]:
    from content_pipeline.llm.client import parse_json_object

    raw = client.complete(
        system=JUDGE_SYSTEM,
        user=JUDGE_USER.format(
            fuente=shorten_text(source, _JUDGE_SOURCE_CHARS),
            leccion=shorten_text(content, _JUDGE_SOURCE_CHARS),
        ),
        max_tokens=900,
        model=model,
        temperature=0.0,
    )
    data = parse_json_object(raw)
    try:
        score = float(data.get("faithfulness"))
    except (TypeError, ValueError):
        score = None
    if score is not None:
        score = max(0.0, min(1.0, score))
    claims = [str(c).strip() for c in (data.get("unsupported_claims") or []) if str(c).strip()]
    return {"score": score, "claims": claims}


def judge_lessons_llm(
    pairs: list[tuple[dict[str, Any], str]],
    *,
    client,
    model: str,
    judge_min: float = JUDGE_MIN,
    limit: int | None = None,
) -> dict[str, Any]:
    """Juzga la fidelidad de cada lección con el LLM. REPORTE, no bloquea.

    Separa dos severidades para que el conteo sea accionable:
      - ``criticas``   — fidelidad ``score < judge_min``: problema real.
      - ``con_reparos`` — ``score >= judge_min`` pero con afirmaciones no
        soportadas: revisión menor (nitpick), no urgente.
    Ambas listas van ordenadas por score ascendente (peor primero). Devuelve
    ``{evaluadas, promedio, judge_min, criticas, con_reparos, errores}``. Un fallo
    del LLM en una lección se registra en ``errores`` y no tumba la auditoría.
    """
    if limit is not None:
        pairs = pairs[:limit]
    criticas: list[dict[str, Any]] = []
    con_reparos: list[dict[str, Any]] = []
    errores: list[str] = []
    scores: list[float] = []
    for lesson, source in pairs:
        label = _label(lesson)
        try:
            res = _judge_one(str(lesson.get("contenido") or ""), source, client=client, model=model)
        except Exception as exc:  # noqa: BLE001 — un fallo puntual no tumba la auditoría
            errores.append(f"{label}: {exc}")
            continue
        if res["score"] is None:
            errores.append(f"{label}: el juez no devolvió un puntaje válido")
            continue
        scores.append(res["score"])
        item = {"leccion": label, "score": round(res["score"], 2), "claims": res["claims"]}
        if res["score"] < judge_min:
            criticas.append(item)          # fidelidad baja: prioridad
        elif res["claims"]:
            con_reparos.append(item)       # fiel en general, pero con reparos menores
    criticas.sort(key=lambda x: x["score"])
    con_reparos.sort(key=lambda x: x["score"])
    promedio = round(sum(scores) / len(scores), 3) if scores else None
    return {
        "evaluadas": len(scores),
        "promedio": promedio,
        "judge_min": judge_min,
        "criticas": criticas,
        "con_reparos": con_reparos,
        "errores": errores,
    }
