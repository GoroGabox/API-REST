"""Extracción ESTRUCTURADA (tipografía / estilos) para planificar el curso.

A diferencia de `pdf_text_extractor` (que usa `get_text("text")` y pierde la
jerarquía), aquí se conserva **tamaño de fuente, negrita, posición y numeración**
para detectar encabezados (capítulos/secciones) y limpiar ruido (headers/pies
repetidos, números de página, líneas de índice). Es la base del flujo
"planificación dirigida por el índice".

Soporta:
  - PDF: spans de `get_text("dict")` (mismo patrón que `cuestionario_parser`).
  - DOCX: estilos de párrafo (`w:pStyle` → Heading N), más fiables que el PDF.

Salida: lista de ``Line`` en orden de lectura, con ``level`` (0 = cuerpo, 1..3 =
encabezado) y ``is_noise`` ya marcado.
"""
from __future__ import annotations

import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_HEADING_NUM_RE = re.compile(r"^\s*(cap[ií]tulo|unidad|m[oó]dulo)\s+\d+", re.IGNORECASE)
_SECTION_NUM_RE = re.compile(r"^\s*\d+(\.\d+){0,2}[.)]?\s+\S")
_DOTS_RE = re.compile(r"\.{4,}")
_PAGENUM_RE = re.compile(r"^\s*(p[aá]g\.?\s*)?\d{1,4}\s*$", re.IGNORECASE)


@dataclass
class Line:
    page: int
    text: str
    size: float          # tamaño de fuente dominante (pt); 0 en DOCX
    bold: bool
    y0: float            # posición vertical (top) — 0 en DOCX
    page_h: float        # alto de página — 0 en DOCX
    style: str = ""      # estilo de párrafo (DOCX)
    level: int = 0       # 0 cuerpo · 1 capítulo · 2 sección · 3 subsección
    is_noise: bool = False


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _pdf_lines(path: Path) -> list[Line]:
    import fitz  # PyMuPDF

    lines: list[Line] = []
    with fitz.open(path) as doc:
        for pno, page in enumerate(doc, start=1):
            ph = float(page.rect.height)
            data = page.get_text("dict")
            for block in data.get("blocks", []):
                if block.get("type") != 0:  # solo texto
                    continue
                for ln in block.get("lines", []):
                    spans = ln.get("spans", [])
                    text = "".join(s.get("text", "") for s in spans).strip()
                    if not text:
                        continue
                    size = max((float(s.get("size", 0)) for s in spans), default=0.0)
                    bold = any(
                        (int(s.get("flags", 0)) & 16) or "bold" in str(s.get("font", "")).lower()
                        for s in spans
                    )
                    y0 = float(ln.get("bbox", [0, 0, 0, 0])[1])
                    lines.append(Line(page=pno, text=re.sub(r"\s+", " ", text),
                                      size=round(size, 1), bold=bold, y0=y0, page_h=ph))
    return lines


def _body_size(lines: list[Line]) -> float:
    """Tamaño de fuente del CUERPO: el más frecuente ponderado por nº de chars."""
    weight: Counter[float] = Counter()
    for ln in lines:
        weight[ln.size] += len(ln.text)
    return weight.most_common(1)[0][0] if weight else 0.0


def _mark_pdf_noise(lines: list[Line]) -> None:
    """Marca headers/pies repetidos por posición + números de página sueltos."""
    n_pages = max((ln.page for ln in lines), default=0)
    if n_pages < 4:
        return
    top: Counter[str] = Counter()
    bot: Counter[str] = Counter()
    seen_top: dict[str, set[int]] = {}
    seen_bot: dict[str, set[int]] = {}
    for ln in lines:
        if not ln.page_h:
            continue
        key = _norm(ln.text)
        if not key or len(key) > 90:
            continue
        if ln.y0 < ln.page_h * 0.10:
            seen_top.setdefault(key, set()).add(ln.page)
        elif ln.y0 > ln.page_h * 0.90:
            seen_bot.setdefault(key, set()).add(ln.page)
    thr = max(3, n_pages // 3)
    rep_top = {k for k, ps in seen_top.items() if len(ps) >= thr}
    rep_bot = {k for k, ps in seen_bot.items() if len(ps) >= thr}
    for ln in lines:
        if _PAGENUM_RE.match(ln.text):
            ln.is_noise = True
            continue
        key = _norm(ln.text)
        if ln.page_h and ln.y0 < ln.page_h * 0.10 and key in rep_top:
            ln.is_noise = True
        elif ln.page_h and ln.y0 > ln.page_h * 0.90 and key in rep_bot:
            ln.is_noise = True


def _classify_pdf_headings(lines: list[Line]) -> None:
    """Asigna ``level`` por tamaño de fuente relativo + numeración."""
    body = _body_size(lines)
    if not body:
        return
    # Escalones de tamaño por sobre el cuerpo → niveles de encabezado.
    for ln in lines:
        if ln.is_noise:
            continue
        big = ln.size / body if body else 1.0
        looks_num = bool(_HEADING_NUM_RE.match(ln.text)) or bool(_SECTION_NUM_RE.match(ln.text))
        if big >= 1.45 or (_HEADING_NUM_RE.match(ln.text) and (ln.bold or big >= 1.15)):
            ln.level = 1
        elif big >= 1.22 or (ln.bold and big >= 1.12) or (looks_num and (ln.bold or big >= 1.1)):
            ln.level = 2
        elif ln.bold and len(ln.text) <= 90 and big >= 1.05:
            ln.level = 3


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------
_STYLE_LEVEL = {"title": 1, "heading1": 1, "heading2": 2, "heading3": 3, "heading4": 3}


def _docx_lines(path: Path, page: int = 1) -> list[Line]:
    root = ET.fromstring(zipfile.ZipFile(path).read("word/document.xml"))
    lines: list[Line] = []
    for p in root.iter(_W + "p"):
        text = re.sub(r"\s+", " ", "".join(t.text or "" for t in p.iter(_W + "t"))).strip()
        if not text:
            continue
        style = ""
        ppr = p.find(_W + "pPr")
        if ppr is not None:
            st = ppr.find(_W + "pStyle")
            if st is not None:
                style = (st.get(_W + "val") or "").strip()
        level = _STYLE_LEVEL.get(_norm(style).replace(" ", ""), 0)
        # Heurística extra: bold/negrita a nivel de run + tamaño (por si no hay estilo).
        bold = False
        rpr = p.find(f"{_W}pPr/{_W}rPr")
        if rpr is not None and rpr.find(_W + "b") is not None:
            bold = True
        lines.append(Line(page=page, text=text, size=0.0, bold=bold, y0=0.0, page_h=0.0,
                          style=style, level=level))
    return lines


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def extract_structured(path: Path, *, page_offset: int = 0) -> list[Line]:
    """Extrae líneas con jerarquía y ruido marcado. PDF o DOCX según extensión."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        lines = _pdf_lines(path)
        _mark_pdf_noise(lines)
        _classify_pdf_headings(lines)
    elif ext in (".docx",):
        lines = _docx_lines(path, page=1 + page_offset)
    else:
        raise ValueError(f"Formato no soportado para extracción estructurada: {ext}")
    return lines


def is_index_line(text: str) -> bool:
    """Línea de índice (dot leaders + nº de página al final)."""
    return bool(_DOTS_RE.search(text)) and bool(re.search(r"\d{1,4}\s*$", text))
