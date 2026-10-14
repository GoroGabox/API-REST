"""Lleva las páginas del libro del PLAN a un curso ya generado (sin regenerarlo).

Caso: el curso se redactó desde un plan sin páginas (guía en Word → todo "1-1") y
después el plan se alineó con el libro en PDF (`attach_figures --solo-paginas`). Este
comando actualiza, por `plan_id`, las `fuentes` de cada lección (página inicio/fin y
nombre de la fuente) y la línea de la sección "## Fuente" de su contenido. No toca el
texto, los recursos ni la auditoría, y no usa IA.

Uso::

    python manage.py sync_course_pages --plan out/plan_b_fig.json --course out/b.json --out out/b_pag.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

_FUENTE_RE = re.compile(r"(^##\s+Fuente\s*\n)(.*?)(?=\n##\s|\Z)", flags=re.S | re.M | re.I)


def sync_course_pages(plan: dict, course: dict, source_name: str | None = None) -> dict:
    """Actualiza ``course`` in place. Devuelve ``{lecciones, quizzes, sin_plan}``."""
    nombre = source_name or (plan.get("curso") or {}).get("fuente_nombre") or ""
    por_id, por_unidad = {}, {}
    for u in plan.get("unidades") or []:
        por_unidad[int(u.get("orden") or 0)] = u.get("paginas") or [0, 0]
        for lec in u.get("lecciones") or []:
            por_id[lec.get("id")] = lec.get("paginas") or [0, 0]

    hechas = quizzes = 0
    sin_plan: list[str] = []
    for lesson in course.get("lessons") or []:
        es_quiz = lesson.get("tipo") == "quiz"
        pgs = por_unidad.get(int(lesson.get("unidad_orden") or 0)) if es_quiz else por_id.get(lesson.get("plan_id"))
        if not pgs or not pgs[0]:
            if not es_quiz:
                sin_plan.append(str(lesson.get("nombre", "")))
            continue
        a, b = int(pgs[0]), int(pgs[1] or pgs[0])
        fuentes = lesson.get("fuentes") or []
        if fuentes:
            fuentes[0]["pagina_inicio"], fuentes[0]["pagina_fin"] = a, b
            if nombre:
                fuentes[0]["fuente_nombre"] = nombre
        if es_quiz:
            quizzes += 1
            continue
        contenido = lesson.get("contenido")
        if isinstance(contenido, str):
            fuente = nombre or (fuentes[0].get("fuente_nombre") if fuentes else "") or "Libro"
            linea = f"{fuente}, páginas {a}-{b}."
            lesson["contenido"] = _FUENTE_RE.sub(lambda m: m.group(1) + linea + "\n", contenido, count=1)
        hechas += 1
    return {"lecciones": hechas, "quizzes": quizzes, "sin_plan": sin_plan}


class Command(BaseCommand):
    help = "Copia las páginas del libro del plan a las fuentes de un curso ya generado (sin IA, sin regenerar)."

    def add_arguments(self, parser):
        parser.add_argument("--plan", required=True, help="plan.json con las páginas del libro por lección.")
        parser.add_argument("--course", required=True, help="JSON del curso generado desde ese plan.")
        parser.add_argument("--out", required=True, help="JSON del curso de salida.")
        parser.add_argument("--source-name", default=None,
                            help="Nombre de la fuente a citar (def. curso.fuente_nombre del plan).")

    def handle(self, *args, **opts):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
        plan = json.loads(Path(opts["plan"]).read_text(encoding="utf-8"))
        course = json.loads(Path(opts["course"]).read_text(encoding="utf-8"))
        if not isinstance(course.get("lessons"), list):
            raise CommandError("--course no parece un JSON de generate_course.")
        res = sync_course_pages(plan, course, opts["source_name"])
        out = Path(opts["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(course, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(
            f"Páginas del libro: {res['lecciones']} lecciones y {res['quizzes']} quizzes actualizados → {out}"))
        if res["sin_plan"]:
            self.stderr.write(self.style.WARNING(
                f"{len(res['sin_plan'])} lección(es) sin páginas en el plan: " + "; ".join(res["sin_plan"][:8])))
