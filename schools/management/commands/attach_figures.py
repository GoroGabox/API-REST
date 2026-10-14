"""Agrega a un plan YA REVISADO las figuras y las páginas de otro documento (p. ej. el libro en PDF).

Caso: el plan salió de la guía en Word (sin imágenes ni páginas), ya pasó por la Brújula
y no se quiere regenerar; las figuras y la paginación están en el libro oficial. Este
comando NO cambia unidades, lecciones, textos ni ids del plan.

- **Figuras**: capítulo del libro → unidad del plan por NOMBRE; dentro de la unidad, una IA
  (Haiku) ve cada figura (pie / descripción / texto vecino del libro) y los párrafos de las
  lecciones, y elige lección y párrafo, o ninguna. Si el libro trae un capítulo de señales
  sin unidad en el plan: las que el texto nombra se insertan en el texto, el resto queda
  como galería (`en_texto: false`) en la lección de señales, y todas se listan en la unidad
  «Anexo: Señales de tránsito» (`--sin-anexo` la omite).
- **Páginas**: cada lección se alinea con las páginas del libro que cubre (sin IA:
  vocabulario compartido + orden del libro) → `paginas` reales en vez de "1-1"
  (`--sin-paginas` lo omite).

Uso::

    python manage.py attach_figures --plan out/plan_b_rev.json --contenido libro.pdf --out out/plan_b_fig.json --describir-figuras
    # solo las páginas del libro (sin IA; conserva las figuras ya asignadas)
    python manage.py attach_figures --plan out/plan_b_fig.json --contenido libro.pdf --out out/plan_b_fig.json --solo-paginas
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_pipeline.services.plan_figures import attach_figures_cross
from content_pipeline.services.plan_pages import AFINIDAD_BAJA, align_plan_pages


def _huella(plan: dict) -> list:
    """Lo que este comando no puede cambiar: lecciones (sin anexos), sus ids y sus textos."""
    return [(lec.get("id"), lec.get("texto")) for u in plan["unidades"]
            for lec in u.get("lecciones") or [] if not lec.get("anexo")]


class Command(BaseCommand):
    help = "Agrega a un plan revisado las figuras y las páginas de otro documento. No regenera el plan."

    def add_arguments(self, parser):
        parser.add_argument("--plan", required=True, help="plan.json revisado (no se modifica su estructura ni su texto).")
        parser.add_argument("--contenido", required=True, help="Documento con las figuras y la paginación (PDF del libro).")
        parser.add_argument("--out", required=True, help="plan.json de salida.")
        parser.add_argument("--describir-figuras", action="store_true",
                            help="IA con visión (Haiku): pie + alt por figura y descarte de decorativas. Mejora el mapeo.")
        parser.add_argument("--describe-model", default=None, help="Modelo de visión (def. Haiku).")
        parser.add_argument("--map-model", default=None, help="Modelo que ubica cada figura (def. Haiku).")
        parser.add_argument("--sin-anexo", action="store_true", help="No crear la unidad «Anexo: Señales de tránsito».")
        parser.add_argument("--sin-paginas", action="store_true", help="No alinear las lecciones con las páginas del libro.")
        parser.add_argument("--solo-paginas", action="store_true",
                            help="Solo alinear páginas (sin IA); conserva las figuras que el plan ya tenga.")
        parser.add_argument("--source-name", default=None, help="Nombre de la fuente a citar (def. nombre del archivo).")
        parser.add_argument("--limit", type=int, default=0, help="Procesar solo las primeras N figuras (prueba).")

    def handle(self, *args, **opts):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
        if opts["solo_paginas"] and opts["sin_paginas"]:
            raise CommandError("--solo-paginas y --sin-paginas se excluyen.")
        plan_path, src, out = Path(opts["plan"]), Path(opts["contenido"]), Path(opts["out"])
        for path in (plan_path, src):
            if not path.exists():
                raise CommandError(f"No existe: {path}")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if not isinstance(plan.get("unidades"), list):
            raise CommandError("--plan no parece un plan.json.")
        antes = _huella(plan)
        figuras_antes = json.dumps([lec.get("figuras") for u in plan["unidades"] for lec in u.get("lecciones") or []],
                                   sort_keys=True)
        out.parent.mkdir(parents=True, exist_ok=True)

        if not opts["solo_paginas"]:
            self._figuras(plan, plan_path, src, out, opts)
        if not opts["sin_paginas"]:
            self._paginas(plan, src, opts)

        # Garantía: el plan revisado no cambió (mismas lecciones, mismos textos).
        if antes != _huella(plan):
            raise CommandError("Error interno: el texto o las lecciones del plan cambiaron; no se escribe nada.")
        if opts["solo_paginas"] and figuras_antes != json.dumps(
                [lec.get("figuras") for u in plan["unidades"] for lec in u.get("lecciones") or []], sort_keys=True):
            raise CommandError("Error interno: --solo-paginas cambió las figuras; no se escribe nada.")

        plan.setdefault("resumen", {})["unidades"] = len(plan["unidades"])
        plan["resumen"]["lecciones"] = sum(len(u.get("lecciones") or []) for u in plan["unidades"])
        out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Plan (mismas lecciones y textos) → {out}"))
        self.stdout.write(f"Revisalo: python manage.py build_brujula --plan {out} --out {out.with_suffix('.zip')}")

    # ------------------------------------------------------------------ figuras
    def _figuras(self, plan, plan_path, src, out, opts) -> None:
        from content_pipeline.llm.client import LLMClient, LLMError, draft_model
        if not LLMClient.is_available():
            raise CommandError("Ubicar las figuras necesita ANTHROPIC_API_KEY (o usá --solo-paginas).")
        codigo = str((plan.get("curso") or {}).get("codigo") or "curso").strip().upper()
        map_model = opts["map_model"] or draft_model()
        map_client = LLMClient(model=map_model)
        vision_client = vision_model = None
        if opts["describir_figuras"]:
            vision_model = opts["describe_model"] or draft_model()
            vision_client = LLMClient(model=vision_model)

        self.stdout.write(f"Figuras de {src} → plan {plan_path} (mapeo: {map_model}"
                          + (f", visión: {vision_model}" if vision_client else "") + ")…")
        try:
            resumen = attach_figures_cross(
                plan, src, out.parent / f"figuras_{codigo}", rel_to=out.parent,
                map_client=map_client, map_model=map_model,
                vision_client=vision_client, vision_model=vision_model,
                anexo=not opts["sin_anexo"], limit=opts["limit"])
        except LLMError as exc:
            raise CommandError(
                f"La IA no respondió y no se escribió ningún plan: {exc}\n"
                "Revisá ANTHROPIC_API_KEY en .env (clave válida y con saldo) y volvé a correr; "
                "las descripciones ya hechas se reutilizan.") from exc
        plan.setdefault("resumen", {})["figuras"] = resumen

        self.stdout.write("Capítulos del documento → unidad del plan:")
        for cap, unidad in resumen["capitulos"].items():
            self.stdout.write(f"  {cap[:50]:50} → {unidad or '— (sin unidad: se descarta)'}")
        for u in plan["unidades"]:
            for i, lec in enumerate(u.get("lecciones") or [], start=1):
                figs = lec.get("figuras") or []
                if figs:
                    gal = sum(1 for f in figs if f.get("en_texto") is False)
                    self.stdout.write(f"  {u['orden']}.{i} {lec['nombre'][:55]:55} {len(figs) - gal:>3} en texto"
                                      + (f" · {gal} galería" if gal else ""))
        costo = sum(c.meter.cost_usd for c in (map_client, vision_client) if c is not None)
        self.stdout.write(self.style.SUCCESS(
            f"Figuras: {resumen['extraidas']} extraídas · {resumen['en_texto']} en el texto · "
            f"{resumen['galeria']} en galería · {resumen['anexo']} en el anexo · "
            f"{len(resumen['sin_leccion'])} sin lección · ~US${round(costo, 3)}"))
        self.stdout.write(f"  descartadas: {resumen['descartadas']}")

    # ------------------------------------------------------------------ páginas
    def _paginas(self, plan, src, opts) -> None:
        if src.suffix.lower() != ".pdf":
            self.stderr.write(self.style.WARNING("Las páginas solo se alinean con un PDF; se omite."))
            return
        res = align_plan_pages(plan, src, source_name=opts["source_name"])
        plan.setdefault("resumen", {})["paginas"] = {
            "fuente": res["fuente"], "lecciones": len(res["lecciones"]),
            "afinidad_baja": [f["clave"] for f in res["afinidad_baja"]],
            "unidades_sin_capitulo": res["unidades_sin_capitulo"],
        }
        self.stdout.write(f"Páginas de «{res['fuente']}» por lección (afinidad 0-1):")
        for f in res["lecciones"]:
            marca = "  ← confirmar" if f["afinidad"] < AFINIDAD_BAJA else ""
            self.stdout.write(f"  {f['clave']:>5} {f['leccion'][:52]:52} págs {f['paginas'][0]:>3}-{f['paginas'][1]:<3} "
                              f"{f['afinidad']:.2f}{marca}")
        if res["unidades_sin_capitulo"]:
            self.stderr.write(self.style.WARNING(
                "Unidades sin capítulo en el documento (páginas sin cambio): " + "; ".join(res["unidades_sin_capitulo"])))
        self.stdout.write(self.style.SUCCESS(
            f"Páginas: {len(res['lecciones'])} lecciones alineadas · {len(res['afinidad_baja'])} por confirmar."))
