"""Genera un curso (manifest + lecciones) desde dos PDFs y lo escribe a un JSON.

Pensado para correr en LOCAL con `ANTHROPIC_API_KEY`: hace lo caro/lento
(extracción de PDF + redacción con IA) sin tocar la base de datos, y deja un
único archivo `{manifest, lessons}`. Ese JSON se lleva al entorno desplegado y se
sube con `import_course` (upsert trivial, sin IA ni PDFs en producción).

Reproduce el mismo pipeline que el endpoint de streaming
(`content_pipeline.services.course_generator`) pero en vez de persistir/streamear
escribe el resultado a disco. Sin API key cae al mismo fallback extractivo.
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_pipeline.exporters.json_exporter import write_json
from content_pipeline.extractors.pdf_text_extractor import extract_pdf_pages
from content_pipeline.llm.client import LLMClient, default_model, draft_model
from content_pipeline.processors.generic_lesson_generator import generate_lessons_generic
from content_pipeline.processors.llm_lesson_writer import generate_lessons_llm
from content_pipeline.processors.manifest_builder import build_manifest_from_temario
from content_pipeline.processors.manifest_llm import build_manifest_from_temario_llm
from content_pipeline.processors.map_topics import map_topics_to_segments
from content_pipeline.processors.segment_book import segment_pages


class Command(BaseCommand):
    help = (
        "Genera un curso (manifest + lecciones) desde dos PDFs y lo escribe a un "
        "JSON, SIN tocar la base de datos. Correr en local con ANTHROPIC_API_KEY; "
        "luego subir el JSON a la BD desplegada con `import_course`."
    )

    def add_arguments(self, parser):
        parser.add_argument("--temario", required=True, help="PDF del temario (estructura del curso).")
        parser.add_argument("--contenido", required=True, help="PDF del contenido fuente (material).")
        parser.add_argument("--nombre", required=True, help="Nombre del curso.")
        parser.add_argument("--codigo", required=True, help="Código del curso (<=10 chars).")
        parser.add_argument("--costo", type=int, required=True, help="Costo del curso (> 0).")
        parser.add_argument("--out", required=True, help="Ruta del JSON de salida ({manifest, lessons}).")
        parser.add_argument("--is-profesional", action="store_true", help="Marca el curso como profesional.")
        parser.add_argument("--max-lecciones", type=int, default=20)
        parser.add_argument("--idioma", default="es")
        parser.add_argument("--modo", choices=["draft", "final"], default="draft",
                            help="draft usa el modelo barato para las lecciones; final usa el modelo principal.")
        parser.add_argument("--source-name", default=None, help="Nombre de la fuente citada en las lecciones.")

    def handle(self, *args, **opts):
        temario_path = Path(opts["temario"])
        contenido_path = Path(opts["contenido"])
        for p in (temario_path, contenido_path):
            if not p.exists():
                raise CommandError(f"No existe el PDF: {p}")

        costo = int(opts["costo"])
        if costo <= 0:
            raise CommandError("El costo es obligatorio y debe ser mayor a 0.")
        codigo = opts["codigo"].strip().upper()
        if len(codigo) > 10:
            raise CommandError("El código no puede superar 10 caracteres.")
        nombre = opts["nombre"].strip()
        max_lecciones = max(1, min(int(opts["max_lecciones"]), 100))
        modo = opts["modo"]
        source_name = opts["source_name"] or f"Contenido: {nombre}"

        use_llm = LLMClient.is_available()
        client = LLMClient() if use_llm else None
        final_model = default_model()
        lesson_model = final_model if modo == "final" else draft_model()

        if use_llm:
            self.stdout.write(f"Modo IA · temario: {final_model} · lecciones: {lesson_model}")
        else:
            self.stdout.write("IA no configurada (sin ANTHROPIC_API_KEY): generación heurística extractiva.")

        self.stdout.write("Leyendo el temario…")
        temario_pages = extract_pdf_pages(temario_path)

        manifest = None
        if use_llm:
            self.stdout.write("Interpretando el temario con IA…")
            try:
                manifest = build_manifest_from_temario_llm(
                    temario_pages,
                    nombre=nombre,
                    codigo=codigo,
                    is_profesional=opts["is_profesional"],
                    max_lecciones=max_lecciones,
                    client=client,
                    model=final_model,
                )
            except Exception as exc:  # noqa: BLE001 — degradar a heurística como el stream
                self.stderr.write(f"La IA no pudo interpretar el temario ({exc}); uso heurística.")
                manifest = None
        if manifest is None:
            manifest = build_manifest_from_temario(
                temario_pages,
                nombre=nombre,
                codigo=codigo,
                is_profesional=opts["is_profesional"],
                max_lecciones=max_lecciones,
            )

        # El costo lo fija el operador (obligatorio, > 0): sobrescribe el
        # placeholder del manifest builder.
        manifest["curso"]["costo"] = costo

        n_units = len(manifest["unidades"])
        n_topics = sum(len(u["temas"]) for u in manifest["unidades"])
        self.stdout.write(f"Temario interpretado: {n_units} unidades · {n_topics} temas.")

        self.stdout.write("Extrayendo el contenido fuente…")
        content_pages = extract_pdf_pages(contenido_path)
        self.stdout.write(f"{len(content_pages)} páginas de contenido extraídas.")

        segments = segment_pages(content_pages)
        self.stdout.write(f"{len(segments)} segmentos generados.")

        mappings = map_topics_to_segments(manifest, segments)
        covered = sum(1 for m in mappings if m.get("matched_segments"))
        self.stdout.write(f"{covered}/{len(mappings)} temas con fuente encontrada.")

        if use_llm:
            self.stdout.write("Redactando lecciones con IA…")
            lessons = []
            for index, lesson in enumerate(
                generate_lessons_llm(
                    manifest, segments, mappings,
                    source_name=source_name, client=client, model=lesson_model,
                ),
                start=1,
            ):
                lessons.append(lesson)
                self.stdout.write(f"  [{index}] {lesson.get('nombre', '')}")
        else:
            self.stdout.write("Redactando lecciones…")
            lessons = generate_lessons_generic(manifest, segments, mappings, source_name=source_name)

        payload = {"manifest": manifest, "lessons": lessons}
        if use_llm and client is not None:
            payload["ia"] = client.meter.as_dict()

        out_path = Path(opts["out"])
        write_json(out_path, payload)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Curso generado: {len(lessons)} lecciones → {out_path}"))
        if use_llm and client is not None:
            m = client.meter
            self.stdout.write(f"IA: {m.calls} llamadas · ~US${m.cost_usd:.3f}")
        self.stdout.write("")
        self.stdout.write("Siguiente paso (en el entorno desplegado, apuntando a la BD de prod):")
        self.stdout.write(f"  python manage.py import_course --file {out_path} --dry-run")
        self.stdout.write(f"  python manage.py import_course --file {out_path}")
