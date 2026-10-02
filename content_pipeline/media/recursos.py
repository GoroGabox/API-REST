"""Recursos de lección en el JSON del curso (``lessons[].recursos``).

Mismo formato que ``schools.LeccionRecurso``: ``{tipo, rol, clave, url, orden, titulo,
meta}``. Mientras un archivo no está publicado lleva ``archivo_local`` y ``url`` vacía
(``publish_media`` lo sube y rellena la URL; ``import_course`` omite los que no tienen).
La ``clave`` identifica al recurso: el marcador ``{{figura:<clave>}}`` del contenido,
``audio`` para la narración o ``paginas`` para las páginas del libro.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def figura_clave(hash_: str) -> str:
    """Id estable de una figura (contenido): ``F-`` + 10 hex del sha1."""
    return f"F-{hash_[:10]}"


def upsert_recurso(lesson: dict[str, Any], recurso: dict[str, Any]) -> dict[str, Any]:
    """Agrega o reemplaza (por ``clave``) un recurso de la lección. Devuelve el recurso."""
    recursos = lesson.setdefault("recursos", [])
    for i, r in enumerate(recursos):
        if r.get("clave") == recurso["clave"]:
            recursos[i] = {**r, **recurso}
            return recursos[i]
    recursos.append(recurso)
    return recurso


_FIG_META = ("pie", "alt", "pagina", "parrafo", "ancho", "alto", "origen", "mapeo", "hash", "ext")


def figura_recurso(entrada: dict[str, Any], *, orden: int, archivo_local: str = "") -> dict[str, Any]:
    """Recurso ``imagen/figura`` desde una entrada de figura (``images.save_figures`` /
    ``plan.lecciones[].figuras``). ``archivo_local`` = ruta relativa al JSON del curso."""
    rec: dict[str, Any] = {
        "tipo": "imagen", "rol": "figura", "clave": entrada["id"], "url": entrada.get("url") or "",
        "orden": orden, "titulo": "",
        "meta": {k: entrada.get(k) for k in _FIG_META if entrada.get(k) is not None},
    }
    local = archivo_local or entrada.get("archivo") or ""
    if local:
        rec["archivo_local"] = local
    return rec


def recursos_de(lesson: dict[str, Any], *, tipo: str | None = None, rol: str | None = None) -> list[dict[str, Any]]:
    return [r for r in lesson.get("recursos") or []
            if (tipo is None or r.get("tipo") == tipo) and (rol is None or r.get("rol") == rol)]


def rebase_archivos(lessons: list[dict[str, Any]], desde: str | Path, hacia: str | Path) -> int:
    """Rehace ``archivo_local`` (relativo a ``desde``, p. ej. la carpeta del plan) para que
    quede relativo a ``hacia`` (la carpeta del JSON del curso). Devuelve cuántos cambió."""
    n = 0
    for lesson in lessons:
        for r in lesson.get("recursos") or []:
            local = r.get("archivo_local")
            if not local or Path(local).is_absolute():
                continue
            target = (Path(desde) / local).resolve()
            try:
                r["archivo_local"] = Path(os.path.relpath(target, Path(hacia).resolve())).as_posix()
            except ValueError:                     # otra unidad de disco (Windows)
                r["archivo_local"] = target.as_posix()
            n += 1
    return n
