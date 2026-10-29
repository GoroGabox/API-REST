"""Componentes de contenido en el Markdown de una lección: recuadros y tablas.

Contrato compartido con los renderizadores (webapp, app Expo y Brújula); ver
``docs/contenido_lecciones.md``. Los casos de ``content_pipeline/review/fixtures_contenido.json``
son la referencia: cada cliente comprueba su parser contra ellos.

Recuadro (alerta estilo GitHub; un renderizador sin soporte lo muestra como cita)::

    > [!DEBES_SABER] Título opcional
    > Cuerpo con **negritas** y viñetas.

Tabla (GFM), con título opcional en la línea anterior::

    Tabla: Estadísticas en Chile
    | Indicador | Valor |
    |---|---:|
    | Siniestros al año | 82.000 |

Aquí, todo determinista:
- ``parse_blocks``: Markdown → bloques (el modelo del contrato).
- ``validate_components``: errores (bloquean el import) y advertencias.
- ``strip_components``: texto plano para guion TTS, juez, cifras y anclaje.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

# Tipo → rótulo por defecto. El orden es el de prioridad (de mayor a menor).
TIPOS_RECUADRO: dict[str, str] = {
    "NORMA": "Norma y sanción",
    "ATENCION": "Atención",
    "DEBES_SABER": "Debes saber",
    "DATO": "Dato",
    "CONSEJO": "Consejo",
}
MAX_COLUMNAS = 6
MAX_RECUADROS = 4
# Secciones de lectura: las únicas donde van recuadros y tablas.
SECCIONES_LECTURA = ("desarrollo", "introduccion", "aplicacion practica", "ejemplo aplicado")

_FIG_RE = re.compile(r"^\{\{\s*figura\s*:\s*([A-Za-z0-9_-]+)\s*\}\}$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_FENCE_RE = re.compile(r"^```")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_CALLOUT_RE = re.compile(r"^\[!([A-Za-z_]+)\]\s*(.*)$")
_UL_RE = re.compile(r"^[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^\d+[.)]\s+(.*)$")
_TABLE_TITLE_RE = re.compile(r"^Tabla:\s*(.+)$")
_SEP_CELL_RE = re.compile(r"^:?-+:?$")


# ---------------------------------------------------------------------------
# Tablas
# ---------------------------------------------------------------------------

def _split_row(line: str) -> list[str]:
    """Celdas de una fila ``| a | b |`` (``\\|`` es una barra literal)."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells = re.split(r"(?<!\\)\|", s)
    return [c.strip().replace("\\|", "|") for c in cells]


def _is_table_row(line: str) -> bool:
    return line.strip().startswith("|")


def _is_separator(line: str) -> bool:
    if not _is_table_row(line):
        return False
    cells = _split_row(line)
    return bool(cells) and all(_SEP_CELL_RE.match(c.replace(" ", "")) for c in cells)


def _align(cell: str) -> str | None:
    c = cell.replace(" ", "")
    if c.startswith(":") and c.endswith(":"):
        return "center"
    if c.endswith(":"):
        return "right"
    if c.startswith(":"):
        return "left"
    return None


def _starts_table(lines: list[str], i: int) -> bool:
    return i + 1 < len(lines) and _is_table_row(lines[i]) and _is_separator(lines[i + 1])


# ---------------------------------------------------------------------------
# Bloques
# ---------------------------------------------------------------------------

def _starts_block(lines: list[str], i: int) -> bool:
    s = lines[i].strip()
    return bool(
        not s or _FIG_RE.match(s) or _HEADING_RE.match(s) or _HR_RE.match(s) or _FENCE_RE.match(s)
        or s.startswith(">") or _UL_RE.match(s) or _OL_RE.match(s) or _starts_table(lines, i)
        or (_TABLE_TITLE_RE.match(s) and _starts_table(lines, i + 1))
    )


def parse_blocks(md: str, *, _en_recuadro: bool = False) -> list[dict[str, Any]]:
    """Markdown → lista de bloques del contrato.

    Bloques: ``heading {level, text}``, ``paragraph {text}``, ``list {ordered, items}``,
    ``callout {tipo, titulo, blocks}``, ``quote {blocks}``, ``table {titulo, align, header, rows}``,
    ``figura {id}``, ``code {text}``, ``hr``. El texto de línea queda crudo (``**``, ``*``, `` ` ``):
    cada renderizador lo interpreta con las mismas reglas de línea.
    """
    lines = str(md or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[dict[str, Any]] = []
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if _FENCE_RE.match(s):
            j = i + 1
            while j < n and not _FENCE_RE.match(lines[j].strip()):
                j += 1
            blocks.append({"t": "code", "text": "\n".join(lines[i + 1:j])})
            i = j + 1
            continue
        if m := _FIG_RE.match(s):
            blocks.append({"t": "figura", "id": m.group(1)})
            i += 1
            continue
        if m := _HEADING_RE.match(s):
            blocks.append({"t": "heading", "level": len(m.group(1)), "text": m.group(2)})
            i += 1
            continue
        if _HR_RE.match(s):
            blocks.append({"t": "hr"})
            i += 1
            continue
        if s.startswith(">"):
            body: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                body.append(_QUOTE_RE.match(lines[i].strip()).group(1))
                i += 1
            m = _CALLOUT_RE.match(body[0].strip()) if not _en_recuadro else None
            if m:
                blocks.append({"t": "callout", "tipo": m.group(1).upper(), "titulo": m.group(2).strip() or None,
                               "blocks": parse_blocks("\n".join(body[1:]), _en_recuadro=True)})
            else:
                blocks.append({"t": "quote", "blocks": parse_blocks("\n".join(body), _en_recuadro=True)})
            continue
        titulo = None
        if (m := _TABLE_TITLE_RE.match(s)) and _starts_table(lines, i + 1):
            titulo = m.group(1).strip()
            i += 1
        if _starts_table(lines, i):
            header = _split_row(lines[i])
            align = [_align(c) for c in _split_row(lines[i + 1])]
            rows = []
            i += 2
            while i < n and _is_table_row(lines[i]):
                rows.append(_split_row(lines[i]))
                i += 1
            blocks.append({"t": "table", "titulo": titulo, "align": align, "header": header, "rows": rows})
            continue
        if _UL_RE.match(s) or _OL_RE.match(s):
            ordered = bool(_OL_RE.match(s))
            item_re = _OL_RE if ordered else _UL_RE
            items: list[str] = []
            while i < n:
                raw = lines[i]
                st = raw.strip()
                if m := item_re.match(st):
                    items.append(m.group(1).strip())
                elif st and raw[:1] in (" ", "\t") and items and not _starts_block(lines, i):
                    items[-1] += " " + st                       # continuación indentada
                else:
                    break
                i += 1
            blocks.append({"t": "list", "ordered": ordered, "items": items})
            continue
        para = [s]
        i += 1
        while i < n and not _starts_block(lines, i):
            para.append(lines[i].strip())
            i += 1
        blocks.append({"t": "paragraph", "text": " ".join(para)})
    return blocks


# ---------------------------------------------------------------------------
# Texto de línea
# ---------------------------------------------------------------------------

# Orden de prueba en cada posición: código, enlace, negrita, cursiva. Sin anidar.
_INLINE_RE = re.compile(
    r"`(?P<code>[^`]+)`"
    r"|\[(?P<ltext>[^\]]+)\]\((?P<href>https?://[^)\s]+)\)"
    r"|\*\*(?P<strong>.+?)\*\*|__(?P<strong2>.+?)__"
    r"|\*(?P<em>[^*\s][^*]*?)\*|(?<![\w])_(?P<em2>[^_\s][^_]*?)_(?![\w])"
)


def parse_inline(text: str) -> list[dict[str, str]]:
    """Texto de línea → ``[{t: text|strong|em|code|link, v, href?}]`` (tokens contiguos)."""
    out: list[dict[str, str]] = []
    pos = 0
    s = str(text or "")
    for m in _INLINE_RE.finditer(s):
        if m.start() > pos:
            out.append({"t": "text", "v": s[pos:m.start()]})
        if m.group("code") is not None:
            out.append({"t": "code", "v": m.group("code")})
        elif m.group("href") is not None:
            out.append({"t": "link", "v": m.group("ltext"), "href": m.group("href")})
        elif (v := m.group("strong") or m.group("strong2")) is not None:
            out.append({"t": "strong", "v": v})
        else:
            out.append({"t": "em", "v": m.group("em") or m.group("em2")})
        pos = m.end()
    if pos < len(s):
        out.append({"t": "text", "v": s[pos:]})
    return out


# ---------------------------------------------------------------------------
# Validación
# ---------------------------------------------------------------------------

def _norm(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in t if not unicodedata.combining(c)).strip()


def validate_components(md: str) -> tuple[list[str], list[str]]:
    """(errores, advertencias) de los componentes de una lección.

    Errores: tipo de recuadro desconocido, recuadro vacío o anidado, tabla sin filas,
    con más de ``MAX_COLUMNAS`` columnas o con filas de otro ancho que el encabezado.
    Advertencias: componentes fuera de las secciones de lectura o más de ``MAX_RECUADROS``.
    """
    errores: list[str] = []
    advertencias: list[str] = []
    seccion = ""
    recuadros = 0
    for b in parse_blocks(md):
        if b["t"] == "heading" and b["level"] <= 2:
            seccion = _norm(b["text"])
            continue
        if b["t"] not in ("callout", "table"):
            continue
        fuera = seccion and not any(seccion.startswith(s) for s in SECCIONES_LECTURA)
        if b["t"] == "callout":
            recuadros += 1
            nombre = f"recuadro [!{b['tipo']}]"
            if b["tipo"] not in TIPOS_RECUADRO:
                errores.append(f"{nombre}: tipo desconocido (usar {', '.join(TIPOS_RECUADRO)}).")
            if not b["blocks"]:
                errores.append(f"{nombre}: sin contenido.")
            if any(x["t"] == "paragraph" and _CALLOUT_RE.match(x["text"]) for x in b["blocks"]):
                errores.append(f"{nombre}: no se pueden anidar recuadros.")
            if any(x["t"] == "table" for x in b["blocks"]):
                errores.append(f"{nombre}: una tabla no puede ir dentro de un recuadro.")
        else:
            nombre = f"tabla «{b['titulo'] or ' | '.join(b['header'])[:40]}»"
            cols = len(b["header"])
            if cols > MAX_COLUMNAS:
                errores.append(f"{nombre}: {cols} columnas (máximo {MAX_COLUMNAS}).")
            if not b["rows"]:
                errores.append(f"{nombre}: sin filas.")
            if len(b["align"]) != cols:
                errores.append(f"{nombre}: la fila separadora tiene {len(b['align'])} columnas y el encabezado {cols}.")
            malas = [k for k, r in enumerate(b["rows"], start=1) if len(r) != cols]
            if malas:
                errores.append(f"{nombre}: fila(s) {', '.join(map(str, malas))} con un número de columnas distinto "
                               f"al encabezado ({cols}).")
        if fuera:
            advertencias.append(f"{nombre} fuera de las secciones de lectura (está en «{seccion}»).")
    if recuadros > MAX_RECUADROS:
        advertencias.append(f"{recuadros} recuadros en la lección (recomendado: hasta {MAX_RECUADROS}).")
    return errores, advertencias


# ---------------------------------------------------------------------------
# Texto plano (TTS, juez, cifras, anclaje)
# ---------------------------------------------------------------------------

def _table_text(b: dict[str, Any]) -> str:
    """Una frase por fila: «Encabezado: valor; Encabezado: valor.»."""
    out = [b["titulo"] + "."] if b.get("titulo") else []
    for row in b["rows"]:
        partes = [f"{h}: {v}" if h else v for h, v in zip(b["header"], row) if v]
        if partes:
            out.append("; ".join(partes) + ".")
    return "\n".join(out)


def strip_components(md: str) -> str:
    """Markdown sin sintaxis de componentes (sin parsear el resto).

    - Recuadro: quita ``> `` y la etiqueta ``[!TIPO]``; un título propio queda como línea.
    - Tabla: frases «Encabezado: valor; …» por fila (las cifras se siguen auditando).
    El resto del Markdown queda intacto.
    """
    lines = str(md or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if s.startswith(">"):
            body = []
            while i < n and lines[i].strip().startswith(">"):
                body.append(_QUOTE_RE.match(lines[i].strip()).group(1))
                i += 1
            m = _CALLOUT_RE.match(body[0].strip())
            if m:
                body = ([m.group(2).strip()] if m.group(2).strip() else []) + body[1:]
            out.extend(body)
            continue
        titulo_tabla = (m := _TABLE_TITLE_RE.match(s)) and _starts_table(lines, i + 1)
        if titulo_tabla or _starts_table(lines, i):
            j = i + (1 if titulo_tabla else 0) + 2
            while j < n and _is_table_row(lines[j]):
                j += 1
            tabla = parse_blocks("\n".join(lines[i:j]))[0]
            out.append(_table_text(tabla))
            i = j
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)
