"""Audita la fidelidad de un curso YA generado (JSON) contra su fuente.

Dos modos:

  * TRIAGE (por defecto, sin el libro): usa el ``fragmento_resumen`` embebido en
    cada lección como proxy. Solo guard de cifras (una cifra ausente del extracto
    es candidata a revisión). No calcula anclaje (contra ~200 chars sería ruido).

  * COMPLETO (``--contenido <pdf>``): re-extrae y segmenta el libro, remapea
    contra el manifest del JSON y corre la auditoría real (cifras + anclaje
    lexical contra los segmentos completos). Requiere auditar UN solo JSON.

Ambos son REPORTE. Hay falsos positivos: confirmar cada hallazgo en el libro
(p. ej. con la Brújula del Libro).

Uso::

    python manage.py audit_course_json                                  # triage a2 y a4
    python manage.py audit_course_json curso_a2_final.json
    python manage.py audit_course_json curso_a2_final.json --contenido "Libro A2.pdf"
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_pipeline.processors.faithfulness import (
    ANCHOR_MIN,
    JUDGE_MIN,
    audit_generated_json,
    audit_lessons,
    build_lesson_sources,
    build_lesson_sources_from_fuentes,
    judge_lessons_llm,
)

_DEFAULTS = ["curso_a2_final.json", "curso_a4_final.json"]


class Command(BaseCommand):
    help = "Audita la fidelidad (cifras sin respaldo; anclaje con --contenido) de cursos ya generados."

    def add_arguments(self, parser):
        parser.add_argument("files", nargs="*", help="JSON(s) a auditar. Por defecto: a2 y a4.")
        parser.add_argument("--contenido", default=None,
                            help="PDF del libro fuente: activa la auditoría COMPLETA (un solo JSON).")
        parser.add_argument("--llm", action="store_true",
                            help="Juez LLM de fidelidad (opt-in, con costo). Con --contenido usa la "
                                 "fuente completa; sin él, el extracto embebido (más débil).")
        parser.add_argument("--limit", type=int, default=None,
                            help="Máximo de lecciones a juzgar con LLM (control de costo).")
        parser.add_argument("--anchor-min", type=float, default=ANCHOR_MIN,
                            help=f"Umbral de anclaje lexical (solo modo COMPLETO; def. {ANCHOR_MIN}).")
        parser.add_argument("--judge-min", type=float, default=JUDGE_MIN,
                            help=f"Umbral de fidelidad del juez: bajo esto es CRÍTICA (def. {JUDGE_MIN}).")

    def handle(self, *args, **opts):
        for stream in (sys.stdout, sys.stderr):  # consola Windows: forzar utf-8
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass

        files = opts["files"] or _DEFAULTS
        pdf = opts["contenido"]
        if pdf and len(files) != 1:
            raise CommandError("--contenido audita UN solo JSON; pasa exactamente un archivo.")

        total_fig = total_anc = 0
        for name in files:
            path = Path(name)
            if not path.exists():
                self.stderr.write(self.style.WARNING(f"No existe: {path} (omitido)"))
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except json.JSONDecodeError as exc:
                raise CommandError(f"JSON inválido en {path}: {exc}")

            lessons = data.get("lessons") or []
            curso = (data.get("manifest") or {}).get("curso") or {}
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"=== {path.name} · {curso.get('nombre', '')} ({curso.get('codigo', '')}) ==="
            ))

            segments = mappings = None
            if pdf:
                segments, mappings = self._segmentar(data, Path(pdf))
                audit = audit_lessons(lessons, segments, mappings, anchor_min=opts["anchor_min"])
                modo = "COMPLETA (contra segmentos del libro)"
            else:
                audit = audit_generated_json(lessons)
                audit.setdefault("anclaje_bajo", None)
                modo = "TRIAGE (extracto embebido; sin anclaje)"

            n_anc = len(audit["anclaje_bajo"]) if audit.get("anclaje_bajo") is not None else "n/d"
            self.stdout.write(
                f"Modo: {modo}\n"
                f"Auditadas: {audit['auditadas']} · cifras sin respaldo: {len(audit['figuras'])} · "
                f"bajo anclaje: {n_anc}"
            )

            if audit["figuras"]:
                self.stdout.write(self.style.WARNING("\n  Cifras que NO aparecen en la fuente:"))
                for f in audit["figuras"]:
                    self.stdout.write(f"    - {f['leccion']}: {', '.join(f['cifras'])}")
            if audit.get("anclaje_bajo"):
                self.stdout.write(self.style.WARNING(
                    f"\n  Lecciones con bajo anclaje (< {audit.get('anchor_min', ANCHOR_MIN)}):"))
                for a in sorted(audit["anclaje_bajo"], key=lambda x: x["anclaje"]):
                    self.stdout.write(f"    - {a['leccion']}: anclaje {a['anclaje']}")
            if not audit["figuras"] and not audit.get("anclaje_bajo"):
                self.stdout.write(self.style.SUCCESS("  Sin hallazgos (cifras/anclaje)."))

            if opts["llm"]:
                self._juez_llm(lessons, segments, mappings, limit=opts["limit"], judge_min=opts["judge_min"])

            total_fig += len(audit["figuras"])
            if audit.get("anclaje_bajo") is not None:
                total_anc += len(audit["anclaje_bajo"])

        self.stdout.write("")
        self.stdout.write(self.style.HTTP_INFO(
            f"TOTAL · {total_fig} lección(es) con cifras sin respaldo"
            + (f" · {total_anc} con bajo anclaje" if pdf else "")
            + ". Confirma cada hallazgo contra el libro."
        ))

    def _juez_llm(self, lessons, segments, mappings, *, limit, judge_min):
        from content_pipeline.llm.client import LLMClient, draft_model

        if not LLMClient.is_available():
            self.stderr.write(self.style.ERROR(
                "  --llm requiere ANTHROPIC_API_KEY (y el paquete anthropic). Omitido."))
            return
        if segments is not None:
            pairs = build_lesson_sources(lessons, segments, mappings)
            fuente = "fuente completa"
        else:
            pairs = build_lesson_sources_from_fuentes(lessons)
            fuente = "extracto embebido (débil; usa --contenido para fuente completa)"
        if not pairs:
            self.stdout.write("  Juez LLM: sin lecciones con fuente para juzgar.")
            return
        client = LLMClient(model=draft_model())
        self.stdout.write(self.style.HTTP_INFO(
            f"\n  Juez LLM de fidelidad ({len(pairs) if not limit else min(limit, len(pairs))} lecciones · {fuente})…"))
        res = judge_lessons_llm(pairs, client=client, model=draft_model(), judge_min=judge_min, limit=limit)
        self.stdout.write(
            f"  Evaluadas: {res['evaluadas']} · fidelidad promedio: {res['promedio']} · "
            f"críticas (< {res['judge_min']}): {len(res['criticas'])} · "
            f"con reparos menores: {len(res['con_reparos'])} · ~US${round(client.meter.cost_usd, 4)}")
        if res["criticas"]:
            self.stdout.write(self.style.ERROR("    CRÍTICAS (fidelidad baja):"))
            for item in res["criticas"]:
                self.stdout.write(self.style.ERROR(f"      - {item['leccion']} · fidelidad {item['score']}"))
                for claim in item["claims"]:
                    self.stdout.write(f"          · {claim}")
        if res["con_reparos"]:
            self.stdout.write(self.style.WARNING("    Con reparos menores:"))
            for item in res["con_reparos"]:
                self.stdout.write(self.style.WARNING(f"      - {item['leccion']} · fidelidad {item['score']}"))
                for claim in item["claims"]:
                    self.stdout.write(f"          · {claim}")
        if res["errores"]:
            self.stdout.write(self.style.ERROR(f"  Errores del juez: {len(res['errores'])}"))

    def _segmentar(self, data, pdf: Path):
        if not pdf.exists():
            raise CommandError(f"No existe el PDF: {pdf}")
        from content_pipeline.extractors.pdf_text_extractor import extract_pdf_pages
        from content_pipeline.processors.map_topics import map_topics_to_segments
        from content_pipeline.processors.segment_book import segment_pages

        manifest = data.get("manifest") or {}
        self.stdout.write("  Re-extrayendo y segmentando el libro…")
        segments = segment_pages(extract_pdf_pages(pdf))
        # Reusa la procedencia del JSON (mismo mapeo que en generación); si no la
        # trae (curso viejo), cae al mapeo lexical.
        mappings = map_topics_to_segments(manifest, segments, provenance=data.get("provenance"))
        return segments, mappings
