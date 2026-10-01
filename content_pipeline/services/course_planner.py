"""Planificación: OUTLINE → plan de curso editable (unidades + lecciones).

Cada CAPÍTULO se vuelve una UNIDAD. Sus LECCIONES las decide la IA
(`plan_structure.LLMSegmenter`): agrupa párrafos consecutivos por TEMA y DENSIDAD,
con la granularidad de `--largo`; las palabras de la banda solo actúan como tope de
seguridad. Si el libro no trae capítulos detectables (Word sin formato), la IA
también propone las unidades. Sin IA (o si falla en un capítulo) se corta por
palabras en límites de párrafo, como antes. Cada lección lleva su **texto fuente
exacto + páginas** → procedencia 1:1 por construcción (la IA solo da rangos).

El plan es un JSON revisable/editable: el operador ajusta nombres, divide/fusiona
lecciones o cambia el objetivo de palabras antes de pagar la redacción.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from content_pipeline.processors.outline import Chapter, Para, book_as_single_chapter, build_outline
from content_pipeline.processors.visual_pages import annotate_plan_visuals
from content_pipeline.services.plan_structure import MINIMO_PALABRAS, fix_colon_borders, merge_tiny
from content_pipeline.review.ids import ensure_plan_ids
from content_pipeline.taxonomy import resolve as resolve_categoria

_SENT_RE = re.compile(r"(?<=[.!?…])\s+")


def _split_big_para(p: Para, target: int, high: int) -> list[Para]:
    """Parte un párrafo que excede ``high`` en trozos ~``target`` por ORACIÓN."""
    if len(p.text.split()) <= high:
        return [p]
    out: list[Para] = []
    buf: list[str] = []
    w = 0
    for sent in _SENT_RE.split(p.text):
        sent = sent.strip()
        if not sent:
            continue
        sw = len(sent.split())
        if buf and w + sw > target:
            out.append(Para(text=" ".join(buf), page=p.page, page_end=p.page_end)); buf = []; w = 0
        buf.append(sent); w += sw
    if buf:
        out.append(Para(text=" ".join(buf), page=p.page, page_end=p.page_end))
    return out or [p]

# Bandas de palabras por lección: (mínimo, objetivo, máximo).
BANDAS = {
    "corta": (400, 550, 700),
    "media": (700, 950, 1200),
    "larga": (1200, 1500, 1800),
}


def _words(paras: list[Para]) -> int:
    return sum(len(p.text.split()) for p in paras)


def _split_paras(paras: list[Para], low: int, target: int, high: int) -> list[list[Para]]:
    """Reparte párrafos en lecciones ~``target`` palabras.

    Los párrafos que exceden ``high`` se sub-parten por oración (evita lecciones
    gigantes de un solo bloque). La cola corta solo se fusiona si el resultado NO
    supera ``high`` (si no, queda como lección corta propia).
    """
    expanded: list[Para] = []
    for p in paras:
        expanded.extend(_split_big_para(p, target, high))

    lessons: list[list[Para]] = []
    cur: list[Para] = []
    w = 0
    for p in expanded:
        pw = len(p.text.split())
        if cur and w + pw > high and w >= low:   # se pasaría del máximo y ya hay suficiente
            lessons.append(cur); cur = []; w = 0
        cur.append(p); w += pw
        if w >= target:                          # alcanzó el objetivo → corta
            lessons.append(cur); cur = []; w = 0
    if cur:
        if lessons and w < low and _words(lessons[-1]) + w <= high:
            lessons[-1].extend(cur)              # cola corta que cabe → fusiona
        else:
            lessons.append(cur)                  # si no cabe, lección corta propia
    return lessons or ([paras] if paras else [])


def _chapter_lesson_groups(ch: Chapter, band: tuple[int, int, int]) -> list[tuple[str, list[Para]]]:
    """Grupos (nombre_semilla, párrafos) por capítulo, respetando secciones si hay."""
    low, target, high = band
    groups: list[tuple[str, list[Para]]] = []
    if ch.sections and sum(len(s.paras) for s in ch.sections) >= len(ch.paras) * 0.6:
        # Hay secciones que cubren el capítulo: úsalas como semilla.
        for sec in ch.sections:
            if not sec.paras:
                continue
            for part in _split_paras(sec.paras, low, target, high):
                groups.append((sec.titulo, part))
    else:
        for part in _split_paras(ch.paras, low, target, high):
            groups.append(("", part))
    return groups


def _lesson_name(seed: str, chapter: str, idx: int, total: int) -> str:
    if seed and len(seed) >= 4:
        return seed.strip()[:100]
    base = chapter.strip()[:80]
    return f"{base} — parte {idx}" if total > 1 else base


def _expand_paras(paras: list[Para], band: tuple[int, int, int]) -> list[Para]:
    """Sub-parte por oración los párrafos que exceden el máximo (la IA no corta dentro)."""
    out: list[Para] = []
    for p in paras:
        out.extend(_split_big_para(p, band[1], band[2]))
    return out


def _ia_lesson_groups(ch: Chapter, band: tuple[int, int, int], largo: str,
                      segmentador: Any) -> list[dict[str, Any]] | None:
    """Lecciones del capítulo según la IA (rangos de párrafos). ``None`` si falla."""
    low, target, high = band
    paras = _expand_paras(ch.paras, band)
    grupos = segmentador.lessons(ch.titulo, paras, largo, high)
    if not grupos:
        return None
    # Red de seguridad: una "lección" de pocas decenas de palabras se une a su vecina.
    grupos = merge_tiny(grupos, paras, MINIMO_PALABRAS.get(largo, MINIMO_PALABRAS["media"]) // 2, high)
    grupos = fix_colon_borders(grupos, paras)   # nunca cerrar con un "…:" que introduce una lista
    out: list[dict[str, Any]] = []
    for g in grupos:
        rango = paras[g.desde:g.hasta + 1]
        meta = {"temas": g.temas, "densidad": g.densidad, "motivo_corte": g.motivo}
        if _words(rango) <= high:
            out.append({"nombre": g.titulo, "paras": rango, "corte": "ia", **meta})
            continue
        # Excede el tope de seguridad del LLM: se sub-parte por párrafos solo en este rango.
        partes = _split_paras(rango, low, target, high)
        for k, parte in enumerate(partes, start=1):
            nombre = f"{g.titulo} — parte {k}" if len(partes) > 1 else g.titulo
            out.append({"nombre": nombre, "paras": parte, "corte": "tope", **meta})
    return out


def _word_lesson_groups(ch: Chapter, band: tuple[int, int, int]) -> list[dict[str, Any]]:
    """Corte por palabras (fallback sin IA)."""
    groups = _chapter_lesson_groups(ch, band)
    return [{"nombre": _lesson_name(seed, ch.titulo, j, len(groups)), "seccion": seed or None,
             "paras": paras, "corte": "palabras"}
            for j, (seed, paras) in enumerate(groups, start=1)]


def _ia_units(source: Any, chapters: list[Chapter], segmentador: Any) -> list[Chapter]:
    """Libro único sin capítulos detectables: la IA propone las unidades."""
    whole = chapters[0] if chapters else book_as_single_chapter(source)
    grupos = segmentador.units(whole.paras) if whole.paras else None
    if not grupos or len(grupos) < 2:
        return chapters or ([whole] if whole.paras else [])
    out: list[Chapter] = []
    for g in grupos:
        paras = whole.paras[g.desde:g.hasta + 1]
        pgs = [pg for p in paras for pg in (p.page, p.page_end) if pg]
        out.append(Chapter(titulo=g.titulo, page_start=min(pgs) if pgs else 0,
                           page_end=max(pgs) if pgs else 0, paras=paras))
    return out


def _is_single_file(source: Any) -> bool:
    return not isinstance(source, (list, tuple)) and not Path(source).is_dir()


def build_plan(
    source: Any,
    *,
    nombre: str,
    codigo: str,
    largo: str = "media",
    is_profesional: bool = False,
    segmentador: Any = None,
) -> dict[str, Any]:
    """Construye el plan editable desde el libro. ``source`` = carpeta/archivo/lista.

    Con ``segmentador`` (``plan_structure.LLMSegmenter``) la IA decide los cortes por
    tema y densidad (y las unidades si el libro no trae capítulos); sin él, o si la IA
    falla en un capítulo, se corta por palabras según la banda.
    """
    band = BANDAS.get(largo, BANDAS["media"])
    try:
        chapters = build_outline(source)
    except ValueError:
        if segmentador is None or not _is_single_file(source):
            raise
        chapters = []
    unidades_ia = False
    if segmentador is not None and len(chapters) <= 1 and _is_single_file(source):
        before = len(chapters)
        chapters = _ia_units(source, chapters, segmentador)
        unidades_ia = len(chapters) > max(before, 1)
    if not chapters:
        raise ValueError("El outline no produjo capítulos.")

    unidades: list[dict[str, Any]] = []
    fallback: list[str] = []
    for i, ch in enumerate(chapters, start=1):
        groups = _ia_lesson_groups(ch, band, largo, segmentador) if segmentador is not None else None
        if groups is None:
            if segmentador is not None:
                fallback.append(ch.titulo)
            groups = _word_lesson_groups(ch, band)
        lecciones: list[dict[str, Any]] = []
        for g in groups:
            paras = g["paras"]
            texto = "\n\n".join(p.text for p in paras)
            pgs = [pg for p in paras for pg in (p.page, p.page_end) if pg]
            lec: dict[str, Any] = {
                "nombre": g["nombre"],
                "seccion": g.get("seccion"),
                "paginas": [min(pgs), max(pgs)] if pgs else [0, 0],
                "palabras_objetivo": band[2],       # tope de palabras de la lección
                "palabras_fuente": len(texto.split()),
                "corte": g["corte"],                 # ia | tope | palabras
            }
            for k in ("temas", "densidad", "motivo_corte"):
                if g.get(k):
                    lec[k] = g[k]
            lec["texto"] = texto                     # fuente EXACTA (procedencia por construcción)
            lecciones.append(lec)
        unidades.append({
            "orden": i,
            "nombre": ch.titulo,
            "categoria": resolve_categoria(ch.titulo),
            "paginas": [ch.page_start, ch.page_end],
            "palabras": ch.words(),
            "lecciones": lecciones,
        })

    total_lecc = sum(len(u["lecciones"]) for u in unidades)
    estructura: dict[str, Any] = {"modo": "ia" if segmentador is not None else "palabras"}
    if segmentador is not None:
        estructura.update({
            "modelo": getattr(segmentador, "model", None),
            "unidades_ia": unidades_ia,
            "capitulos_ia": len(unidades) - len(fallback),
            "fallback": fallback,                    # capítulos cortados por palabras (IA falló)
            "errores": dict(getattr(segmentador, "errores", {}) or {}),
        })
    plan = ensure_plan_ids({  # ids estables → las observaciones de auditores apuntan por id
        "curso": {
            "nombre": nombre, "codigo": codigo,
            "is_profesional": bool(is_profesional),
            "largo": largo,
            "descripcion": f"Curso generado a partir del libro de {nombre}.",
        },
        "unidades": unidades,
        "resumen": {
            "unidades": len(unidades),
            "lecciones": total_lecc,
            "palabras_totales": sum(u["palabras"] for u in unidades),
            "banda": {"largo": largo, "min": band[0], "objetivo": band[1], "max": band[2]},
            "estructura": estructura,
        },
    })
    # Lecciones cuya fuente es mayormente gráfica (señales, figuras): revisión humana.
    plan["resumen"]["revision_visual"] = annotate_plan_visuals(plan, source)
    return plan
