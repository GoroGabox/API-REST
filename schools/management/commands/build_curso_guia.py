"""Guías del alumno en Word (una por módulo) → JSON de curso + JSON de ejercicios.

Conversión fiel (``content_pipeline.processors.guia_docx``): el texto de la guía no
se reescribe. La IA (Haiku) solo agrega las secciones núcleo que la guía no trae
(Resumen siempre; Objetivo y Puntos clave si faltan), auditadas por cifras, y
clasifica los ejercicios por categoría. No toca la BD.

    python manage.py build_curso_guia --dir "C:/…/A4" --codigo A4 --nombre "Curso Profesional Clase A4" \
        --precio 6990 --is-profesional --out out/a4/a4_guia.json --ejercicios out/a4/ejercicios_a4.json

Después: ``clasificar_ejercicios --lecciones`` (categoría por lección), ``publish_media
--paginas-libro --contenido libro_pro.pdf``, ``import_course`` e ``import_ejercicios --curso``.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_pipeline.processors import guia_docx as G


class Command(BaseCommand):
    help = "Convierte guías del alumno (.docx por módulo) en un curso importable y su banco de ejercicios."

    def add_arguments(self, parser):
        parser.add_argument("--dir", required=True, help="Carpeta con un .docx por módulo (<COD>_<n>_….docx).")
        parser.add_argument("--codigo", required=True)
        parser.add_argument("--nombre", required=True)
        parser.add_argument("--precio", type=int, required=True, help="Precio del plan de 7 días (> 0).")
        parser.add_argument("--is-profesional", action="store_true")
        parser.add_argument("--guia", default="", help="Nombre de la guía en «## Fuente» (default: «Guía del alumno <COD>»).")
        parser.add_argument("--out", required=True, help="JSON del curso.")
        parser.add_argument("--ejercicios", required=True, help="JSON de ejercicios para import_ejercicios.")
        parser.add_argument("--sin-ia", action="store_true",
                            help="No completa secciones faltantes ni clasifica (queda con errores de validación).")

    def handle(self, *args, **o):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
        if o["precio"] <= 0:
            raise CommandError("--precio debe ser mayor que 0.")
        archivos = G.ordenar_archivos(o["dir"])
        if not archivos:
            raise CommandError(f"No hay .docx con nombre <COD>_<n>_… en {o['dir']}.")
        codigo = o["codigo"]
        modulos = []
        for n, path in archivos:
            mod = G.parsear_modulo(path, n, codigo)
            modulos.append(mod)
            nq = sum(len(l.preguntas) for l in mod.lecciones)
            self.stdout.write(
                f"Módulo {n} · {mod.titulo}: {len(mod.lecciones)} lecciones · "
                f"objetivos {sum(bool(l.objetivos) for l in mod.lecciones)} · "
                f"ideas clave {sum(bool(l.ideas) for l in mod.lecciones)} · preguntas {nq} "
                f"(respuestas {len(mod.respuestas)}) · figuras "
                f"{sum(len(l.figuras) for l in mod.lecciones) + len(mod.anexo_figuras)} · "
                f"recuadros {dict(mod.rotulos) or '-'}")
            for aviso in mod.avisos:
                self.stdout.write(self.style.WARNING(f"   {aviso}"))

        curso, ejercicios, figuras = G.armar_curso(
            modulos, nombre=o["nombre"], codigo=codigo, precio=o["precio"],
            profesional=o["is_profesional"], guia=o["guia"] or f"Guía del alumno {codigo}")

        out = Path(o["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        carpeta = out.parent / f"figuras_{codigo}"
        carpeta.mkdir(exist_ok=True)
        for f in figuras:
            (out.parent / f["archivo"]).write_bytes(f["data"])
        self.stdout.write(f"Figuras: {len(figuras)} en {carpeta}")

        generado = []
        if not o["sin_ia"]:
            from content_pipeline.llm.client import LLMClient, LLMError, draft_model
            from content_pipeline.processors import ejercicio_classifier
            if not LLMClient.is_available():
                raise CommandError("Falta ANTHROPIC_API_KEY (o usa --sin-ia).")
            cli = LLMClient(model=draft_model())
            self.stdout.write("Secciones que la guía no trae:")
            try:
                generado = G.completar_secciones(curso, cli, log=self.stdout.write)
                validos = [e for e in ejercicios if not e["diferir"]]
                cats = ejercicio_classifier.clasificar(
                    [{**e, "numero": e["grupo"]} for e in validos], client=cli)
            except LLMError as exc:
                raise CommandError(f"La IA no respondió ({exc}); no se escribió nada.") from exc
            for e in ejercicios:
                e["categoria"] = cats.get(e["grupo"]) if not e["diferir"] else None
            self.stdout.write(f"Costo IA ~USD {cli.meter.cost_usd:.3f}")
        curso["auditoria"]["generado"] = generado
        curso["auditoria"]["guia"] = {"archivos": [p.name for _, p in archivos]}

        from content_pipeline.processors.validators import validate_generated_course
        val = validate_generated_course(curso["manifest"], curso["lessons"])
        curso["validacion"] = val
        out.write_text(json.dumps(curso, ensure_ascii=False, indent=2), encoding="utf-8")
        Path(o["ejercicios"]).write_text(json.dumps({
            "fuente": f"Guías del alumno {codigo}", "curso": codigo, "ejercicios": ejercicios,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        dist = Counter(e.get("categoria") for e in ejercicios if not e["diferir"])
        self.stdout.write("Ejercicios por categoría: " + ", ".join(f"{k}={v}" for k, v in dist.most_common()))
        modos = Counter(g["modo"] for g in generado)
        self.stdout.write(
            f"Lecciones: {len(curso['lessons'])} · ejercicios: {len(ejercicios)} "
            f"({sum(e['diferir'] for e in ejercicios)} diferidos) · secciones generadas: {dict(modos) or 0}")
        for e in val["errores"]:
            self.stdout.write(self.style.ERROR(f"  ERROR {e}"))
        for a in val["advertencias"]:
            self.stdout.write(self.style.WARNING(f"  aviso {a}"))
        self.stdout.write(self.style.SUCCESS(f"Escrito {out} y {o['ejercicios']}."))
