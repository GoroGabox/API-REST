"""Construye un manifest de curso a partir del PDF de TEMARIO.

El pipeline (`map_topics` -> `generate_lessons`) está dirigido por un manifest
con la forma:

    {
      "curso": {nombre, codigo, descripcion, is_profesional, costo},
      "unidades": [
        {orden, nombre, categoria, horas_elearning, temas: [str, ...]},
        ...
      ]
    }

El A2 traía ese manifest escrito a mano. Para el generador genérico lo
derivamos del temario (un índice/estructura), detectando encabezados de unidad
y las líneas de tema que cuelgan de cada uno.

Supuestos: el temario es un documento *estructurado* (índice, malla, outline),
no prosa corrida, y el PDF trae texto real (no un escaneo sin OCR).
"""
from __future__ import annotations

import re
from typing import Any

from content_pipeline.processors.clean_text import (
    is_probable_heading,
    normalize_for_matching,
    shorten_text,
)

# "Unidad 3", "Módulo IV", "Capítulo 2", "Bloque 1", "Sección 5"
_UNIT_RE = re.compile(
    r"^\s*(?:unidad|m[oó]dulo|cap[ií]tulo|bloque|secci[oó]n)\s+"
    r"(?:\d+|[ivxlcdm]+)\b[\s.:;)\-–—]*",
    re.IGNORECASE,
)
_NUMBERED_RE = re.compile(r"^\s*\d+(?:[.)]\d+)*[.)]?\s+(.+)$")
_BULLET_RE = re.compile(r"^\s*[-•·*▪◦►o]\s+(.+)$")
_TOC_LEADER_RE = re.compile(r"[.·]{3,}\s*\d+\s*$")  # "..... 12" de índices


def _clean_line(line: str) -> str:
    line = _TOC_LEADER_RE.sub("", line)            # quita puntos guía + página del índice
    line = re.sub(r"\s+\d{1,4}\s*$", "", line)      # número de página suelto al final
    line = re.sub(r"\s+", " ", line)
    return line.strip(" .-–—•·*\t")


def _strip_unit_prefix(line: str) -> str:
    return _clean_line(_UNIT_RE.sub("", line))


def _looks_like_topic(text: str) -> bool:
    """Heurística para aceptar una línea como tema del temario."""
    if not (5 <= len(text) <= 200):
        return False
    words = text.split()
    if not (1 <= len(words) <= 16):  # más de 16 palabras => probablemente prosa
        return False
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 4:
        return False
    # descarta líneas que son mayormente dígitos/símbolos
    if len(letters) / len(text.replace(" ", "") or "x") < 0.5:
        return False
    return True


def _cap_to_max_lessons(units: list[dict[str, Any]], max_lecciones: int) -> None:
    """Recorta temas/unidades para respetar el techo de lecciones.

    Cada tema = 1 lección y cada unidad agrega 1 quiz de cierre, así que el
    total generado es sum(len(temas)) + len(units).
    """
    if max_lecciones <= 0:
        return

    def total() -> int:
        return sum(len(u["temas"]) for u in units) + len(units)

    # Primero poda los últimos temas de las unidades más cargadas.
    while total() > max_lecciones:
        richest = max(units, key=lambda u: len(u["temas"]))
        if len(richest["temas"]) <= 1:
            break
        richest["temas"].pop()

    # Si aún excede (demasiadas unidades), descarta unidades finales.
    while total() > max_lecciones and len(units) > 1:
        units.pop()

    for index, unit in enumerate(units, start=1):
        unit["orden"] = index


def build_manifest_from_temario(
    pages: list[dict[str, Any]],
    *,
    nombre: str,
    codigo: str,
    is_profesional: bool,
    descripcion: str = "",
    max_lecciones: int = 20,
) -> dict[str, Any]:
    units: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    seen_topics: set[str] = set()
    # Temas que aparecen antes del primer encabezado de unidad. Suelen ser
    # ruido (título del curso, "Índice", portada). Solo se materializan si el
    # documento no tiene ninguna unidad (temario plano, sin encabezados).
    preamble: list[str] = []

    def start_unit(name: str) -> None:
        nonlocal current
        name = _clean_line(name) or f"Unidad {len(units) + 1}"
        current = {
            "orden": len(units) + 1,
            "nombre": shorten_text(name, 100),
            "categoria": shorten_text(name, 100),
            "horas_elearning": 0,
            "temas": [],
        }
        units.append(current)

    def add_topic(text: str) -> None:
        text = _clean_line(text)
        if not _looks_like_topic(text):
            return
        key = normalize_for_matching(text)
        if not key or key in seen_topics:
            return
        seen_topics.add(key)
        target = current["temas"] if current is not None else preamble
        target.append(shorten_text(text, 200))

    for page in pages:
        if not page.get("has_text"):
            continue
        for raw in str(page.get("text", "")).splitlines():
            line = raw.strip()
            if not line:
                continue
            if _UNIT_RE.match(line):
                start_unit(_strip_unit_prefix(line))
                continue
            match = _NUMBERED_RE.match(line) or _BULLET_RE.match(line)
            if match:
                add_topic(match.group(1))
                continue
            if is_probable_heading(line):
                add_topic(line)

    # Temario plano (sin encabezados de unidad): todo el preámbulo es el curso.
    if not units and preamble:
        start_unit("Contenido")
        current["temas"] = preamble  # type: ignore[index]

    units = [unit for unit in units if unit["temas"]]
    if not units:
        raise ValueError(
            "No se detectaron unidades ni temas en el temario. "
            "Verifica que el PDF tenga texto seleccionable (no un escaneo) y "
            "una estructura de índice/outline."
        )

    _cap_to_max_lessons(units, max_lecciones)

    return {
        "curso": {
            "nombre": nombre,
            "codigo": codigo,
            "descripcion": descripcion
            or f"Curso generado automáticamente a partir del temario de {nombre}.",
            "is_profesional": bool(is_profesional),
            "costo": 0,
        },
        "unidades": units,
    }
