"""Cuestionario en Word (.docx) → JSON de ejercicios por unidad (offline, usa LLM).

Cada «CAPITULO N» del documento es la unidad N del curso. El LLM estructura
preguntas y variantes; la validación determinista exige que enunciado y
alternativas estén en el documento (``cuestionario_docx_parser``). La categoría
la pone ``ejercicio_classifier`` (Haiku), una vez por grupo de variantes.

    python manage.py build_ejercicios_docx --docx "CUESTIONARIO ADOLFO.docx" \
        --curso B --fuente "Cuestionario Adolfo B" --out out/ejercicios/adolfo_b.json

No toca la BD; se importa con ``import_ejercicios --curso B --file …``.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_pipeline.llm.client import LLMClient, default_model, draft_model
from content_pipeline.processors import cuestionario_docx_parser as parser_docx
from content_pipeline.processors import ejercicio_classifier


class Command(BaseCommand):
    help = "Parsea un cuestionario .docx (capítulo = unidad) y clasifica por categoría → JSON."

    def add_arguments(self, parser):
        parser.add_argument("--docx", required=True)
        parser.add_argument("--curso", required=True, help="Código del curso (p. ej. B).")
        parser.add_argument("--fuente", default="", help="Nombre de la fuente (default: nombre del archivo).")
        parser.add_argument("--grupo", default="", help="Prefijo de grupo (default: derivado de --fuente).")
        parser.add_argument("--out", required=True)
        parser.add_argument("--capitulos", default="", help="Solo estos capítulos, p. ej. 1,3.")
        parser.add_argument("--model", default="", help="Modelo para estructurar (default: principal).")
        parser.add_argument(
            "--recategorizar", action="store_true",
            help="No estructura de nuevo: relee --out y solo vuelve a clasificar las categorías.",
        )

    def handle(self, *args, **opts):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
        path = Path(opts["docx"])
        if not path.exists():
            raise CommandError(f"No existe: {path}")
        if not LLMClient.is_available():
            raise CommandError("Falta ANTHROPIC_API_KEY: la estructuración del cuestionario usa IA.")

        if opts["recategorizar"]:
            return self._recategorizar(Path(opts["out"]))
        fuente = opts["fuente"] or path.stem
        prefijo = opts["grupo"] or parser_docx.norm(fuente).replace(" ", "-")[:30] or "docx"
        capitulos = parser_docx.dividir_capitulos(parser_docx.leer_lineas(str(path)))
        if opts["capitulos"]:
            pedidos = {int(x) for x in opts["capitulos"].split(",") if x.strip()}
            capitulos = {k: v for k, v in capitulos.items() if k in pedidos}
        if not capitulos:
            raise CommandError("No encontré «CAPITULO N» en el documento.")

        cli = LLMClient(model=opts["model"] or default_model())
        ejercicios, reporte = [], []
        for cap, lineas in sorted(capitulos.items()):
            if not lineas:
                self.stdout.write(f"Capítulo {cap}: vacío, se omite.")
                reporte.append({"capitulo": cap, "preguntas": 0, "ejercicios": 0, "diferidas": 0})
                continue
            self.stdout.write(f"Capítulo {cap}: {len(lineas)} líneas …")
            res = parser_docx.estructurar_capitulo(cap, lineas, client=cli)
            items = parser_docx.a_ejercicios(res, prefijo_grupo=prefijo, fuente=fuente)
            faltan = parser_docx.numeros_faltantes(res)
            self.stdout.write(
                f"  preguntas={len(res.preguntas)} · ejercicios={sum(not e['diferir'] for e in items)}"
                f" · diferidas={len(res.diferidas)}" + (f" · números faltantes: {faltan}" if faltan else ""))
            for d in res.diferidas:
                self.stdout.write(self.style.WARNING(f"    diferida {d.get('numero')}: {'; '.join(d['errores'])[:160]}"))
            reporte.append({"capitulo": cap, "preguntas": len(res.preguntas),
                            "ejercicios": sum(not e["diferir"] for e in items),
                            "diferidas": len(res.diferidas), "numeros_faltantes": faltan})
            ejercicios.extend(items)

        clasificador = self._categorizar(ejercicios)
        costo = cli.meter.cost_usd + clasificador.meter.cost_usd
        out = Path(opts["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "fuente": fuente,
            "curso": opts["curso"],
            "taxonomia": ejercicio_classifier.TAXONOMIA,
            "reporte": reporte,
            "costo_usd": round(costo, 4),
            "ejercicios": ejercicios,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(
            f"Escrito {out} · {sum(not e['diferir'] for e in ejercicios)} ejercicios · "
            f"{sum(e['diferir'] for e in ejercicios)} diferidos · ~USD {costo:.3f}"))

    def _categorizar(self, ejercicios) -> LLMClient:
        """Categoría por grupo de variantes (todas comparten tema), con Haiku."""
        representantes, vistos = [], set()
        for e in ejercicios:
            if not e["diferir"] and e["grupo"] not in vistos:
                vistos.add(e["grupo"])
                representantes.append({**e, "numero": e["grupo"]})
        clasificador = LLMClient(model=draft_model())
        self.stdout.write(f"Clasificando {len(representantes)} grupos por categoría …")
        cats = ejercicio_classifier.clasificar(representantes, client=clasificador)
        for e in ejercicios:
            e["categoria"] = cats.get(e["grupo"]) if not e["diferir"] else None
        for cat, n in Counter(e["categoria"] for e in ejercicios if e.get("categoria")).most_common():
            self.stdout.write(f"  {n:4}  {cat}")
        return clasificador

    def _recategorizar(self, out: Path):
        data = json.loads(out.read_text(encoding="utf-8"))
        clasificador = self._categorizar(data["ejercicios"])
        data["costo_usd"] = round(data.get("costo_usd", 0) + clasificador.meter.cost_usd, 4)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Categorías actualizadas en {out}."))
