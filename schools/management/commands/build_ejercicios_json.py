"""Parsea el cuestionario PDF + clasifica por tema (LLM) → JSON de ejercicios.

Paso OFFLINE del pipeline de ejercicios (análogo a `generate_course`): usa el
LLM para clasificar, por lo que se corre en local. El JSON resultante se importa
luego con `import_ejercicios` en el entorno desplegado (sin LLM).

Uso::

    python manage.py build_ejercicios_json --pdf "Cuestionario ... CLASE B.pdf" --out ejercicios_clase_b.json
    python manage.py build_ejercicios_json --pdf "..." --out "..." --no-llm   # sin clasificar (categoría fallback)

Las preguntas que dependen de imagen o sin datos suficientes quedan marcadas
``diferir=true`` (Fase 3); el importador las omite.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_pipeline.processors.cuestionario_parser import parse_cuestionario
from content_pipeline.processors import ejercicio_classifier


class Command(BaseCommand):
    help = "Parsea el cuestionario PDF y clasifica los ejercicios por tema (LLM) → JSON."

    def add_arguments(self, parser):
        parser.add_argument("--pdf", required=True, help="Ruta al PDF del cuestionario.")
        parser.add_argument("--out", required=True, help="Ruta de salida del JSON.")
        parser.add_argument(
            "--no-llm", action="store_true",
            help="No clasifica con LLM; usa la categoría fallback para todos.",
        )

    def handle(self, *args, **opts):
        # La consola de Windows (cp1252) no puede imprimir acentos ni '~USD';
        # fuerza utf-8 en la salida para no crashear con texto en español.
        import sys
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass

        pdf = Path(opts["pdf"])
        if not pdf.exists():
            raise CommandError(f"No existe el PDF: {pdf}")

        self.stdout.write(f"Parseando {pdf.name} ...")
        ejercicios, stats = parse_cuestionario(pdf)
        d = stats.as_dict()
        importables = [e for e in ejercicios if not e["diferir"]]
        diferidos = len(ejercicios) - len(importables)
        self.stdout.write(
            f"  total={d['total']} single={d['single']} multi={d['multi']} "
            f"imagen={d['con_imagen']} · importables={len(importables)} diferidos={diferidos}"
        )

        if opts["no_llm"]:
            self.stdout.write("Clasificación: OMITIDA (--no-llm) → categoría fallback.")
            cats = {e["numero"]: ejercicio_classifier.CATEGORIA_FALLBACK for e in importables}
        else:
            self.stdout.write(f"Clasificando {len(importables)} ejercicios con LLM ...")
            from content_pipeline.llm.client import LLMClient, draft_model
            cli = LLMClient(model=draft_model())
            cats = ejercicio_classifier.clasificar(importables, client=cli)
            self.stdout.write(
                f"  costo~USD {round(cli.meter.cost_usd, 4)} "
                f"(in={cli.meter.input_tokens} out={cli.meter.output_tokens})"
            )

        # Adjunta categoría a cada ejercicio (los diferidos quedan sin ella).
        for e in ejercicios:
            e["categoria"] = cats.get(e["numero"]) if not e["diferir"] else None

        from collections import Counter
        dist = Counter(e["categoria"] for e in ejercicios if e.get("categoria"))
        self.stdout.write("Distribución por categoría:")
        for cat, n in dist.most_common():
            self.stdout.write(f"  {n:3}  {cat}")

        out = Path(opts["out"])
        payload = {
            "fuente": pdf.name,
            "taxonomia": ejercicio_classifier.TAXONOMIA,
            "stats": d,
            "ejercicios": ejercicios,
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(
            f"Escrito {out} · {len(importables)} importables · {diferidos} diferidos."
        ))
