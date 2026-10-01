"""Aplica la revisión humana (Brújula del Libro) a un plan o a un curso generado.

Solo aplica observaciones con ``estado = "aceptada"``. Sin IA; determinista.

    # estructura → plan.json (antes de generate_course --from-plan)
    python manage.py apply_review --review out/review_b.json --plan out/plan_b.json --out out/plan_b_rev.json
    # contenido → JSON de generate_course (antes de import_course)
    python manage.py apply_review --review out/review_b.json --course out/b.json --out out/b_rev.json --dry-run

``--review`` acepta varios archivos (se fusionan por id de observación). Las
correcciones de contenido son reemplazos acotados: si el texto a corregir no
aparece exactamente una vez, se reporta como conflicto y no se aplica.
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_pipeline.exporters.json_exporter import read_json, write_json
from content_pipeline.review.apply import apply_review, merge_reviews


class Command(BaseCommand):
    help = "Aplica las observaciones aceptadas de review.json (Brújula) a un plan o a un curso generado."

    def add_arguments(self, parser):
        parser.add_argument("--review", nargs="+", required=True, help="Uno o más review.json exportados por la Brújula.")
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--plan", help="plan.json (aplica la fase 'estructura').")
        target.add_argument("--course", help="JSON de generate_course (aplica la fase 'contenido').")
        parser.add_argument("--out", help="Archivo de salida (requerido salvo --dry-run).")
        parser.add_argument("--dry-run", action="store_true", help="No escribe; solo reporta qué aplicaría.")

    def handle(self, *args, **opts):
        if not opts["dry_run"] and not opts.get("out"):
            raise CommandError("Falta --out (o usá --dry-run).")
        reviews = []
        for path in opts["review"]:
            p = Path(path)
            if not p.exists():
                raise CommandError(f"No existe la revisión: {p}")
            reviews.append(read_json(p))
        items = merge_reviews(reviews)

        fase = "estructura" if opts.get("plan") else "contenido"
        src = Path(opts.get("plan") or opts.get("course"))
        if not src.exists():
            raise CommandError(f"No existe: {src}")
        data = read_json(src)
        if fase == "estructura" and not isinstance(data.get("unidades"), list):
            raise CommandError("--plan debe ser un plan.json (salida de plan_course).")
        if fase == "contenido" and not isinstance(data.get("lessons"), list):
            raise CommandError("--course debe ser un JSON de generate_course ({manifest, lessons}).")

        res = apply_review(data, items, fase=fase)
        n_fase = sum(1 for i in items if i.get("fase") == fase)
        self.stdout.write(f"Revisión · fase {fase} · {n_fase} observaciones de {len(reviews)} archivo(s)")
        for a in res["aplicadas"]:
            self.stdout.write(self.style.SUCCESS(f"  + {a['resumen']} ({a['autor']})"))
        for c in res["conflictos"]:
            self.stderr.write(self.style.WARNING(f"  x {c['resumen']} ({c['autor']}): {c['motivo']}"))
        self.stdout.write(
            f"Aplicadas: {len(res['aplicadas'])} · conflictos: {len(res['conflictos'])} · "
            f"ignoradas (pendientes/rechazadas/comentarios): {res['ignoradas']}"
        )
        if opts["dry_run"]:
            self.stdout.write("DRY-RUN: no se escribió nada.")
            return
        out = Path(opts["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        write_json(out, res["data"])
        self.stdout.write(self.style.SUCCESS(f"-> {out}"))
