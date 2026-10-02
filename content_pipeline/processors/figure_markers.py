"""Marcadores de figura en el contenido de una lección: ``{{figura:<id>}}``.

El redactor recibe las figuras del plan (``lecciones[].figuras``) y las inserta en el
Markdown con su marcador, en una línea propia, junto al contenido que ilustran. Los
clientes reemplazan cada marcador por la figura (``LeccionRecurso`` con esa clave).

Aquí, todo determinista:
- ``figures_prompt``: bloque del prompt con las figuras disponibles.
- ``sanitize_markers``: tras el LLM quita ids desconocidos, deduplica, deja cada
  marcador en su línea y agrega al final de "## Desarrollo" las figuras que el
  redactor no usó (así ninguna se pierde; quedan marcadas como ``auto``).
- ``strip_markers``: texto sin marcadores (auditoría de cifras/anclaje, guion TTS) o
  con ``[Figura: pie]`` (juez).
"""
from __future__ import annotations

import re
from typing import Any

MARKER_RE = re.compile(r"\{\{\s*figura\s*:\s*([A-Za-z0-9_-]+)\s*\}\}")


def marker(fid: str) -> str:
    return "{{figura:" + fid + "}}"


def figure_ids(text: str) -> list[str]:
    return MARKER_RE.findall(str(text or ""))


def _paragraphs(texto: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", str(texto or "")) if p.strip()]


def figures_prompt(figuras: list[dict[str, Any]], texto: str) -> str:
    """Lista de figuras para el prompt del redactor (vacío si no hay)."""
    if not figuras:
        return ""
    paras = _paragraphs(texto)
    lines = []
    for f in figuras:
        desc = []
        if f.get("pie"):
            desc.append(f"pie: «{f['pie']}»")
        if f.get("alt") and f.get("alt") != f.get("pie"):
            desc.append(f"muestra: «{f['alt']}»")
        par = f.get("parrafo")
        if par is None:
            donde = "en las mismas páginas del libro"
        elif int(par) < 0:
            donde = "al inicio del extracto"
        else:
            inicio = " ".join(paras[int(par)].split()[:10]) if int(par) < len(paras) else ""
            donde = f"en el libro aparece tras el párrafo {int(par) + 1}" + (f" («{inicio}…»)" if inicio else "")
        lines.append(f"- {marker(f['id'])} — {'; '.join(desc) or 'sin descripción'} — {donde}")
    return (
        "\n\nFIGURAS DEL LIBRO para esta lección. Insértalas en el Markdown con su marcador "
        "exacto, cada una en una línea propia, junto al contenido que ilustran (cada marcador "
        "una sola vez). Puedes remitir a ellas (\"como muestra la figura\"), pero describe solo "
        "lo que dicen el extracto y su pie; no inventes lo que muestran:\n" + "\n".join(lines) + "\n"
    )


def _insert_in_desarrollo(body: str, block: str) -> str:
    """Agrega ``block`` al final de la sección "## Desarrollo" (o antes de "## Fuente")."""
    m = re.search(r"^##\s+Desarrollo\b.*$", body, flags=re.M | re.I)
    if m:
        nxt = re.search(r"^##\s+", body[m.end():], flags=re.M)
        pos = m.end() + nxt.start() if nxt else len(body)
    else:
        f = re.search(r"^##\s+Fuente\b", body, flags=re.M | re.I)
        pos = f.start() if f else len(body)
    head, tail = body[:pos].rstrip(), body[pos:]
    return f"{head}\n\n{block}\n\n{tail.lstrip()}" if tail.strip() else f"{head}\n\n{block}\n"


def sanitize_markers(body: str, figuras: list[dict[str, Any]] | None) -> tuple[str, dict[str, Any]]:
    """(cuerpo saneado, info) — info: ``usadas``, ``auto``, ``desconocidas``, ``duplicadas``."""
    validos = {f["id"] for f in figuras or [] if f.get("id")}
    vistos: list[str] = []
    desconocidas: list[str] = []
    duplicadas: list[str] = []

    def repl(m: re.Match) -> str:
        fid = m.group(1)
        if fid not in validos:
            desconocidas.append(fid)
            return ""
        if fid in vistos:
            duplicadas.append(fid)
            return ""
        vistos.append(fid)
        return "\n\n" + marker(fid) + "\n\n"            # siempre en su propia línea

    out = MARKER_RE.sub(repl, str(body or ""))
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    auto = [f["id"] for f in figuras or [] if f.get("id") and f["id"] not in vistos]
    if auto:
        out = _insert_in_desarrollo(out, "\n\n".join(marker(fid) for fid in auto))
    return out.strip() + "\n", {"usadas": vistos, "auto": auto, "desconocidas": desconocidas,
                                "duplicadas": duplicadas}


def strip_markers(text: str, figuras: list[dict[str, Any]] | None = None, *, modo: str = "quitar") -> str:
    """Sin marcadores (``quitar``) o como ``[Figura: pie]`` (``pie``, para el juez)."""
    if modo == "pie":
        pies = {f.get("id") or f.get("clave"): (f.get("pie") or (f.get("meta") or {}).get("pie") or "")
                for f in figuras or []}
        out = MARKER_RE.sub(lambda m: f"[Figura: {pies.get(m.group(1)) or 'sin pie'}]", str(text or ""))
    else:
        out = MARKER_RE.sub("", str(text or ""))
    return re.sub(r"\n{3,}", "\n\n", out)
