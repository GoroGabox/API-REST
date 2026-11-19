"""«Material de estudio» de un curso profesional (ZIP con material + práctica de test) → curso + banco.

Conversión fiel (``content_pipeline.processors.material_docx``): el texto no se reescribe.
La IA (Haiku) solo agrega Objetivo, Puntos clave y Resumen por lección (auditados por
cifras) y clasifica los ejercicios. Escribe la presentación de la unidad 0 en
``content_pipeline/presentaciones/<COD>.md`` (desde la «Introducción al curso»). No toca la BD.

    python manage.py build_curso_material --zip "C:/…/Curso_A4_Clase_A4.zip" --codigo A4 \
        --nombre "Curso Profesional Clase A4" --precio 6990 --is-profesional --libro libro_pro.pdf \
        --out out/prof/A4/a4.json --ejercicios out/prof/A4/ejercicios_a4.json --publicar-imagenes s3

Después: ``clasificar_ejercicios --lecciones``, ``publish_media --paginas-libro --contenido
libro_pro.pdf``, ``import_course``, ``import_ejercicios --curso`` y ``presentacion_curso``.
"""
from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_pipeline.processors import guia_docx as G
from content_pipeline.processors import material_docx as M
from content_pipeline.processors import presentacion as P


def _buscar(carpeta: Path, patron: str) -> Path:
    hallados = sorted(carpeta.rglob(patron))
    if not hallados:
        raise CommandError(f"No se encontró «{patron}» en el ZIP.")
    return hallados[0]


class Command(BaseCommand):
    help = "Convierte el «Material de estudio» (7 módulos) y su práctica de test en un curso importable."

    def add_arguments(self, parser):
        parser.add_argument("--zip", required=True, help="ZIP con *Material_de_estudio*.docx y *CON_RESPUESTAS.docx.")
        parser.add_argument("--codigo", required=True)
        parser.add_argument("--nombre", required=True)
        parser.add_argument("--precio", type=int, required=True)
        parser.add_argument("--is-profesional", action="store_true")
        parser.add_argument("--guia", default="", help="Nombre del material en «## Fuente».")
        parser.add_argument("--libro", default="", help="PDF del libro para alinear las páginas de cada lección.")
        parser.add_argument("--libro-desde", type=int, default=13, help="Primera página de contenido del libro.")
        parser.add_argument("--out", required=True)
        parser.add_argument("--ejercicios", required=True)
        parser.add_argument("--presentacion", default="",
                            help="Dónde escribir la presentación (default: content_pipeline/presentaciones/<COD>.md).")
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
        guia = o["guia"] or f"{o['nombre']} — Material de estudio"
        out = Path(o["out"])
        out.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as tmp:
            zipfile.ZipFile(o["zip"]).extractall(tmp)
            material = _buscar(Path(tmp), "*Material_de_estudio*.docx")
            practica = _buscar(Path(tmp), "*CON_RESPUESTAS*.docx")
            mat = M.parsear_material(material, codigo)
            ejercicios, declarados, imagenes_banco = M.parsear_banco(
                practica, codigo=codigo, fuente_guia=f"Práctica de test {codigo}")

        # Banco: único por módulo vs el declarado.
        por_mod = Counter(e["unidad_orden"] for e in ejercicios)
        for n in sorted(set(declarados) | set(por_mod)):
            ok = por_mod.get(n, 0) == declarados.get(n)
            msg = f"Banco módulo {n}: {por_mod.get(n, 0)} únicas / {declarados.get(n, '?')} declaradas"
            self.stdout.write(msg if ok else self.style.WARNING(msg + " (revisar)"))
        for e in [e for e in ejercicios if e["diferir"]]:
            self.stdout.write(self.style.WARNING(f"   diferida m{e['unidad_orden']}: {e['pregunta'][:70]} — {e['motivo']}"))

        # Páginas del libro por afinidad.
        paginas = {}
        if o["libro"]:
            claves, textos = [], []
            for mod in mat.modulos:
                for pos, l in enumerate(mod.lecciones, start=1):
                    if not l.anexo:
                        claves.append((mod.numero, pos))
                        textos.append(l.nombre + "\n" + "\n".join(l.desarrollo))
            res = M.paginas_libro(textos, o["libro"], desde=o["libro_desde"])
            paginas = dict(zip(claves, res))
            bajas = [(k, v) for k, v in paginas.items() if v[2] < 0.5]
            self.stdout.write(f"Páginas del libro: {len(paginas)} lecciones; {len(bajas)} con cobertura < 0,5 (revisar):")
            for (m_, p_), (a, b, c) in bajas:
                self.stdout.write(f"   U{m_}.{p_} págs. {a}-{b} cobertura {c}")

        curso, figuras = M.armar_curso(mat, nombre=o["nombre"], codigo=codigo, precio=o["precio"],
                                       profesional=o["is_profesional"], guia=guia, ejercicios=ejercicios,
                                       paginas=paginas)
        for mod in mat.modulos:
            ls = [l for l in curso["lessons"] if l["unidad_orden"] == mod.numero]
            self.stdout.write(f"Módulo {mod.numero} · {mod.titulo}: {len(ls) - 1} lecciones + quiz · figuras "
                              f"{sum(len(l.get('recursos') or []) for l in ls)} · preguntas {por_mod.get(mod.numero, 0)} · "
                              f"recuadros {dict(mod.rotulos)}")

        (out.parent / f"figuras_{codigo}").mkdir(exist_ok=True)
        for f in figuras + imagenes_banco:
            (out.parent / f["archivo"]).write_bytes(f["data"])
        self.stdout.write(f"Figuras: {len(figuras)} en lecciones + {len(imagenes_banco)} del banco")

        if o["publicar_imagenes"]:
            from content_pipeline.media.storage import get_storage
            st = get_storage(o["publicar_imagenes"], kind="images")
            subidas = {}
            for e in ejercicios:
                arch = e.get("imagen_archivo")
                if arch and arch not in subidas:
                    nombre = f"{codigo}/{Path(arch).name}"
                    subidas[arch] = st.url(nombre) if st.exists(nombre) else st.save(
                        nombre, (out.parent / arch).read_bytes())
                if arch:
                    e["imagen"] = subidas[arch]
            self.stdout.write(f"Imágenes del banco publicadas: {len(subidas)}")

        pres = Path(o["presentacion"]) if o["presentacion"] else P.CARPETA / f"{codigo}.md"
        pres.write_text(M.presentacion_md(mat, codigo=codigo, guia=guia), encoding="utf-8")
        self.stdout.write(f"Presentación (unidad 0): {pres}")

        generado = []
        if not o["sin_ia"]:
            from content_pipeline.llm.client import LLMClient, LLMError, draft_model
            from content_pipeline.processors import ejercicio_classifier
            if not LLMClient.is_available():
                raise CommandError("Falta ANTHROPIC_API_KEY (o usa --sin-ia).")
            cli = LLMClient(model=draft_model())
            self.stdout.write("Secciones que el material no trae por lección:")
            try:
                generado = G.completar_secciones(curso, cli, log=lambda *_: None)
                validos = [e for e in ejercicios if not e["diferir"]]
                cats = ejercicio_classifier.clasificar([{**e, "numero": e["grupo"]} for e in validos], client=cli)
            except LLMError as exc:
                raise CommandError(f"La IA no respondió ({exc}); no se escribió nada.") from exc
            for e in ejercicios:
                e["categoria"] = cats.get(e["grupo"]) if not e["diferir"] else None
            self.stdout.write(f"  {Counter((g['seccion'], g['modo']) for g in generado)}")
            self.stdout.write(f"Costo IA ~USD {cli.meter.cost_usd:.3f}")
        curso["auditoria"]["generado"] = generado
        curso["auditoria"]["material"] = {
            "zip": Path(o["zip"]).name, "banco_declarado": declarados, "banco_unico": dict(por_mod),
            "paginas_bajas": [f"U{k[0]}.{k[1]}" for k, v in paginas.items() if v[2] < 0.5],
        }

        from content_pipeline.processors.validators import validate_generated_course
        val = validate_generated_course(curso["manifest"], curso["lessons"])
        curso["validacion"] = val
        out.write_text(json.dumps(curso, ensure_ascii=False, indent=2), encoding="utf-8")
        Path(o["ejercicios"]).write_text(json.dumps({
            "fuente": guia, "curso": codigo, "ejercicios": ejercicios,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        self.stdout.write(f"Lecciones: {len(curso['lessons'])} · ejercicios: {len(ejercicios)} "
                          f"({sum(e['diferir'] for e in ejercicios)} diferidos) · validación: "
                          f"{len(val['errores'])} errores, {len(val['advertencias'])} advertencias")
        for e in val["errores"][:30]:
            self.stdout.write(self.style.ERROR(f"  ERROR {e}"))
        for a in val["advertencias"][:15]:
            self.stdout.write(self.style.WARNING(f"  aviso {a}"))
        self.stdout.write(self.style.SUCCESS(f"Escrito {out} y {o['ejercicios']}."))
