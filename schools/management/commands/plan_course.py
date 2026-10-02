"""Genera el PLAN de curso (unidades + lecciones) desde el libro, para revisar.

Fase 1 del flujo dirigido por el índice. No redacta ni toca la BD: toma los
capítulos de los archivos (`--dir`) o del libro, y la IA (modelo principal, Sonnet)
agrupa los párrafos de cada capítulo en lecciones por TEMA y DENSIDAD, con la
granularidad de `--largo` (corta|media|larga); las palabras solo son tope de
seguridad. Si el libro único no trae capítulos detectables, la IA propone también
las unidades. La IA solo devuelve rangos de párrafos → el texto de cada lección es
la fuente exacta. Sin `ANTHROPIC_API_KEY` o con `--sin-ia-estructura` se corta por
palabras (como antes). Además extrae las FIGURAS del libro y las ubica por lección y
párrafo (`figuras_<codigo>/` junto al plan; `--sin-figuras` lo apaga, `--describir-figuras`
agrega pie/alt con IA de visión). El JSON resultante se revisa/edita a mano (o en la
Brújula) y luego se redacta con `generate_course --from-plan`.

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
    help = "Genera el plan editable (unidades + lecciones) desde el libro; la IA decide los cortes por tema."

    def add_arguments(self, parser):
        parser.add_argument("--contenido", default=None, help="Libro único (PDF/DOCX).")
        parser.add_argument("--dir", default=None, help="Carpeta con un archivo por capítulo (docx/pdf).")
        parser.add_argument("--nombre", required=True)
        parser.add_argument("--codigo", required=True)
        parser.add_argument("--largo", choices=list(BANDAS), default="media",
                            help="Granularidad por lección: corta (≈1 subtema) | media | larga (≈1 tema). "
                                 "Las palabras de la banda son solo tope.")
        parser.add_argument("--sin-ia-estructura", action="store_true",
                            help="No usar IA para los cortes: reparte por palabras (offline).")
        parser.add_argument("--structure-model", default=None,
                            help="Modelo para la estructura (def. COURSE_LLM_MODEL, Sonnet).")
        parser.add_argument("--is-profesional", action="store_true")
        parser.add_argument("--ia", action="store_true",
                            help="Enriquecer con IA: clasifica categorías (taxonomía) y nombra las lecciones.")
        parser.add_argument("--plan-model", default=None,
                            help="Modelo para el enriquecimiento (def. Haiku; trabajo ligero).")
        parser.add_argument("--sin-figuras", action="store_true",
                            help="No extraer las figuras del libro (por defecto se extraen y ubican por párrafo).")
        parser.add_argument("--describir-figuras", action="store_true",
                            help="IA con visión (Haiku): pie + alt por figura y descarte de decorativas.")
        parser.add_argument("--describe-model", default=None, help="Modelo de visión (def. Haiku).")
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
        segmentador = None
        if not opts["sin_ia_estructura"]:
            from content_pipeline.llm.client import LLMClient
            if LLMClient.is_available():
                from content_pipeline.services.plan_structure import LLMSegmenter
                segmentador = LLMSegmenter(model=opts["structure_model"])
                self.stdout.write(f"Estructura por IA ({segmentador.model}): cortes por tema y densidad…")
            else:
                self.stderr.write(self.style.WARNING(
                    "Sin ANTHROPIC_API_KEY: las lecciones se cortan por palabras (sin criterio de tema)."))
        plan = build_plan(
            source, nombre=opts["nombre"].strip(), codigo=codigo,
            largo=opts["largo"], is_profesional=opts["is_profesional"],
            segmentador=segmentador,
        )
        if segmentador is not None:
            m = segmentador.client.meter
            self.stdout.write(f"  ~US${round(m.cost_usd, 4)} · {m.calls} llamadas")
            errores = plan["resumen"]["estructura"].get("errores") or {}
            for titulo, motivo in errores.items():
                self.stderr.write(self.style.WARNING(
                    f"  IA sin estructura para '{titulo}' ({motivo}); se cortó por palabras."))

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

        out = Path(opts["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        if not opts["sin_figuras"]:
            self._figuras(plan, source, out, codigo, opts)

        r = plan["resumen"]
        self.stdout.write(self.style.SUCCESS(
            f"Plan: {r['unidades']} unidades · {r['lecciones']} lecciones · "
            f"{r['palabras_totales']} palabras · {opts['largo']} (tope {r['banda']['max']} pal) · "
            f"estructura: {r['estructura']['modo']}"
        ))
        for u in plan["unidades"]:
            self.stdout.write(
                f"  U{u['orden']} {u['nombre'][:48]:48} · {len(u['lecciones'])} lecc · "
                f"{u['palabras']} pal · págs {u['paginas'][0]}-{u['paginas'][1]} · [{u['categoria']}]"
            )
            for lec in u["lecciones"]:
                self.stdout.write(f"      - {lec['nombre'][:60]:60} {lec['palabras_fuente']:>5} pal"
                                  + (f" · densidad {lec['densidad']}" if lec.get("densidad") else "")
                                  + (f" · {len(lec['figuras'])} fig" if lec.get("figuras") else ""))

        out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Plan escrito: {out}"))
        self.stdout.write("Revisá/editá el plan y luego: python manage.py generate_course --from-plan " + str(out))

    def _figuras(self, plan, source, out: Path, codigo: str, opts) -> None:
        """Figuras del libro → ``figuras`` por lección (ubicadas por párrafo)."""
        from content_pipeline.services.plan_figures import attach_figures_to_plan

        client = model = None
        if opts["describir_figuras"]:
            from content_pipeline.llm.client import LLMClient, draft_model
            if LLMClient.is_available():
                model = opts["describe_model"] or draft_model()
                client = LLMClient(model=model)
            else:
                self.stderr.write(self.style.WARNING("--describir-figuras requiere ANTHROPIC_API_KEY; se omite."))
        self.stdout.write("Extrayendo figuras del libro…")
        resumen = attach_figures_to_plan(plan, source, out.parent / f"figuras_{codigo}", rel_to=out.parent,
                                         client=client, model=model)
        plan["resumen"]["figuras"] = resumen
        self.stdout.write(
            f"  {resumen['asignadas']} figuras en {resumen['lecciones_con_figuras']} lecciones · "
            f"mapeo {resumen['mapeo']} · {len(resumen['sin_leccion'])} sin lección · "
            f"descartadas {resumen['descartadas']} → {resumen['carpeta']}")
        if client is not None:
            self.stdout.write(f"  IA ({model}): ~US${round(client.meter.cost_usd, 4)} · {client.meter.calls} llamadas")
        if resumen["visuales_sin_figuras"]:
            self.stderr.write(self.style.WARNING(
                "  Lecciones gráficas sin figuras: " + "; ".join(resumen["visuales_sin_figuras"][:8])))
