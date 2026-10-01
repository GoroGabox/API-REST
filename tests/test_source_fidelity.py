"""Fidelidad a la fuente: reconstrucción de párrafos, stubs marcados y bloqueo de importación."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from content_pipeline.extractors.structured_extractor import Line
from content_pipeline.processors.outline import lines_to_paras
from content_pipeline.processors.validators import import_blockers, validate_generated_course


def L(text, y0, page=1, size=10.0, h=12.0):
    """Línea de PDF: alto 12pt; interlineado normal = y0 consecutivos cada 13pt."""
    return Line(page=page, text=text, size=size, bold=False, y0=y0, page_h=800.0, y1=y0 + h)


class ParagraphReconstructionTests(SimpleTestCase):
    def test_wrapped_lines_join_into_one_sentence(self):
        paras = lines_to_paras([L("Mide y muestra el", 100), L("valor de la rapidez", 113), L("instantánea del vehículo.", 126)])
        self.assertEqual([p.text for p in paras], ["Mide y muestra el valor de la rapidez instantánea del vehículo."])

    def test_soft_hyphen_rejoins_word(self):
        paras = lines_to_paras([L("es un error llamar a los accidentes impredeci­", 100), L("bles por naturaleza.", 113)])
        self.assertEqual(paras[0].text, "es un error llamar a los accidentes impredecibles por naturaleza.")

    def test_real_hyphen_is_kept(self):
        paras = lines_to_paras([L("factores físico-", 100), L("síquicos del conductor.", 113)])
        self.assertEqual(paras[0].text, "factores físico-síquicos del conductor.")

    def test_sentence_continues_across_page_break(self):
        paras = lines_to_paras([L("La presión se ajusta cuando", 700, page=5), L("los neumáticos están fríos.", 60, page=6)])
        self.assertEqual(len(paras), 1)
        self.assertEqual((paras[0].page, paras[0].page_end), (5, 6))

    def test_new_paragraph_after_sentence_and_vertical_gap(self):
        paras = lines_to_paras([L("Primera idea completa.", 100), L("Segunda idea separada.", 130)])
        self.assertEqual(len(paras), 2)

    def test_bullets_are_separate_paragraphs(self):
        paras = lines_to_paras([L("Considera lo siguiente:", 100), L("• Revisa los frenos", 113), L("• Revisa las luces", 126)])
        self.assertEqual(len(paras), 3)

    def test_chapter_opener_and_running_header_are_dropped(self):
        paras = lines_to_paras([
            L("CAPÍTULO", 40), L("los siniestros de tránsito", 60),
            L("Antes de comenzar, conviene saberlo.", 100),
            L("los siniestros de tránsito", 20, page=2),          # encabezado de página
            L("Otra idea en la página siguiente.", 100, page=2),
        ], titulo="Los siniestros de tránsito")
        texts = [p.text for p in paras]
        self.assertNotIn("CAPÍTULO", " ".join(texts))
        self.assertFalse(any(t.lower() == "los siniestros de tránsito" for t in texts))
        self.assertEqual(len(texts), 2)

    def test_figure_labels_moved_out_of_a_cut_word(self):
        paras = lines_to_paras([
            L("para avisar que temporal­", 700, page=3),
            L("Testigo luces", 40, page=4, size=7.0), L("neblineras", 50, page=4, size=7.0),
            L("mente se está obstruyendo la circulación.", 300, page=4),
        ])
        self.assertEqual(paras[0].text, "para avisar que temporalmente se está obstruyendo la circulación.")
        self.assertIn("Testigo luces", " ".join(p.text for p in paras[1:]))

    def test_no_false_join_when_continuation_is_far(self):
        paras = lines_to_paras([
            L("llevar el cinturón de segu­", 700, page=3),
            L("Siempre debes conducir en una postura", 40, page=4),
            L("adecuada; no reclinar el asiento.", 53, page=4),
        ])
        self.assertNotIn("seguadecuada", " ".join(p.text for p in paras))

    def test_docx_paragraphs_stay_separate(self):
        a = Line(page=1, text="Primer párrafo del autor", size=0, bold=False, y0=0, page_h=0)
        b = Line(page=1, text="Segundo párrafo", size=0, bold=False, y0=0, page_h=0)
        self.assertEqual(len(lines_to_paras([a, b])), 2)


class _FailingLLM:
    meter = None

    def complete_meta(self, **kw):
        raise RuntimeError("429 rate limit")

    def complete(self, **kw):
        raise RuntimeError("429 rate limit")


def _plan():
    return {"curso": {"nombre": "Curso B", "codigo": "B"}, "unidades": [{
        "orden": 1, "nombre": "Normas", "categoria": "General", "paginas": [10, 12],
        "lecciones": [{"id": "L-x", "nombre": "Velocidad urbana", "paginas": [10, 11],
                       "palabras_objetivo": 900, "texto": "El límite urbano es 50 km/h."}]}]}


class StubHandlingTests(SimpleTestCase):
    def test_failed_redaction_yields_flagged_stub_instead_of_crashing(self):
        from content_pipeline.processors.llm_lesson_writer import generate_lessons_from_plan
        out = list(generate_lessons_from_plan(_plan(), source_name="Libro B", client=_FailingLLM(), model="x"))
        lec = next(l for l in out if l["tipo"] == "texto")
        self.assertTrue(lec["fuente_debil"])
        self.assertIn("Libro B, páginas 10-11.", lec["contenido"])

    def test_validation_reports_stub_and_bad_quiz(self):
        from content_pipeline.processors.llm_lesson_writer import generate_lessons_from_plan
        lessons = list(generate_lessons_from_plan(_plan(), source_name="Libro B", client=_FailingLLM(), model="x"))
        for l in lessons:
            l.pop("_source_text", None)
            if l["tipo"] == "quiz":
                l["contenido"] = {"questions": [{"question": "?", "options": ["a", "b"], "correct_index": 5}]}
        manifest = {"curso": {"codigo": "B"}, "unidades": [{"orden": 1, "nombre": "Normas"}]}
        v = validate_generated_course(manifest, lessons)
        self.assertEqual(len(v["stubs"]), 2)          # la lección y el quiz (sin preguntas válidas)
        self.assertTrue(any("correct_index" in e for e in v["errores"]))

    def test_missing_sections_is_an_error_for_redacted_lessons(self):
        manifest = {"unidades": [{"orden": 1}]}
        lessons = [{"unidad_orden": 1, "posicion": 1, "nombre": "X", "tipo": "texto", "contenido": "# X\n\n## Objetivo\nalgo",
                    "fuentes": [{"fuente_nombre": "L", "pagina_inicio": 1, "pagina_fin": 2}]}]
        v = validate_generated_course(manifest, lessons)
        self.assertTrue(any("faltan secciones" in e for e in v["errores"]))


def _good_course(**extra):
    from content_pipeline.processors.llm_lesson_writer import _REQUIRED_SECTIONS
    body = "# Velocidad\n\n" + "\n\n".join(f"{s}\ntexto" for s in _REQUIRED_SECTIONS) + "\n\n## Fuente\nLibro, páginas 10-11."
    fuentes = [{"fuente_nombre": "Libro", "pagina_inicio": 10, "pagina_fin": 11}]
    data = {
        "manifest": {"curso": {"nombre": "Curso T", "codigo": "TFID", "costo": 1000},
                     "unidades": [{"orden": 1, "nombre": "Normas", "categoria": "General", "temas": ["Velocidad"]}]},
        "lessons": [
            {"unidad_orden": 1, "unidad_nombre": "Normas", "categoria": "General", "posicion": 1, "tipo": "texto",
             "nombre": "Velocidad", "tema_regulatorio": "Velocidad", "descripcion": "d", "duracion_min": 15,
             "contenido": body, "fuentes": fuentes},
            {"unidad_orden": 1, "unidad_nombre": "Normas", "categoria": "General", "posicion": 2, "tipo": "quiz",
             "nombre": "Evaluación", "tema_regulatorio": "Evaluación", "descripcion": "d", "duracion_min": 25,
             "contenido": {"questions": [{"question": "?", "options": ["a", "b", "c", "d"], "correct_index": 0}]},
             "fuentes": fuentes},
        ],
    }
    data.update(extra)
    return data


class ImportGateTests(SimpleTestCase):
    def test_clean_course_has_no_blockers(self):
        self.assertEqual(import_blockers(_good_course())[0], [])

    def test_judge_criticals_block(self):
        data = _good_course(auditoria={"juez": {"criticas": [{"leccion": "U1 · Velocidad", "score": 0.4}]}})
        bloqueos, _ = import_blockers(data)
        self.assertTrue(any("fidelidad crítica" in b for b in bloqueos))

    def test_import_course_refuses_without_forzar(self):
        data = _good_course()
        data["lessons"][0]["fuente_debil"] = True
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "c.json"
            f.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(CommandError):
                call_command("import_course", "--file", str(f), stdout=open(Path(tmp) / "o.txt", "w", encoding="utf-8"),
                             stderr=open(Path(tmp) / "e.txt", "w", encoding="utf-8"))


