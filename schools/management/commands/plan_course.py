"""Genera el PLAN de curso (unidades + lecciones) desde el libro, para revisar.

Fase 1 del flujo dirigido por el índice. NO usa IA ni toca la BD: detecta la
estructura por tipografía/archivos y reparte cada capítulo en lecciones según la
banda de longitud (`--largo`). El JSON resultante se revisa/edita a mano y luego
se redacta con `generate_course --from-plan`.

Uso::

    # Libro único (capítulos por tipografía)
    python manage.py plan_course --contenido "libro_b.pdf" --nombre "Curso Clase B" --codigo B --largo media --out out/plan_b.json
    # Carpeta de capítulos (un archivo por capítulo)
    python manage.py plan_course --dir "D:/AutoTest/libro B" --nombre "Curso Clase B" --codigo B --largo media --out out/plan_b.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_pipeline.services.course_planner import BANDAS, build_plan


class Command(BaseCommand):
    help = "Genera el plan editable (unidades + lecciones) desde el libro, sin IA."

    def add_arguments(self, parser):
        parser.add_argument("--contenido", default=None, help="Libro único (PDF/DOCX).")
        parser.add_argument("--dir", default=None, help="Carpeta con un archivo por capítulo (docx/pdf).")
        parser.add_argument("--nombre", required=True)
        parser.add_argument("--codigo", required=True)
        parser.add_argument("--largo", choices=list(BANDAS), default="media",
                            help="Longitud por lección: corta|media|larga.")
        parser.add_argument("--is-profesional", action="store_true")
        parser.add_argument("--ia", action="store_true",
                            help="Enriquecer con IA: clasifica categorías (taxonomía) y nombra las lecciones.")
        parser.add_argument("--plan-model", default=None,
                            help="Modelo para el enriquecimiento (def. Haiku; trabajo ligero).")
        parser.add_argument("--out", required=True, help="Ruta del plan.json de salida.")

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

        codigo = opts["codigo"].strip().upper()
        self.stdout.write(f"Construyendo outline desde: {source}")
        plan = build_plan(
            source, nombre=opts["nombre"].strip(), codigo=codigo,
            largo=opts["largo"], is_profesional=opts["is_profesional"],
        )

        if opts["ia"]:
            from content_pipeline.llm.client import LLMClient, draft_model
            if not LLMClient.is_available():
                self.stderr.write(self.style.WARNING(
                    "--ia requiere ANTHROPIC_API_KEY; se omite el enriquecimiento."))
            else:
                from content_pipeline.services.plan_enrich import enrich_plan
                model = opts["plan_model"] or draft_model()
                client = LLMClient(model=model)
                self.stdout.write(f"Enriqueciendo con IA ({model}): categorías + nombres de lección…")
                enrich_plan(plan, client=client, model=model)
                self.stdout.write(f"  ~US${round(client.meter.cost_usd, 4)} · {client.meter.calls} llamadas")

        r = plan["resumen"]
        self.stdout.write(self.style.SUCCESS(
            f"Plan: {r['unidades']} unidades · {r['lecciones']} lecciones · "
            f"{r['palabras_totales']} palabras · banda {opts['largo']} {r['banda']['min']}-{r['banda']['max']}"
        ))
        for u in plan["unidades"]:
            self.stdout.write(
                f"  U{u['orden']} {u['nombre'][:48]:48} · {len(u['lecciones'])} lecc · "
                f"{u['palabras']} pal · págs {u['paginas'][0]}-{u['paginas'][1]} · [{u['categoria']}]"
            )

        out = Path(opts["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Plan escrito: {out}"))
        self.stdout.write("Revisá/editá el plan y luego: python manage.py generate_course --from-plan " + str(out))
