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
from content_pipeline.processors.figure_markers import strip_markers
from content_pipeline.processors.lesson_generator import (
    _combined_text,
    _mapping_lookup,
    _matched_segments,
)

# Anclaje mínimo aceptable (fracción de keywords del contenido presentes en la
# fuente). Bajo esto se marca la lección para revisión. Conservador: el contenido
# pedagógico agrega marco propio, así que solo cae lo claramente poco anclado.
ANCHOR_MIN = 0.28

# ---------------------------------------------------------------------------
# Guard de cifras
# ---------------------------------------------------------------------------
# Una cifra se compara como (valor canónico, clase de unidad). Antes solo se
# reconocían %, km/h, plazos, UTM/UF y pesos, y los decimales se comparaban sin
# la coma ("0,3" contra cualquier "3"): quedaban fuera justo los datos más
# críticos del curso (alcoholemia en g/l, distancias en metros, tiempos de
# reacción en segundos) y las cifras escritas con palabras ("tres segundos").

# (clase, patrón) — el orden importa: las unidades compuestas van antes que sus
# prefijos (km/h antes que km; mg/l antes que mg; g/l antes que g).
_UNIT_PATTERNS: list[tuple[str, str]] = [
    ("%", r"%|por\s*ciento"),
    ("km/h", r"km\s*/\s*h|km\s+por\s+hora|kil[oó]metros?\s+por\s+hora|kph"),
    ("g/l", r"g(?:r|ramos?)?\.?\s*/\s*(?:l|lt|litro)\b|gramos?\s+(?:de\s+alcohol\s+)?por\s+litro"),
    ("mg/l", r"mg\s*/\s*(?:l|dl|100\s*ml)\b|miligramos?\s+por\s+(?:litro|decilitro)"),
    ("mg", r"mg|miligramos?"),
    ("km", r"km|kil[oó]metros?"),
    ("cm", r"cm|cent[ií]metros?"),
    ("mm", r"mm|mil[ií]metros?"),
    ("m", r"mts?\.?|metros?|m"),
    ("s", r"seg(?:undos?)?\.?|s"),
    ("min", r"minutos?|min\.?"),
    ("h", r"horas?|hrs?\.?"),
    ("día", r"d[ií]as?"),
    ("mes", r"meses|mes"),
    ("año", r"a[nñ]os?"),
    ("kg", r"kg|kilos?|kilogramos?"),
    ("t", r"toneladas?|ton\.?"),
    ("l", r"litros?|lts?\.?"),
    ("cc", r"cc|cm3|cm³"),
    ("°", r"°\s*c|grados?(?:\s+celsius)?|°"),
    ("presión", r"psi|bar|libras?"),
    ("utm", r"utm"),
    ("uf", r"uf"),
    ("$", r"pesos"),
]
_UNIT_RES = [(cls, re.compile(r"\s*(?:" + pat + r")(?![a-záéíóúñü])", re.IGNORECASE))
             for cls, pat in _UNIT_PATTERNS]

# Números escritos con palabras (los más frecuentes en prosa didáctica). Se
# excluyen "un/uno/una" y "medio": demasiado comunes para ser señal fiable.
_NUM_WORDS = {
    "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8,
    "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14,
    "quince": 15, "dieciséis": 16, "dieciseis": 16, "diecisiete": 17, "dieciocho": 18,
    "diecinueve": 19, "veinte": 20, "treinta": 30, "cuarenta": 40, "cincuenta": 50,
    "sesenta": 60, "setenta": 70, "ochenta": 80, "noventa": 90, "cien": 100,
    "ciento": 100, "doscientos": 200, "trescientos": 300, "quinientos": 500, "mil": 1000,
}
_NUM_TOKEN_RE = re.compile(
    r"(?<![\w.,])(\d+(?:[.,]\d+)*)|\b(" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(r"\$\s?(\d[\d.,]*)")
_ANY_NUM_RE = re.compile(r"\d[\d.,]*")


def _num_core(token: str) -> str:
    """Valor canónico de un número: "82.000"→"82000", "0,3"→"0.3", "tres"→"3".

    Punto/coma seguidos de grupos de 3 dígitos = separador de miles; si no, es
    separador decimal (así "0,3 g/l" ya no coincide con cualquier "3").
    """
    t = str(token or "").strip().lower().rstrip(".,")
    if t in _NUM_WORDS:
        return str(_NUM_WORDS[t])
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", t) or re.fullmatch(r"\d{1,3}(?:,\d{3})+", t):
        t = re.sub(r"[.,]", "", t)
    else:
        t = t.replace(",", ".")
        if t.count(".") > 1:                       # "1.2.3": no es número limpio
            t = t.replace(".", "")
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    t = t.lstrip("0") or "0"
    return "0" + t if t.startswith(".") else t


def _figures(text: str) -> list[tuple[str, str, str]]:
    """Cifras cualificadas del texto: [(valor canónico, clase de unidad, etiqueta)]."""
    out: list[tuple[str, str, str]] = []
    text = str(text or "")
    for m in _NUM_TOKEN_RE.finditer(text):
        token = m.group(1) or m.group(2)
        rest = text[m.end(): m.end() + 40]
        for cls, rx in _UNIT_RES:
            um = rx.match(rest)
            if um:
                out.append((_num_core(token), cls, f"{token}{um.group(0)}".strip()))
                break
    for m in _MONEY_RE.finditer(text):
        out.append((_num_core(m.group(1)), "$", f"${m.group(1)}"))
    return out


def _source_number_cores(source: str) -> set[str]:
    """Todos los números de la fuente (dígitos y palabras), canonizados."""
    cores = {_num_core(m) for m in _ANY_NUM_RE.findall(source or "")}
    cores |= {str(v) for w, v in _NUM_WORDS.items()
              if re.search(r"\b" + w + r"\b", source or "", re.IGNORECASE)}
    return cores


def _source_figure_units(source: str) -> dict[str, set[str]]:
    """Por valor, las clases de unidad con que aparece en la fuente."""
    units: dict[str, set[str]] = {}
    for core, cls, _label in _figures(source):
        units.setdefault(core, set()).add(cls)
    return units


def _flag_figures(content: str, src_cores: set[str],
                  src_units: dict[str, set[str]] | None = None) -> list[str]:
    """Cifras cualificadas del contenido que la fuente no respalda.

    Se marca si el valor no aparece en la fuente, o si aparece SOLO con otra
    unidad (p. ej. la lección dice "50 metros" y la fuente solo trae "50 km/h").
    """
    flagged: list[str] = []
    seen: set[str] = set()
    for core, cls, label in _figures(content):
        if core == "0" or label.lower() in seen:
            continue                               # el "0" es demasiado común
        if core not in src_cores:
            flagged.append(label)
        elif src_units and src_units.get(core) and cls not in src_units[core]:
            otras = ", ".join(sorted(src_units[core]))
            flagged.append(f"{label} (en la fuente: {otras})")
        else:
            continue
        seen.add(label.lower())
    return flagged


def unsupported_figures(content: str, source: str) -> list[str]:
    """Cifras del contenido cuyo valor (y unidad) NO respalda la fuente.

    Solo considera cifras *cualificadas* (con unidad o símbolo de dinero): son
    las afirmaciones verificables y de riesgo. Devuelve etiquetas legibles,
    deduplicadas y en orden de aparición.
    """
    if not content or not source:
        return []
    return _flag_figures(content, _source_number_cores(source), _source_figure_units(source))


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
    book_text = "\n".join(str(s.get("text", "")) for s in segments)
    book_number_cores = _source_number_cores(book_text)
    book_units = _source_figure_units(book_text)

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
        content = strip_markers(str(lesson.get("contenido") or ""))

        figs = _flag_figures(content, book_number_cores, book_units)  # cifras vs TODO el libro
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


def audit_lessons_from_plan(
    lessons: list[dict[str, Any]], *, anchor_min: float = ANCHOR_MIN,
) -> dict[str, Any]:
    """Audita lecciones redactadas DESDE un plan (traen ``_source_text``).

    Cifras y anclaje contra la fuente PROPIA de cada lección: en este flujo la
    lección debe salir solo de su extracto, así que una cifra que está en otro
    capítulo del libro también es un dato agregado. Misma forma que ``audit_lessons``.
    """
    figuras: list[dict[str, Any]] = []
    anclaje_bajo: list[dict[str, Any]] = []
    auditadas = 0
    for lesson in lessons:
        if lesson.get("tipo") != "texto":
            continue
        src = str(lesson.get("_source_text", ""))
        if not src:
            continue
        auditadas += 1
        content = strip_markers(str(lesson.get("contenido") or ""))
        figs = _flag_figures(content, _source_number_cores(src), _source_figure_units(src))
        if figs:
            figuras.append({"leccion": _label(lesson), "cifras": figs})
        score = anchoring_score(content, src)
        if score < anchor_min:
            anclaje_bajo.append({"leccion": _label(lesson), "anclaje": round(score, 2)})
    return {"auditadas": auditadas, "anchor_min": anchor_min,
            "figuras": figuras, "anclaje_bajo": anclaje_bajo}


def build_lesson_sources_from_plan(
    lessons: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], str]]:
    """Pares (lección, texto fuente) para el juez, desde lecciones con ``_source_text``."""
    return [(l, str(l.get("_source_text", "")))
            for l in lessons if l.get("tipo") == "texto" and str(l.get("_source_text", "")).strip()]


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
        figs = unsupported_figures(strip_markers(str(lesson.get("contenido") or "")), source)
        if figs:
            figuras.append({"leccion": _label(lesson), "cifras": figs})
    return {"auditadas": auditadas, "figuras": figuras}


# ---------------------------------------------------------------------------
# #3 · Juez LLM de fidelidad (opt-in, con costo)
# ---------------------------------------------------------------------------

# Fidelidad por debajo de la cual se marca la lección para revisión.
JUDGE_MIN = 0.7
# Cobertura por debajo de la cual se reporta que la lección omite datos importantes.
COVERAGE_MIN = 0.6
# El juez ve la fuente y la lección completas (antes se cortaban a 8.000 caracteres
# y el final de las lecciones largas quedaba sin auditar).
_JUDGE_SOURCE_CHARS = 30_000

JUDGE_SYSTEM = """\
Eres un auditor de fidelidad de contenido educativo para licencias de conducir en
Chile. Recibes un EXTRACTO FUENTE (material oficial del curso) y una LECCIÓN que
debió redactarse SOLO a partir de él: la lección no puede agregar información que
el extracto no contenga.

1) FIDELIDAD. Revisa TODAS las afirmaciones de la lección, incluidas las de la
introducción, los ejemplos, los consejos, los errores frecuentes y la aplicación
práctica. Es NO respaldada toda afirmación con contenido informativo (dato, regla,
cifra, plazo, sanción, definición, consejo o técnica de conducción, riesgo,
prohibición o recomendación) que el extracto no contenga o contradiga, AUNQUE sea
sentido común o conocimiento general correcto.
NO marques:
- paráfrasis o resúmenes fieles del extracto;
- frases sin contenido informativo (estructura o motivación: "En esta lección
  verás…", "Es importante repasar este tema");
- la adaptación a la licencia objetivo que no agregue un dato nuevo;
- los números de página citados.

2) COBERTURA. Lista en "omissions" los datos o reglas IMPORTANTES del extracto
(cifras, normas, prohibiciones, definiciones, procedimientos, advertencias de
seguridad) que la lección NO incluye. No listes detalles menores, rótulos de
figuras ni referencias a imágenes.

Responde SOLO con JSON válido, sin ```fences:
{
  "faithfulness": 0.0,
  "unsupported_claims": ["afirmación de la lección no respaldada por el extracto", "..."],
  "coverage": 0.0,
  "omissions": ["dato importante del extracto que falta en la lección", "..."]
}
faithfulness = 1.0 si todo está respaldado; baja según cantidad y gravedad (una
cifra errónea o un consejo de seguridad inventado es grave). Con 1-2 matices
menores no respaldados, mantén ≥ 0.85.
coverage = fracción de los datos importantes del extracto presentes en la lección
(1.0 = no falta nada importante).
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
    """Pares (lección, texto fuente) reconstruidos igual que en ``audit_lessons``."""
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


def build_lesson_sources_from_fuentes(lessons: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
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


def _clean_list(value: Any) -> list[str]:
    return [str(c).strip() for c in (value or []) if str(c).strip()]


def _judge_one(content: str, source: str, *, client, model: str) -> dict[str, Any]:
    from content_pipeline.llm.client import parse_json_object

    raw = client.complete(
        system=JUDGE_SYSTEM,
        user=JUDGE_USER.format(
            fuente=shorten_text(source, _JUDGE_SOURCE_CHARS),
            leccion=shorten_text(content, _JUDGE_SOURCE_CHARS),
        ),
        max_tokens=1_500,
        model=model,
        temperature=0.0,
    )
    data = parse_json_object(raw)

    def _score(key: str) -> float | None:
        try:
            v = float(data.get(key))
        except (TypeError, ValueError):
            return None
        return max(0.0, min(1.0, v))

    return {
        "score": _score("faithfulness"),
        "claims": _clean_list(data.get("unsupported_claims")),
        "coverage": _score("coverage"),          # None si el juez no la informó
        "omissions": _clean_list(data.get("omissions")),
    }


def judge_lessons_llm(
    pairs: list[tuple[dict[str, Any], str]],
    *,
    client,
    model: str,
    judge_min: float = JUDGE_MIN,
    coverage_min: float = COVERAGE_MIN,
    limit: int | None = None,
) -> dict[str, Any]:
    """Juzga fidelidad y cobertura de cada lección con el LLM. REPORTE.

    - ``criticas``       — fidelidad ``score < judge_min``: contenido no respaldado
      (bloquea ``import_course``).
    - ``con_reparos``    — fiel en general, con afirmaciones no respaldadas menores.
    - ``cobertura_baja`` — ``coverage < coverage_min``: la lección omite datos
      importantes del extracto (se reporta; no es información falsa, no bloquea).
    Listas ordenadas por puntaje ascendente. Un fallo del LLM en una lección se
    registra en ``errores`` y no tumba la auditoría.
    """
    if limit is not None:
        pairs = pairs[:limit]
    criticas: list[dict[str, Any]] = []
    con_reparos: list[dict[str, Any]] = []
    cobertura_baja: list[dict[str, Any]] = []
    errores: list[str] = []
    scores: list[float] = []
    coberturas: list[float] = []
    for lesson, source in pairs:
        label = _label(lesson)
        try:
            res = _judge_one(strip_markers(str(lesson.get("contenido") or ""), lesson.get("recursos"), modo="pie"),
                           source, client=client, model=model)
        except Exception as exc:  # noqa: BLE001 — un fallo puntual no tumba la auditoría
            errores.append(f"{label}: {exc}")
            continue
        if res["score"] is None:
            errores.append(f"{label}: el juez no devolvió un puntaje válido")
            continue
        scores.append(res["score"])
        item = {"leccion": label, "score": round(res["score"], 2), "claims": res["claims"]}
        if res["coverage"] is not None:
            coberturas.append(res["coverage"])
            item["coverage"] = round(res["coverage"], 2)
            item["omissions"] = res["omissions"]
            if res["coverage"] < coverage_min:
                cobertura_baja.append({"leccion": label, "coverage": item["coverage"],
                                       "omissions": res["omissions"]})
        if res["score"] < judge_min:
            criticas.append(item)          # fidelidad baja: prioridad
        elif res["claims"]:
            con_reparos.append(item)       # fiel en general, pero con reparos menores
    criticas.sort(key=lambda x: x["score"])
    con_reparos.sort(key=lambda x: x["score"])
    cobertura_baja.sort(key=lambda x: x["coverage"])
    return {
        "evaluadas": len(scores),
        "promedio": round(sum(scores) / len(scores), 3) if scores else None,
        "judge_min": judge_min,
        "criticas": criticas,
        "con_reparos": con_reparos,
        "promedio_cobertura": round(sum(coberturas) / len(coberturas), 3) if coberturas else None,
        "coverage_min": coverage_min,
        "cobertura_baja": cobertura_baja,
        "errores": errores,
    }


# ---------------------------------------------------------------------------
# Juez de quizzes: ¿la clave es correcta según la evidencia citada?
# ---------------------------------------------------------------------------
# La evidencia de cada pregunta ya se verificó literalmente contra la fuente al
# generarla; aquí se revisa lo que ese chequeo no ve: que la opción marcada como
# correcta se desprenda de la evidencia y que ningún distractor también lo sea.
# Una clave errónea enseña información falsa: los problemas bloquean el import.
QUIZ_JUDGE_SYSTEM = """\
Eres un auditor de evaluaciones de cursos de conducción en Chile. Para cada
pregunta recibes el enunciado, las opciones, cuál está marcada como correcta y la
EVIDENCIA (cita textual del manual oficial).

Para cada pregunta verifica, usando SOLO la evidencia:
- la opción marcada como correcta está respaldada por la evidencia;
- ninguna otra opción es también correcta según la evidencia;
- el enunciado no es ambiguo ni contradice la evidencia.

Primero escribe tu "analisis" (1-2 frases) y DESPUÉS decide "ok". Marca
"ok": false SOLO si encontraste un error concreto (clave equivocada, otra opción
también correcta, enunciado ambiguo o contradictorio); si tu análisis concluye que
la pregunta está bien, "ok" debe ser true y "problema" vacío.

Responde SOLO con JSON válido, sin ```fences:
{"items": [{"n": 1, "analisis": "…", "ok": true, "problema": ""}]}
"""


def _quiz_items_text(questions: list[dict[str, Any]]) -> str:
    bloques = []
    for n, q in enumerate(questions, start=1):
        opts = "\n".join(f"  {chr(65 + i)}. {o}" for i, o in enumerate(q.get("options") or []))
        ci = q.get("correct_index")
        marcada = chr(65 + ci) if isinstance(ci, int) and 0 <= ci < 26 else "?"
        bloques.append(f"{n}. {q.get('question', '')}\n{opts}\n  Correcta: {marcada}\n"
                       f"  Evidencia: \"{q.get('evidencia', '')}\"")
    return "\n\n".join(bloques)


def _flagged_quiz_items(questions: list[dict[str, Any]], *, client, model: str) -> dict[int, str]:
    """Índices (0-based) de preguntas con un problema CONCRETO → motivo."""
    from content_pipeline.llm.client import parse_json_object

    raw = client.complete(system=QUIZ_JUDGE_SYSTEM, user=_quiz_items_text(questions),
                          max_tokens=300 + 160 * len(questions), model=model, temperature=0.0)
    out: dict[int, str] = {}
    for it in parse_json_object(raw).get("items") or []:
        if not isinstance(it, dict) or it.get("ok") is not False:
            continue
        problema = str(it.get("problema") or "").strip()
        if not problema:          # "ok": false sin motivo concreto: no es un hallazgo
            continue
        try:
            k = int(it.get("n")) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= k < len(questions):
            out[k] = problema
    return out


def judge_quiz_llm(quizzes: list[dict[str, Any]], *, client, model: str) -> dict[str, Any]:
    """Audita las claves de los quizzes (solo preguntas con ``evidencia``).

    Una pregunta marcada se RECONFIRMA con una segunda consulta aislada: solo queda
    como problema si ambas coinciden (los problemas bloquean el import, así que se
    evita que un veredicto inconsistente del modelo frene un curso correcto).
    Devuelve ``{evaluadas, sin_evidencia, problemas, descartados, errores}``; cada
    problema es ``{leccion, pregunta, problema}``.
    """
    problemas: list[dict[str, Any]] = []
    descartados: list[dict[str, Any]] = []
    errores: list[str] = []
    evaluadas = sin_evidencia = 0
    for quiz in quizzes:
        cont = quiz.get("contenido")
        questions = [q for q in ((cont or {}).get("questions") or []) if isinstance(q, dict)]
        con_ev = [q for q in questions if str(q.get("evidencia") or "").strip()]
        sin_evidencia += len(questions) - len(con_ev)
        if not con_ev:
            continue
        label = _label(quiz)
        try:
            flagged = _flagged_quiz_items(con_ev, client=client, model=model)
        except Exception as exc:  # noqa: BLE001
            errores.append(f"{label}: {exc}")
            continue
        evaluadas += len(con_ev)
        for k, problema in flagged.items():
            q = con_ev[k]
            item = {"leccion": label, "pregunta": str(q.get("question", "")), "problema": problema}
            try:
                confirmado = bool(_flagged_quiz_items([q], client=client, model=model))
            except Exception:  # noqa: BLE001 — sin segunda opinión, se conserva el hallazgo
                confirmado = True
            (problemas if confirmado else descartados).append(item)
    return {"evaluadas": evaluadas, "sin_evidencia": sin_evidencia,
            "problemas": problemas, "descartados": descartados, "errores": errores}
