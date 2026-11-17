"""Curso redactado como HTML interactivo (Cursos-Licencias-C-y-D) → JSON de curso + ejercicios.

Conversión fiel (``content_pipeline.processors.curso_html``): el texto no se
reescribe. La IA (Haiku) solo agrega Objetivo, Puntos clave y Resumen (el HTML no
los trae), auditados por cifras, y clasifica los ejercicios. No toca la BD.

    python manage.py build_curso_html --html "out/cd/zip/Curso-Licencia-Clase-C.html" --codigo C \
        --nombre "Curso Clase C" --precio 7000 --fuente "Curso Licencia Clase C (AutoTest)" \
        --libro "Libro del Nuevo Conductor Motociclistas (CONASET)" \
        --paginas "6-10,11-22,23-60,61-67,68-73,74-111,112-118,119-156,157-166,167-178" \
        --out out/c/c_html.json --ejercicios out/c/ejercicios_c.json --publicar-imagenes s3

``--publicar-imagenes`` sube las imágenes de las preguntas (señales) y deja su URL en
``imagen``; sin él quedan en ``imagen_archivo`` (local) y ``import_ejercicios`` usa el
placeholder. Después: ``clasificar_ejercicios --lecciones``, ``publish_media``,
``import_course``, ``import_ejercicios --curso`` y ``presentacion_curso``.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_pipeline.processors import curso_html as H
from content_pipeline.processors import guia_docx as G


def _paginas(texto: str) -> dict[int, tuple[int, int]]:
    out = {}
    for i, tramo in enumerate([t for t in (texto or "").split(",") if t.strip()], start=1):
        a, _, b = tramo.strip().partition("-")
        out[i] = (int(a), int(b or a))
    return out


class Command(BaseCommand):
    help = "Convierte un curso HTML interactivo en un curso importable y su banco de ejercicios."

    def add_arguments(self, parser):
        parser.add_argument("--html", required=True)
        parser.add_argument("--codigo", required=True)
        parser.add_argument("--nombre", required=True)
        parser.add_argument("--precio", type=int, required=True, help="Precio del plan de 7 días (> 0).")
        parser.add_argument("--is-profesional", action="store_true")
        parser.add_argument("--fuente", required=True, help="Nombre del material en «## Fuente».")
        parser.add_argument("--libro", default="", help="Libro CONASET de referencia (con --paginas).")
        parser.add_argument("--paginas", default="",
                            help="Páginas del libro por unidad, en orden: «6-10,11-22,…».")
        parser.add_argument("--titulo-extras", default="Otras maquinarias",
                            help="Nombre de la unidad que junta los módulos «extra».")
        parser.add_argument("--fuente-banco", default="", help="Fuente de las preguntas propias.")
        parser.add_argument("--out", required=True)
        parser.add_argument("--ejercicios", required=True)
        parser.add_argument("--publicar-imagenes", default="", help="Storage (local|s3) para las imágenes del banco.")
        parser.add_argument("--sin-ia", action="store_true")

    def handle(self, *args, **o):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
        if o["precio"] <= 0:
            raise CommandError("--precio debe ser mayor que 0.")
        codigo = o["codigo"]
        datos = H.extraer(o["html"])
        self.stdout.write(
            "Módulos: " + ", ".join(f"{m['id']}({m['group']})" for m in datos["modules"])
            + f" · láminas {len(datos['svg'])} · señales {len(datos['signs'])} · preguntas {len(datos['q'])}")
        curso, ejercicios, figuras, mod_unidad = H.armar_curso(
            datos, nombre=o["nombre"], codigo=codigo, precio=o["precio"], profesional=o["is_profesional"],
            fuente=o["fuente"], libro=o["libro"], paginas_unidad=_paginas(o["paginas"]),
            titulo_extras=o["titulo_extras"], fuente_banco=o["fuente_banco"])
        unidades = curso["manifest"]["unidades"]
        if o["paginas"] and len(_paginas(o["paginas"])) != len(unidades):
            raise CommandError(f"--paginas trae {len(_paginas(o['paginas']))} tramos y hay {len(unidades)} unidades.")
        for u in unidades:
            ls = [l for l in curso["lessons"] if l["unidad_orden"] == u["orden"]]
            nq = sum(1 for e in ejercicios if e["unidad_orden"] == u["orden"] and not e["diferir"])
            self.stdout.write(f"Unidad {u['orden']} · {u['nombre']}: {len(ls) - 1} lecciones + quiz · "
                              f"figuras {sum(len(l.get('recursos') or []) for l in ls)} · preguntas {nq}")

        out = Path(o["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        (out.parent / f"figuras_{codigo}").mkdir(exist_ok=True)
        for f in figuras:
            (out.parent / f["archivo"]).write_bytes(f["data"])
        self.stdout.write(f"Figuras: {len(figuras)} en {out.parent / f'figuras_{codigo}'}")

        if o["publicar_imagenes"]:
            from content_pipeline.media.storage import get_storage
            st = get_storage(o["publicar_imagenes"], kind="images")
            subidas = {}
            for e in ejercicios:
                arch = e.get("imagen_archivo")
                if not arch:
                    continue
                if arch not in subidas:
                    nombre = f"{codigo}/{Path(arch).name}"
                    subidas[arch] = st.url(nombre) if st.exists(nombre) else st.save(
                        nombre, (out.parent / arch).read_bytes())
                e["imagen"] = subidas[arch]
            self.stdout.write(f"Imágenes del banco publicadas: {len(subidas)}")

        generado = []
        if not o["sin_ia"]:
            from content_pipeline.llm.client import LLMClient, LLMError, draft_model
            from content_pipeline.processors import ejercicio_classifier
            if not LLMClient.is_available():
                raise CommandError("Falta ANTHROPIC_API_KEY (o usa --sin-ia).")
            cli = LLMClient(model=draft_model())
            self.stdout.write("Secciones que el HTML no trae:")
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
        curso["auditoria"]["html"] = {"archivo": Path(o["html"]).name, "modulos": mod_unidad}

        from content_pipeline.processors.validators import validate_generated_course
        val = validate_generated_course(curso["manifest"], curso["lessons"])
        curso["validacion"] = val
        out.write_text(json.dumps(curso, ensure_ascii=False, indent=2), encoding="utf-8")
        Path(o["ejercicios"]).write_text(json.dumps({
            "fuente": o["fuente"], "curso": codigo, "ejercicios": ejercicios,
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
