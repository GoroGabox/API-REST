"""Ids estables para planes y cursos.

Las observaciones de los auditores apuntan a unidades/lecciones por id, así que
el id tiene que sobrevivir a renombres y reordenamientos:

- lección del plan: ``L-`` + sha256(texto fuente)[:10] (el texto es la procedencia).
- unidad del plan:  ``U-`` + sha256(nombre + id de su primera lección)[:8].
- lección de curso: clave ``U{unidad_orden}.{posicion}`` (+ ``plan_id`` si salió de un plan).

La Brújula (``brujula.html``) deriva ids nuevos al dividir (``-a``/``-b``) o unir
(``+``) lecciones con la misma convención que ``apply.py``.
"""
from __future__ import annotations

import hashlib
from typing import Any


def _sha(text: str, n: int) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def lesson_id(texto: str) -> str:
    return "L-" + _sha(str(texto or ""), 10)


def unit_id(nombre: str, first_lesson_id: str) -> str:
    return "U-" + _sha(f"{nombre}|{first_lesson_id}", 8)


def course_key(lesson: dict[str, Any]) -> str:
    return f"U{int(lesson.get('unidad_orden') or 0)}.{int(lesson.get('posicion') or 0)}"


def ensure_plan_ids(plan: dict[str, Any]) -> dict[str, Any]:
    """Asigna ids a unidades/lecciones que no los tengan (in-place). Deduplica con sufijo."""
    seen: set[str] = set()

    def unique(base: str) -> str:
        cand, n = base, 2
        while cand in seen:
            cand, n = f"{base}~{n}", n + 1
        seen.add(cand)
        return cand

    for unidad in plan.get("unidades", []):
        for lec in unidad.get("lecciones", []):
            lec["id"] = unique(lec.get("id") or lesson_id(lec.get("texto", "")))
        first = (unidad.get("lecciones") or [{}])[0].get("id", "")
        unidad["id"] = unique(unidad.get("id") or unit_id(str(unidad.get("nombre", "")), first))
    return plan
