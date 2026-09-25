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
from content_pipeline.licenses import orientation_for
from content_pipeline.extractors.pdf_text_extractor import extract_pdf_pages
from content_pipeline.llm.client import LLMClient, default_model, draft_model
from content_pipeline.processors.faithfulness import (
    ANCHOR_MIN,
    JUDGE_MIN,
    audit_lessons,
    build_lesson_sources,
    judge_lessons_llm,
)
from content_pipeline.processors.generic_lesson_generator import generate_lessons_generic
from content_pipeline.processors.llm_lesson_writer import generate_lessons_llm
from content_pipeline.processors.manifest_from_content import (
    build_manifest_from_content,
    build_manifest_from_content_llm,
)
from content_pipeline.processors.llm_mapper import map_topics_llm
from content_pipeline.processors.map_topics import coverage_alert, map_topics_to_segments
from content_pipeline.processors.segment_book import segment_pages
from content_pipeline.services.course_planning import (
    extract_temario_topics,
    mapped_source_text,
    resolve_max_lecciones,
    truncation_notes,
    validate_topics_present,
)


class Command(BaseCommand):
    help = (
        "Genera un curso (manifest + lecciones) desde el CONTENIDO del libro y lo "
        "escribe a un JSON, SIN tocar la base de datos. La estructura sale del libro "
        "completo; --temario (opcional) se usa solo para VALIDAR que sus temas estén "
        "en el curso. Correr en local con ANTHROPIC_API_KEY; luego subir con `import_course`."
    )

    def add_arguments(self, parser):
        parser.add_argument("--temario", default=None,
                            help="PDF del temario (opcional). Ya NO define la estructura: se usa como "
                                 "checklist para validar que sus temas aparezcan en el curso generado.")
        parser.add_argument("--contenido", required=True, help="PDF del contenido fuente (material).")
        parser.add_argument("--nombre", required=True, help="Nombre del curso.")
        parser.add_argument("--codigo", required=True, help="Código del curso (<=10 chars).")
        parser.add_argument("--costo", type=int, required=True, help="Costo del curso (> 0).")
        parser.add_argument("--out", required=True, help="Ruta del JSON de salida ({manifest, lessons}).")
        parser.add_argument("--is-profesional", action="store_true", help="Marca el curso como profesional.")
        parser.add_argument("--max-lecciones", type=int, default=None,
                            help="Techo de lecciones. Por defecto se auto-dimensiona al tamaño del libro.")
        parser.add_argument("--unidades", type=int, default=None,
                            help="Número de unidades a inferir del contenido (por defecto lo decide el generador).")
        parser.add_argument("--idioma", default="es")
        parser.add_argument("--modo", choices=["draft", "final"], default="draft",
                            help="draft usa el modelo barato para las lecciones; final usa el modelo principal.")
        parser.add_argument("--source-name", default=None, help="Nombre de la fuente citada en las lecciones.")
        parser.add_argument("--orientacion", default=None,
                            help="Texto para orientar el curso a una licencia. Si se omite, se deriva del "
                                 "código (A2/A4/A5…). Útil porque varias licencias salen del mismo libro.")
        parser.add_argument("--judge", action="store_true",
                            help="Juez LLM de fidelidad al final (opt-in, con costo extra).")
        parser.add_argument("--judge-min", type=float, default=JUDGE_MIN,
                            help=f"Umbral de fidelidad del juez: bajo esto la lección es CRÍTICA "
                                 f"(def. {JUDGE_MIN}).")
        parser.add_argument("--anchor-min", type=float, default=ANCHOR_MIN,
                            help=f"Umbral de anclaje lexical: bajo esto se marca la lección "
                                 f"(def. {ANCHOR_MIN}).")

    def handle(self, *args, **opts):
        contenido_path = Path(opts["contenido"])
        if not contenido_path.exists():
            raise CommandError(f"No existe el PDF: {contenido_path}")
        temario_path = Path(opts["temario"]) if opts.get("temario") else None
        if temario_path is not None and not temario_path.exists():
            raise CommandError(f"No existe el PDF: {temario_path}")

        costo = int(opts["costo"])
        if costo <= 0:
            raise CommandError("El costo es obligatorio y debe ser mayor a 0.")
        codigo = opts["codigo"].strip().upper()
        if len(codigo) > 10:
            raise CommandError("El código no puede superar 10 caracteres.")
        nombre = opts["nombre"].strip()
        # None => auto-dimensionar al tamaño del libro (resolve_max_lecciones).
        max_lecciones = opts.get("max_lecciones")
        n_unidades = opts.get("unidades")
        modo = opts["modo"]
        source_name = opts["source_name"] or f"Contenido: {nombre}"
        orientacion = orientation_for(codigo, opts.get("orientacion"))
        if orientacion:
            self.stdout.write(f"Orientación ({codigo}): {orientacion}")

        use_llm = LLMClient.is_available()
        client = LLMClient() if use_llm else None
        final_model = default_model()
        lesson_model = final_model if modo == "final" else draft_model()

        if use_llm:
            self.stdout.write(f"Modo IA · estructura: {final_model} · lecciones: {lesson_model}")
        else:
            self.stdout.write("IA no configurada (sin ANTHROPIC_API_KEY): generación heurística extractiva.")

        # El contenido es la fuente de la ESTRUCTURA (libro completo) y de las
        # lecciones. El temario, si se pasa, solo se valida al final.
        self.stdout.write("Extrayendo el contenido fuente…")
        content_pages = extract_pdf_pages(contenido_path)
        self.stdout.write(f"{len(content_pages)} páginas de contenido extraídas.")

        segments = segment_pages(content_pages)
        self.stdout.write(f"{len(segments)} segmentos generados.")

        # Dimensionar el curso al tamaño del libro (evita recortes hardcodeados).
        max_lec, origen = resolve_max_lecciones(len(segments), max_lecciones)
        self.stdout.write(
            f"Objetivo: hasta {max_lec} lecciones "
            f"({'fijado por el operador' if origen == 'operador' else 'auto-dimensionado'})."
        )
        for nota in truncation_notes(len(segments), max_lec, origen):
            self.stderr.write(self.style.WARNING(f"⚠ {nota}"))

        # Estructura desde el LIBRO COMPLETO (siempre), no desde el temario.
        manifest = self._manifest_desde_contenido(
            segments, nombre=nombre, codigo=codigo,
            is_profesional=opts["is_profesional"], max_lecciones=max_lec,
            n_unidades=n_unidades, use_llm=use_llm, client=client, final_model=final_model,
            orientacion=orientacion,
        )

        # El costo lo fija el operador (obligatorio, > 0): sobrescribe el
        # placeholder del generador de estructura.
        manifest["curso"]["costo"] = costo

        n_units = len(manifest["unidades"])
        n_topics = sum(len(u["temas"]) for u in manifest["unidades"])
        self.stdout.write(f"Estructura del libro: {n_units} unidades · {n_topics} temas.")

        # Procedencia por IA (llamada aparte, robusta); si falla → mapeo lexical.
        provenance = None
        if use_llm:
            self.stdout.write("Anclando cada tema a sus segmentos (IA)…")
            try:
                provenance = map_topics_llm(manifest, segments, client=client, model=final_model)
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(self.style.WARNING(f"No se pudo anclar por IA ({exc}); uso mapeo lexical."))
                provenance = None
        mappings = map_topics_to_segments(manifest, segments, provenance=provenance)
        cobertura = coverage_alert(mappings)
        n_prov = sum(
            1 for m in mappings
            if (m.get("matched_segments") or [{}])[0].get("reason", "").startswith("Procedencia")
        )
        self.stdout.write(
            f"Mapeo: {len(cobertura['solid'])}/{cobertura['total']} temas con fuente sólida "
            f"({n_prov} por procedencia del generador)."
        )
        debiles = list(cobertura["weak"]) + list(cobertura["uncovered"])
        if debiles:
            self.stderr.write(self.style.WARNING(
                f"⚠ {len(debiles)} tema(s) sin fuente sólida (su lección se degrada al extractivo, "
                f"sin inventar; revisar el mapeo):"
            ))
            for label in debiles:
                self.stderr.write(self.style.WARNING(f"    - {label}"))

        # Temario como checklist: validar que sus temas estén en el curso.
        if temario_path is not None:
            self.stdout.write("Leyendo el temario para validación…")
            temario_pages = extract_pdf_pages(temario_path)
            expected = extract_temario_topics(
                temario_pages, nombre=nombre, codigo=codigo,
                is_profesional=opts["is_profesional"], use_llm=use_llm,
                client=client, model=final_model,
            )
            corpus = mapped_source_text(mappings, segments)
            validacion = validate_topics_present(expected, manifest, corpus_text=corpus)
            self.stdout.write(
                f"Temario: {len(validacion['present'])}/{validacion['expected']} "
                f"temas presentes en el curso."
            )
            faltantes = validacion["missing"]
            if faltantes:
                self.stderr.write(self.style.WARNING(
                    f"⚠ {len(faltantes)} tema(s) del temario NO representados en el curso del libro:"
                ))
                for label in faltantes:
                    self.stderr.write(self.style.WARNING(f"    - {label}"))

        if use_llm:
            self.stdout.write("Redactando lecciones con IA…")
            lessons = []
            for index, lesson in enumerate(
                generate_lessons_llm(
                    manifest, segments, mappings,
                    source_name=source_name, orientacion=orientacion,
                    client=client, model=lesson_model,
                ),
                start=1,
            ):
                lessons.append(lesson)
                self.stdout.write(f"  [{index}] {lesson.get('nombre', '')}")
        else:
            self.stdout.write("Redactando lecciones…")
            lessons = generate_lessons_generic(manifest, segments, mappings, source_name=source_name)

        # Auditoría de fidelidad (reporte, no bloquea): cifras sin respaldo +
        # bajo anclaje al libro. Se imprime y se adjunta al JSON.
        audit = audit_lessons(lessons, segments, mappings, anchor_min=opts["anchor_min"])
        self.stdout.write(
            f"Fidelidad: {audit['auditadas']} auditadas · "
            f"{len(audit['figuras'])} con cifras sin respaldo · "
            f"{len(audit['anclaje_bajo'])} con bajo anclaje."
        )
        if audit["figuras"]:
            self.stderr.write(self.style.WARNING("⚠ Cifras que NO aparecen en la fuente (posible dato inventado):"))
            for f in audit["figuras"]:
                self.stderr.write(self.style.WARNING(f"    - {f['leccion']}: {', '.join(f['cifras'])}"))
        if audit["anclaje_bajo"]:
            self.stderr.write(self.style.WARNING(f"⚠ Bajo anclaje al libro (< {audit['anchor_min']}):"))
            for a in audit["anclaje_bajo"]:
                self.stderr.write(self.style.WARNING(f"    - {a['leccion']}: {a['anclaje']}"))

        auditoria = dict(audit)
        # Juez LLM de fidelidad (opt-in): pasada extra con costo.
        if opts.get("judge") and use_llm and client is not None:
            pairs = build_lesson_sources(lessons, segments, mappings)
            self.stdout.write(f"Juez LLM de fidelidad · {len(pairs)} lecciones…")
            juez = judge_lessons_llm(pairs, client=client, model=lesson_model, judge_min=opts["judge_min"])
            self.stdout.write(
                f"  Fidelidad promedio: {juez['promedio']} · "
                f"críticas (< {juez['judge_min']}): {len(juez['criticas'])} · "
                f"con reparos menores: {len(juez['con_reparos'])}"
            )
            if juez["criticas"]:
                self.stderr.write(self.style.ERROR("  CRÍTICAS (fidelidad baja):"))
                for item in juez["criticas"]:
                    self.stderr.write(self.style.ERROR(f"    - {item['leccion']} · fidelidad {item['score']}"))
                    for claim in item["claims"]:
                        self.stderr.write(f"        · {claim}")
            if juez["con_reparos"]:
                self.stderr.write(self.style.WARNING("  Con reparos menores:"))
                for item in juez["con_reparos"]:
                    self.stderr.write(self.style.WARNING(f"    - {item['leccion']} · fidelidad {item['score']}"))
                    for claim in item["claims"]:
                        self.stderr.write(f"        · {claim}")
            auditoria["juez"] = juez

        payload = {"manifest": manifest, "lessons": lessons, "auditoria": auditoria}
        if provenance:  # para que audit_course_json --contenido reuse el mismo mapeo
            payload["provenance"] = provenance
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

    # -- construcción del manifest ------------------------------------------

    def _manifest_desde_contenido(self, segments, *, nombre, codigo, is_profesional,
                                  max_lecciones, n_unidades, use_llm, client, final_model,
                                  orientacion=None):
        """Estructura inferida del propio contenido (IA con fallback heurístico)."""
        self.stdout.write("Infiriendo la estructura desde el libro completo…")
        if use_llm:
            try:
                return build_manifest_from_content_llm(
                    segments, nombre=nombre, codigo=codigo,
                    is_profesional=is_profesional, max_lecciones=max_lecciones,
                    n_unidades=n_unidades, orientacion=orientacion,
                    client=client, model=final_model,
                )
            except Exception as exc:  # noqa: BLE001 — degradar a heurística
                self.stderr.write(f"La IA no pudo inferir la estructura ({exc}); uso heurística.")
        return build_manifest_from_content(
            segments, nombre=nombre, codigo=codigo,
            is_profesional=is_profesional, max_lecciones=max_lecciones, n_unidades=n_unidades,
        )
