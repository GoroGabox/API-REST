"""Planificación: OUTLINE → plan de curso editable (unidades + lecciones).

Cada CAPÍTULO se vuelve una UNIDAD; su cuerpo se reparte en LECCIONES según la
densidad real (palabras) y la banda de longitud elegida (`--largo`), cortando en
límites de párrafo (y de sección cuando existen). Cada lección lleva su **texto
fuente exacto + páginas** → procedencia 1:1 por construcción (sin mapeo lexical).

El plan es un JSON revisable/editable: el operador ajusta nombres, divide/fusiona
lecciones o cambia el objetivo de palabras antes de pagar la redacción.
"""
from __future__ import annotations

import math
import re
from typing import Any

from content_pipeline.processors.outline import Chapter, Para, build_outline
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
            out.append(Para(text=" ".join(buf), page=p.page)); buf = []; w = 0
        buf.append(sent); w += sw
    if buf:
        out.append(Para(text=" ".join(buf), page=p.page))
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


def build_plan(
    source: Any,
    *,
    nombre: str,
    codigo: str,
    largo: str = "media",
    is_profesional: bool = False,
) -> dict[str, Any]:
    """Construye el plan editable desde el libro. ``source`` = carpeta/archivo/lista."""
    band = BANDAS.get(largo, BANDAS["media"])
    chapters = build_outline(source)
    if not chapters:
        raise ValueError("El outline no produjo capítulos.")

    unidades: list[dict[str, Any]] = []
    for i, ch in enumerate(chapters, start=1):
        groups = _chapter_lesson_groups(ch, band)
        lecciones: list[dict[str, Any]] = []
        for j, (seed, paras) in enumerate(groups, start=1):
            texto = "\n\n".join(p.text for p in paras)
            pgs = [p.page for p in paras if p.page]
            lecciones.append({
                "nombre": _lesson_name(seed, ch.titulo, j, len(groups)),
                "seccion": seed or None,
                "paginas": [min(pgs), max(pgs)] if pgs else [0, 0],
                "palabras_objetivo": band[2],       # tope de palabras de la lección
                "palabras_fuente": len(texto.split()),
                "texto": texto,                      # fuente EXACTA (procedencia por construcción)
            })
        unidades.append({
            "orden": i,
            "nombre": ch.titulo,
            "categoria": resolve_categoria(ch.titulo),
            "paginas": [ch.page_start, ch.page_end],
            "palabras": ch.words(),
            "lecciones": lecciones,
        })

    total_lecc = sum(len(u["lecciones"]) for u in unidades)
    return {
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
        },
    }
