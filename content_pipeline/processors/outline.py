"""Construye el OUTLINE (capítulos → secciones) del libro, dirigido por el índice.

Fuente del outline, por prioridad:
  1. Varios archivos (carpeta): cada archivo = capítulo; headings internos = secciones.
  2. Archivo único con encabezados tipográficos: los ``level==1`` son capítulos,
     ``level>=2`` secciones. Se saltan portada/índice previos al 1er capítulo.

Cada capítulo se vuelve una UNIDAD del curso; sus párrafos de cuerpo (con su
página) son el material que el planificador reparte en lecciones. La detección de
capítulos por índice de un PDF crudo sin headings claros queda como mejora aparte.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from content_pipeline.extractors.structured_extractor import (
    Line,
    extract_structured,
    is_index_line,
)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()


@dataclass
class Para:
    text: str
    page: int


@dataclass
class Section:
    titulo: str
    page_start: int
    paras: list[Para] = field(default_factory=list)


@dataclass
class Chapter:
    titulo: str
    page_start: int
    page_end: int
    paras: list[Para] = field(default_factory=list)     # cuerpo (sin headings/ruido)
    sections: list[Section] = field(default_factory=list)

    def words(self) -> int:
        return sum(len(p.text.split()) for p in self.paras)


_FRONT_RE = re.compile(r"\b(indice|índice|presentaci[oó]n|prefacio|introducci[oó]n general)\b",
                       re.IGNORECASE)


def _chapter_title_clean(text: str) -> str:
    # "CAPITULO 1 - LOS SINIESTROS..." → "Los Siniestros..."
    t = re.sub(r"^\s*(cap[ií]tulo|unidad|m[oó]dulo)\s+\d+\s*[-–:.]?\s*", "", text, flags=re.IGNORECASE)
    t = t.strip(" -–:.")
    return (t or text).strip()


def _lines_to_chapter(titulo: str, lines: list[Line]) -> Chapter:
    """Arma un capítulo desde sus líneas (cuerpo + secciones por headings level>=2)."""
    ch = Chapter(titulo=_chapter_title_clean(titulo), page_start=0, page_end=0)
    cur: Section | None = None
    for ln in lines:
        if ln.is_noise or not ln.text.strip() or is_index_line(ln.text):
            continue
        if ch.page_start == 0:
            ch.page_start = ln.page
        ch.page_end = ln.page
        if ln.level >= 2:  # nueva sección
            cur = Section(titulo=ln.text.strip(), page_start=ln.page)
            ch.sections.append(cur)
            continue
        if ln.level == 1:  # el título del capítulo (ya lo tenemos) — no es cuerpo
            continue
        p = Para(text=ln.text.strip(), page=ln.page)
        ch.paras.append(p)
        if cur is not None:
            cur.paras.append(p)
    return ch


# ---------------------------------------------------------------------------
# Índice (TOC de la propia página de índice del libro)
# ---------------------------------------------------------------------------
_TOC_JUNK_RE = re.compile(r"^\s*(cap[ií]tulo|anexos?|[a-zñ]|\d{1,3})\s*$", re.IGNORECASE)
_TRAIL_NUM_RE = re.compile(r"^(.*?)\s+(\d{1,3})\s*$")


def _find_index_page(lines: list[Line]) -> int | None:
    """Primera página con encabezado 'Índice' y varias entradas con nº al final."""
    by_page: dict[int, list[Line]] = {}
    for ln in lines[: len(lines)]:
        by_page.setdefault(ln.page, []).append(ln)
    for page in sorted(by_page):
        if page > 20:
            break
        txts = [l.text for l in by_page[page]]
        if not any(re.match(r"^\s*[íÍ]ndice\s*$", t) for t in txts):
            continue
        if sum(1 for t in txts if _TRAIL_NUM_RE.match(t)) >= 4:
            return page
    return None


def _parse_toc_entries(index_lines: list[Line]) -> list[tuple[str, int]]:
    """(título, página impresa) desde las líneas del índice; une títulos partidos."""
    entries: list[tuple[str, int]] = []
    buf: list[str] = []
    for ln in index_lines:
        t = ln.text.strip()
        if not t or re.match(r"^\s*[íÍ]ndice\s*$", t) or _TOC_JUNK_RE.match(t):
            continue
        m = _TRAIL_NUM_RE.match(t)
        if m:
            title = " ".join(buf + [m.group(1)]).strip()
            title = re.sub(r"\s+", " ", title).strip(" .·—-")
            if len(title) >= 4:
                entries.append((title, int(m.group(2))))
            buf = []
        else:
            buf.append(t)
    return entries


def _title_matches(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if nb.startswith(na[:18]) or na.startswith(nb[:18]):
        return True
    ta, tb = set(na.split()), set(nb.split())
    inter = len(ta & tb)
    return inter >= 2 and inter / max(1, min(len(ta), len(tb))) >= 0.6


def _outline_from_index(path: Path, lines: list[Line]) -> list[Chapter] | None:
    idx_page = _find_index_page(lines)
    if idx_page is None:
        return None
    entries = _parse_toc_entries([l for l in lines if l.page == idx_page])
    if len(entries) < 3:
        return None

    # Encabezados de capítulo detectados por tipografía (fuera del índice/portada).
    h1 = [l for l in lines if l.level == 1 and not l.is_noise and l.page > idx_page
          and not _FRONT_RE.search(l.text) and len(l.text.split()) >= 2]

    # Offset página-impresa → página-PDF: matchear la 1ª entrada con su H1.
    offset = None
    for title, printed in entries[:6]:
        for hl in h1:
            if _title_matches(title, hl.text):
                offset = hl.page - printed
                break
        if offset is not None:
            break
    if offset is None:
        offset = 0  # asumir impresa ≈ PDF

    # Marcar qué entradas son CAPÍTULO (coinciden con un H1) vs sección.
    marked: list[tuple[str, int, bool]] = []  # (titulo, pdf_page, es_capitulo)
    for title, printed in entries:
        pdf_page = printed + offset
        es_cap = any(abs(hl.page - pdf_page) <= 1 and _title_matches(title, hl.text) for hl in h1)
        marked.append((title, pdf_page, es_cap))
    if not any(m[2] for m in marked):
        return None  # sin capítulos identificables → dejar que otro método decida

    # Texto de cuerpo por página (sin ruido, sin headings, sin índice).
    body_by_page: dict[int, list[str]] = {}
    for ln in lines:
        if ln.page <= idx_page or ln.is_noise or ln.level >= 1 or is_index_line(ln.text):
            continue
        body_by_page.setdefault(ln.page, []).append(ln.text.strip())

    # Construir capítulos: cada entrada-capítulo abre uno; secciones cuelgan de él.
    cap_idx = [i for i, m in enumerate(marked) if m[2]]
    chapters: list[Chapter] = []
    for k, ci in enumerate(cap_idx):
        title, page_start, _ = marked[ci]
        next_ci = cap_idx[k + 1] if k + 1 < len(cap_idx) else len(marked)
        page_end = (marked[next_ci][1] - 1) if next_ci < len(marked) else max(body_by_page or [page_start])
        ch = Chapter(titulo=_chapter_title_clean(title), page_start=page_start, page_end=page_end)
        for p in range(page_start, page_end + 1):
            for txt in body_by_page.get(p, []):
                ch.paras.append(Para(text=txt, page=p))
        # secciones del índice dentro del capítulo
        for si in range(ci + 1, next_ci):
            stitle, spage, _ = marked[si]
            ch.sections.append(Section(titulo=stitle, page_start=spage))
        if ch.paras:
            chapters.append(ch)
    return chapters or None


def _chapter_tier(lines: list[Line]) -> tuple[float, float] | None:
    """(tamaño del tier de capítulos, tamaño del cuerpo).

    El tier de capítulos es el tamaño de fuente grande cuyas apariciones (fuera de
    portada) son POCAS y BIEN DISTRIBUIDAS por el libro (los divisores de capítulo),
    no las decenas de encabezados de sección.
    """
    from content_pipeline.extractors.structured_extractor import _body_size
    body = _body_size([l for l in lines if l.size])
    if not body:
        return None
    total = max((l.page for l in lines), default=1)
    pages_by_size: dict[float, set[int]] = {}
    for l in lines:
        if l.is_noise or l.page <= 2 or l.size < body * 1.25:
            continue
        pages_by_size.setdefault(l.size, set()).add(l.page)
    for size in sorted(pages_by_size, reverse=True):
        pgs = pages_by_size[size]
        if 4 <= len(pgs) <= 25 and (max(pgs) - min(pgs)) >= total * 0.35:
            return size, body
    return None


def _outline_by_font_tier(path: Path, lines: list[Line]) -> list[Chapter] | None:
    tier_body = _chapter_tier(lines)
    if tier_body is None:
        return None
    tier, _body = tier_body

    # Título de capítulo por página divisora (une líneas envueltas de la misma página).
    title_by_page: dict[int, list[str]] = {}
    for l in lines:
        if not l.is_noise and l.size == tier and l.page > 2 and not _FRONT_RE.search(l.text):
            title_by_page.setdefault(l.page, []).append(l.text.strip())
    starts: list[tuple[int, str]] = []
    prev_norm = None
    for pg in sorted(title_by_page):
        title = " ".join(title_by_page[pg]).strip()
        nt = _norm(title)
        if len(nt) < 3 or nt == prev_norm:  # descartar repeticiones (running header)
            continue
        starts.append((pg, title))
        prev_norm = nt
    if len(starts) < 3:
        return None

    body_by_page: dict[int, list[str]] = {}
    for l in lines:
        if l.page <= 2 or l.is_noise or l.level >= 1 or l.size >= tier or is_index_line(l.text):
            continue
        body_by_page.setdefault(l.page, []).append(l.text.strip())

    total = max((l.page for l in lines), default=1)
    chapters: list[Chapter] = []
    for k, (pg, title) in enumerate(starts):
        end = (starts[k + 1][0] - 1) if k + 1 < len(starts) else total
        ch = Chapter(titulo=_chapter_title_clean(title), page_start=pg, page_end=end)
        for p in range(pg, end + 1):
            for txt in body_by_page.get(p, []):
                ch.paras.append(Para(text=txt, page=p))
        if ch.paras:
            chapters.append(ch)
    return chapters or None


def _outline_single(path: Path) -> list[Chapter]:
    lines = extract_structured(path)
    # 1º tier de capítulos por tamaño de fuente (robusto ante índices de 2 columnas).
    via_tier = _outline_by_font_tier(path, lines)
    if via_tier:
        return via_tier
    # 2º índice del libro (si el TOC parsea limpio).
    via_index = _outline_from_index(path, lines)
    if via_index:
        return via_index
    # 3º fallback: encabezados de capítulo por tipografía (level 1).
    h1 = [i for i, ln in enumerate(lines)
          if ln.level == 1 and not ln.is_noise and not _FRONT_RE.search(ln.text)
          and len(ln.text.split()) >= 2]
    if not h1:
        raise ValueError(
            "No se detectó índice ni capítulos por tipografía. Usá archivos por "
            "capítulo, o un libro con índice o encabezados de capítulo claros."
        )
    chapters: list[Chapter] = []
    for k, start in enumerate(h1):
        end = h1[k + 1] if k + 1 < len(h1) else len(lines)
        titulo = lines[start].text.strip()
        chapters.append(_lines_to_chapter(titulo, lines[start:end]))
    return [c for c in chapters if c.paras]


def _outline_multi(files: list[Path]) -> list[Chapter]:
    chapters: list[Chapter] = []
    for f in files:
        lines = extract_structured(f)
        titulo = _chapter_title_from_name(f)
        # Si el archivo trae un H1 propio, usarlo como título.
        for ln in lines:
            if ln.level == 1 and not ln.is_noise:
                titulo = ln.text.strip()
                break
        ch = _lines_to_chapter(titulo, lines)
        if ch.paras:
            chapters.append(ch)
    return chapters


def _chapter_title_from_name(path: Path) -> str:
    return _chapter_title_clean(re.sub(r"\.(docx|pdf)$", "", path.name, flags=re.IGNORECASE))


_CHAP_NUM_RE = re.compile(r"cap[ií]tulo\s*(\d+)", re.IGNORECASE)


def build_outline(source: str | Path | list[str | Path]) -> list[Chapter]:
    """Outline del libro. ``source`` = carpeta, archivo único, o lista de archivos.

    - Carpeta o lista → un capítulo por archivo (ordenados por 'Capítulo N' si aplica).
    - Archivo único → capítulos por tipografía (``level==1``).
    """
    if isinstance(source, (list, tuple)):
        files = [Path(p) for p in source]
    else:
        p = Path(source)
        if p.is_dir():
            files = [f for f in p.iterdir()
                     if f.suffix.lower() in (".docx", ".pdf") and _CHAP_NUM_RE.search(f.name)]
        else:
            return _outline_single(p)
    if not files:
        raise ValueError(f"Sin archivos de capítulo (.docx/.pdf con 'Capítulo N') en: {source}")

    def _order(f: Path) -> int:
        m = _CHAP_NUM_RE.search(f.name)
        return int(m.group(1)) if m else 9999
    files.sort(key=_order)
    return _outline_multi(files)
