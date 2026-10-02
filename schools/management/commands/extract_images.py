"""Agrega las figuras del libro a un curso YA generado (sin marcadores en el texto).

Para cursos nuevos las figuras se extraen en la fase de plan (`plan_course`) y el
redactor las inserta con `{{figura:<id>}}`. Este comando cubre los cursos generados
antes: ubica cada figura en la lección del PLAN cuyo texto fuente contiene el texto
que la rodea en el libro (respaldo: página en PDF / unidad en carpeta de Word), la
guarda en `<out>/../figuras_<codigo>/` y la agrega a la lección del curso (por
`plan_id`) como recurso `imagen/figura` con `archivo_local`. Los clientes muestran
las figuras no referenciadas en el texto como galería. No toca la BD ni sube nada:
después `publish_media` (subida) e `import_course`.

Uso::

    python manage.py extract_images --plan out/plan_b.json --course out/b.json --contenido libro.pdf --out out/b_img.json [--describir]
    python manage.py publish_media --file out/b_img.json --out out/b_pub.json --storage s3
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_pipeline.media.recursos import figura_recurso, upsert_recurso
from content_pipeline.services.plan_figures import attach_figures_to_plan


def _course_lessons_by_plan(course: dict) -> tuple[dict[str, dict], dict[tuple[int, str], dict]]:
    by_id, by_name = {}, {}
    for lec in course.get("lessons") or []:
        if lec.get("plan_id"):
            by_id[lec["plan_id"]] = lec
        by_name[(int(lec.get("unidad_orden") or 0), str(lec.get("nombre") or "").strip().lower())] = lec
    return by_id, by_name


class Command(BaseCommand):
    help = "Agrega las figuras del libro (recursos locales) a un curso ya generado. No toca la BD."

    def add_arguments(self, parser):
        parser.add_argument("--plan", required=True, help="plan.json usado para generar el curso.")
        parser.add_argument("--course", required=True, help="JSON del curso (salida de generate_course).")
        parser.add_argument("--contenido", default=None, help="Libro único (PDF/DOCX).")
        parser.add_argument("--dir", default=None, help="Carpeta con un archivo por capítulo (docx/pdf).")
        parser.add_argument("--out", required=True, help="JSON de salida (curso + recursos).")
        parser.add_argument("--describir", action="store_true",
                            help="IA con visión: pie + alt y descarte de figuras decorativas.")
        parser.add_argument("--describe-model", default=None, help="Modelo de visión (def. Haiku).")
        parser.add_argument("--limit", type=int, default=0, help="Procesar solo las primeras N figuras.")

    def handle(self, *args, **opts):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
        if bool(opts["contenido"]) == bool(opts["dir"]):
            raise CommandError("Pasá exactamente uno: --contenido (libro único) o --dir (carpeta de capítulos).")
        source = opts["dir"] or opts["contenido"]
        if not Path(source).exists():
            raise CommandError(f"No existe: {source}")
        plan = json.loads(Path(opts["plan"]).read_text(encoding="utf-8"))
        course = json.loads(Path(opts["course"]).read_text(encoding="utf-8"))
        codigo = str(((course.get("manifest") or {}).get("curso") or {}).get("codigo")
                     or (plan.get("curso") or {}).get("codigo") or "curso").strip().upper()
        out = Path(opts["out"])
        out.parent.mkdir(parents=True, exist_ok=True)

        client = model = None
        if opts["describir"]:
            from content_pipeline.llm.client import LLMClient, draft_model
            if LLMClient.is_available():
                model = opts["describe_model"] or draft_model()
                client = LLMClient(model=model)
            else:
                self.stderr.write(self.style.WARNING("--describir requiere ANTHROPIC_API_KEY; se omite."))

        self.stdout.write(f"Extrayendo figuras de: {source}")
        resumen = attach_figures_to_plan(plan, source, out.parent / f"figuras_{codigo}", rel_to=out.parent,
                                         client=client, model=model, limit=opts["limit"])
        if client is not None:
            self.stdout.write(f"  IA ({model}): ~US${round(client.meter.cost_usd, 4)} · {client.meter.calls} llamadas")

        by_id, by_name = _course_lessons_by_plan(course)
        por_leccion: dict[str, int] = {}
        sin_curso = 0
        for u in plan.get("unidades") or []:
            for plan_lec in u.get("lecciones") or []:
                figuras = plan_lec.get("figuras") or []
                if not figuras:
                    continue
                lesson = by_id.get(plan_lec.get("id")) or by_name.get(
                    (int(u.get("orden") or 0), str(plan_lec.get("nombre", "")).strip().lower()))
                if lesson is None:
                    sin_curso += len(figuras)
                    continue
                lesson["recursos"] = [r for r in lesson.get("recursos") or [] if r.get("rol") != "figura"]
                for k, entrada in enumerate(figuras, start=1):
                    upsert_recurso(lesson, figura_recurso(entrada, orden=k))
                lesson.pop("imagenes", None)          # formato previo
                por_leccion[lesson.get("nombre", "")] = len(figuras)

        course["imagenes_meta"] = {**resumen, "fuente": str(source), "por_leccion": por_leccion,
                                   "sin_leccion_en_curso": sin_curso}
        out.write_text(json.dumps(course, ensure_ascii=False, indent=2), encoding="utf-8")

        self.stdout.write(self.style.SUCCESS(
            f"Figuras: {resumen['asignadas']} en {len(por_leccion)} lecciones · "
            f"{len(resumen['sin_leccion'])} sin lección · mapeo {resumen['mapeo']} · "
            f"descartadas {resumen['descartadas']}"))
        for nombre, n in por_leccion.items():
            self.stdout.write(f"  {n:>3} · {nombre[:70]}")
        if sin_curso:
            self.stderr.write(self.style.WARNING(
                f"{sin_curso} figura(s) de lecciones del plan que no están en el curso (¿plan distinto?)."))
        if resumen["visuales_sin_figuras"]:
            self.stderr.write(self.style.WARNING(
                "Lecciones gráficas sin figuras (revisar a mano): " + "; ".join(resumen["visuales_sin_figuras"][:10])))
        self.stdout.write(f"Archivos en {(out.parent / f'figuras_{codigo}').as_posix()} · escrito: {out}")
        self.stdout.write(f"Luego: python manage.py publish_media --file {out} --out <curso_publicado.json>")
