"""Páginas del libro para un plan que no las trae (p. ej. plan desde la guía en Word).

La guía resume el libro oficial pero no tiene páginas: todas las lecciones quedan en
``[1, 1]`` y el curso no puede citar el libro. Acá se ALINEA cada lección con las
páginas del PDF, sin IA:

1. Capítulo del PDF → unidad del plan por nombre (``plan_figures._match_chapters``).
2. Dentro de la unidad, afinidad lección × página = palabras de contenido compartidas,
   ponderadas por su rareza dentro del capítulo.
3. Programación dinámica: a cada lección un tramo CONTIGUO de páginas, en el orden del
   libro (dos lecciones vecinas pueden compartir la página de borde), maximizando la
   afinidad total y penalizando páginas poco afines.

Supone que la guía sigue el orden del libro dentro de cada capítulo; donde no lo hace,
la afinidad sale baja (``paginas_afinidad``) y el rango se revisa a mano.
"""
from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any

from content_pipeline.processors.outline import _norm, build_outline
from content_pipeline.services.plan_figures import _match_chapters

_STOP = set(
    "de del la las los el en y a al e o u un una unos unas que se por con para su sus es son no lo le les "
    "como mas si debe debes deben puede pueden cuando este esta estos estas ser hay muy tambien pero sin sobre "
    "entre ya o cada todo toda todos todas otro otra otros otras tu tus te al desde hasta donde".split()
)
# Una página aporta si su afinidad (normalizada por lección) supera esto; bajo eso, resta.
_PENALIZACION = 0.35
# Bajo esta afinidad media el rango queda marcado para confirmar.
AFINIDAD_BAJA = 0.5
_TF_TOPE = 3


def _tokens(texto: str) -> list[str]:
    return [w for w in _norm(texto).split() if len(w) > 2 and w not in _STOP]


def page_texts(pdf: str | Path, desde: int, hasta: int) -> list[str]:
    """Texto de las páginas ``desde..hasta`` (1-based, inclusivas)."""
    import fitz

    with fitz.open(str(pdf)) as doc:
        return [doc[p - 1].get_text("text") for p in range(desde, min(hasta, len(doc)) + 1)]


def _afinidad(lecciones: list[str], paginas: list[str]) -> list[list[float]]:
    """Matriz lección × página en [0, 1] (normalizada por la mejor página de cada lección)."""
    pag_tok = [Counter(_tokens(t)) for t in paginas]
    df = Counter(w for c in pag_tok for w in c)
    idf = {w: math.log(1 + len(paginas) / n) for w, n in df.items()}
    out = []
    for texto in lecciones:
        tf = Counter(_tokens(texto))
        fila = [sum(idf[w] * min(c, _TF_TOPE) for w, c in tf.items() if w in pt) for pt in pag_tok]
        tope = max(fila, default=0.0) or 1.0
        out.append([x / tope for x in fila])
    return out


def align_lessons_to_pages(lecciones: list[str], paginas: list[str]) -> list[tuple[int, int, float]]:
    """``[(ini, fin, afinidad)]`` por lección: índices 0-based e inclusivos sobre ``paginas``."""
    n, m = len(lecciones), len(paginas)
    if not n or not m:
        return []
    if m < n:                                    # menos páginas que lecciones: se comparten
        return [(min(i * m // n, m - 1),) * 2 + (0.0,) for i in range(n)]
    S = _afinidad(lecciones, paginas)
    pref = [[0.0] * (m + 1) for _ in range(n)]
    for i in range(n):
        for j in range(m):
            pref[i][j + 1] = pref[i][j] + S[i][j] - _PENALIZACION
    NEG = float("-inf")
    best = [[NEG] * (m + 1) for _ in range(n + 1)]
    back: dict[tuple[int, int], tuple[int, int]] = {}
    best[0][0] = 0.0
    for i in range(1, n + 1):
        for fin in range(1, m + 1):              # tramo [ini, fin) de la lección i
            for ini in range(fin):
                # la lección anterior terminó en `ini` (contigua) o en `ini + 1` (comparte la página)
                for prev in (ini, ini + 1):
                    if prev > m or best[i - 1][prev] == NEG or (prev == ini + 1 and i == 1):
                        continue
                    v = best[i - 1][prev] + pref[i - 1][fin] - pref[i - 1][ini]
                    if v > best[i][fin]:
                        best[i][fin] = v
                        back[(i, fin)] = (ini, prev)
    fin = max(range(1, m + 1), key=lambda e: best[n][e])
    out: list[tuple[int, int, float]] = []
    for i in range(n, 0, -1):
        ini, prev = back[(i, fin)]
        tramo = S[i - 1][ini:fin]
        out.append((ini, fin - 1, round(sum(tramo) / len(tramo), 2)))
        fin = prev
    return out[::-1]


def align_plan_pages(plan: dict[str, Any], pdf: str | Path, *, source_name: str | None = None) -> dict[str, Any]:
    """Escribe ``paginas`` (del PDF) en cada lección del plan (in place). Devuelve el resumen.

    No toca textos, ids ni figuras. Las lecciones ``anexo`` (que ya traen páginas reales)
    y las unidades sin capítulo en el PDF quedan como están.
    """
    chapters = build_outline(Path(pdf))
    unidades = plan.get("unidades") or []
    cap_unidad = _match_chapters([ch.titulo for ch in chapters], [u.get("nombre", "") for u in unidades])
    unidad_cap = {ui: ci for ci, ui in cap_unidad.items() if ui is not None}

    filas: list[dict[str, Any]] = []
    sin_capitulo: list[str] = []
    for ui, u in enumerate(unidades):
        lecciones = [lec for lec in u.get("lecciones") or [] if not lec.get("anexo")]
        if not lecciones:
            continue
        if ui not in unidad_cap:
            sin_capitulo.append(u.get("nombre", ""))
            continue
        ch = chapters[unidad_cap[ui]]
        textos = page_texts(pdf, ch.page_start, ch.page_end)
        tramos = align_lessons_to_pages([lec.get("texto", "") for lec in lecciones], textos)
        for li, (lec, (ini, fin, afin)) in enumerate(zip(lecciones, tramos), start=1):
            lec["paginas"] = [ch.page_start + ini, ch.page_start + fin]
            lec["paginas_origen"] = "alineacion"
            lec["paginas_afinidad"] = afin
            filas.append({"clave": f"{u.get('orden', ui + 1)}.{li}", "leccion": lec.get("nombre", ""),
                          "paginas": lec["paginas"], "afinidad": afin})
        pgs = [lec["paginas"] for lec in u.get("lecciones") or [] if lec.get("paginas") and lec["paginas"][0]]
        if pgs:
            u["paginas"] = [min(p[0] for p in pgs), max(p[1] or p[0] for p in pgs)]

    nombre = source_name or Path(pdf).name
    plan.setdefault("curso", {})["fuente_nombre"] = nombre
    return {
        "fuente": nombre,
        "lecciones": filas,
        "afinidad_baja": [f for f in filas if f["afinidad"] < AFINIDAD_BAJA],
        "unidades_sin_capitulo": sin_capitulo,
    }
