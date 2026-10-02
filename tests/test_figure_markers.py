"""Figuras en la fase de plan + marcadores {{figura:ID}} en la redacción, auditoría y revisión."""
from __future__ import annotations

import io
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

from django.test import SimpleTestCase

from content_pipeline.llm.client import LLMResponse
from content_pipeline.processors.figure_markers import (
    figure_ids, figures_prompt, marker, sanitize_markers, strip_markers,
)

BODY = ("# Semáforos\n\n## Objetivo\nConocer las luces.\n\n## Desarrollo\nLa luz roja obliga a detenerse. "
        "{{figura:F-aaa}} La luz verde permite avanzar.\n\n{{figura:F-zzz}}\n\n{{figura:F-aaa}}\n\n"
        "## Puntos clave\n- Rojo: detenerse\n\n## Resumen\nRespeta el semáforo.")
FIGS = [{"id": "F-aaa", "pie": "Semáforo en rojo", "alt": "Semáforo", "parrafo": 0, "archivo": "figuras_B/a.png"},
        {"id": "F-bbb", "pie": "Semáforo peatonal", "parrafo": None, "archivo": "figuras_B/b.png"}]


class SanitizeTests(SimpleTestCase):
    def test_unknown_duplicate_inline_and_auto(self):
        out, info = sanitize_markers(BODY, FIGS)
        self.assertEqual(info["usadas"], ["F-aaa"])
        self.assertEqual((info["desconocidas"], info["duplicadas"], info["auto"]), (["F-zzz"], ["F-aaa"], ["F-bbb"]))
        self.assertEqual(figure_ids(out), ["F-aaa", "F-bbb"])
        self.assertIn("\n\n{{figura:F-aaa}}\n\n", out)                      # en su propia línea
        desarrollo = out.split("## Desarrollo")[1].split("## Puntos clave")[0]
        self.assertIn(marker("F-bbb"), desarrollo)                           # la no usada, al final del Desarrollo

    def test_strip_modes(self):
        self.assertNotIn("{{", strip_markers(BODY))
        self.assertIn("[Figura: Semáforo en rojo]", strip_markers(BODY, FIGS, modo="pie"))
        rec = [{"clave": "F-aaa", "meta": {"pie": "Pie meta"}}]
        self.assertIn("[Figura: Pie meta]", strip_markers(BODY, rec, modo="pie"))

    def test_prompt_lists_figures_with_location(self):
        texto = "La luz roja obliga a detenerse siempre.\n\nOtro párrafo."
        txt = figures_prompt(FIGS, texto)
        self.assertIn("{{figura:F-aaa}}", txt)
        self.assertIn("tras el párrafo 1 («La luz roja obliga", txt)
        self.assertIn("en las mismas páginas del libro", txt)
        self.assertEqual(figures_prompt([], texto), "")


class _LLM:
    meter = None

    def __init__(self, body):
        self.body, self.calls = body, []

    def complete_meta(self, **kw):
        self.calls.append(kw)
        if "evaluador" in kw["system"]:
            return LLMResponse(json.dumps({"questions": []}), stop_reason="end_turn")
        return LLMResponse(self.body, stop_reason="end_turn")


def _plan():
    return {"curso": {"nombre": "Curso B", "codigo": "B"}, "unidades": [{
        "orden": 1, "nombre": "Normas", "categoria": "General", "paginas": [10, 12],
        "lecciones": [{"id": "L-x", "nombre": "Semáforos", "paginas": [10, 11], "palabras_objetivo": 900,
                       "texto": "La luz roja obliga a detenerse.\n\nLa luz verde permite avanzar.",
                       "figuras": FIGS}]}]}


class WriterTests(SimpleTestCase):
    def test_plan_figures_reach_prompt_and_lesson_resources(self):
        from content_pipeline.processors.llm_lesson_writer import generate_lessons_from_plan
        llm = _LLM(BODY)
        lessons = list(generate_lessons_from_plan(_plan(), source_name="Libro", client=llm, model="m"))
        texto = lessons[0]
        user = llm.calls[0]["user"]
        self.assertIn("FIGURAS DEL LIBRO", user)
        self.assertIn("{{figura:ID}}", llm.calls[0]["system"])               # regla del prompt
        self.assertIn("{{figura:F-aaa}}", user)
        self.assertEqual(figure_ids(texto["contenido"]), ["F-aaa", "F-bbb"])
        recs = {r["clave"]: r for r in texto["recursos"]}
        self.assertEqual(recs["F-aaa"]["archivo_local"], "figuras_B/a.png")
        self.assertEqual(recs["F-aaa"]["meta"]["pie"], "Semáforo en rojo")
        self.assertEqual(recs["F-bbb"]["meta"]["insertada"], "auto")
        self.assertNotIn("insertada", recs["F-aaa"]["meta"])

    def test_lesson_without_figures_is_unchanged(self):
        from content_pipeline.processors.llm_lesson_writer import generate_lessons_from_plan
        plan = _plan()
        plan["unidades"][0]["lecciones"][0].pop("figuras")
        body = BODY.replace("{{figura:F-aaa}}", "").replace("{{figura:F-zzz}}", "")
        llm = _LLM(body)
        lesson = list(generate_lessons_from_plan(plan, source_name="Libro", client=llm, model="m"))[0]
        self.assertEqual(lesson["recursos"], [])
        self.assertNotIn("FIGURAS DEL LIBRO", llm.calls[0]["user"])


class AuditTests(SimpleTestCase):
    def test_marker_digits_are_not_figures_and_narration_skips_markers(self):
        from content_pipeline.media.narration import strip_markdown
        from content_pipeline.processors.faithfulness import audit_lessons_from_plan
        lesson = {"tipo": "texto", "nombre": "S", "unidad_orden": 1, "posicion": 1,
                  "contenido": "## Desarrollo\nDetente.\n\n{{figura:F-3fa9c12345}}\n",
                  "_source_text": "Detente."}
        audit = audit_lessons_from_plan([lesson])
        self.assertEqual(audit["figuras"], [])
        self.assertNotIn("figura", strip_markdown(lesson["contenido"]))

    def test_validation_and_import_blockers(self):
        from content_pipeline.processors.validators import import_blockers, validate_generated_course
        body = ("# S\n\n## Objetivo\nx\n\n## Desarrollo\ny\n\n{{figura:F-aaa}}\n\n{{figura:F-nope}}\n\n"
                "## Puntos clave\n- z\n\n## Resumen\nw\n\n## Fuente\nLibro.")
        lesson = {"unidad_orden": 1, "posicion": 1, "nombre": "S", "tipo": "texto", "contenido": body,
                  "fuentes": [{"fuente_nombre": "L", "pagina_inicio": 1, "pagina_fin": 1}],
                  "recursos": [{"tipo": "imagen", "rol": "figura", "clave": "F-aaa", "url": "",
                                "archivo_local": "f.png", "meta": {"insertada": "auto"}}]}
        manifest = {"curso": {"codigo": "X"}, "unidades": [{"orden": 1, "nombre": "U"}]}
        v = validate_generated_course(manifest, [lesson])
        self.assertTrue(any("F-nope" in e for e in v["errores"]))
        self.assertTrue(any("automáticamente" in a for a in v["advertencias"]))
        bloqueos, _ = import_blockers({"manifest": manifest, "lessons": [lesson]})
        self.assertTrue(any("publish_media" in b for b in bloqueos))


class ReviewOpsTests(SimpleTestCase):
    def _plan(self):
        texto = "P0 uno.\n\nP1 dos.\n\nP2 tres."
        return {"resumen": {"banda": {"max": 1200}}, "unidades": [{"id": "U-1", "nombre": "U", "lecciones": [
            {"id": "L-a", "nombre": "A", "paginas": [1, 2], "texto": texto, "palabras_fuente": 6,
             "figuras": [{"id": "F-1", "parrafo": 0, "pie": "uno"}, {"id": "F-2", "parrafo": 2, "pie": "dos"},
                         {"id": "F-3", "parrafo": None}]},
            {"id": "L-b", "nombre": "B", "paginas": [3, 3], "texto": "Q0.\n\nQ1.", "palabras_fuente": 2,
             "figuras": [{"id": "F-4", "parrafo": -1}, {"id": "F-5", "parrafo": 1}]},
        ]}]}

    def _op(self, plan, op, args, lid="L-a"):
        from content_pipeline.review.apply import apply_structure_op
        apply_structure_op(plan, {"op": op, "args": args}, {"tipo": "leccion", "id": lid})
        return plan

    def test_split_and_merge_carry_figures_with_their_paragraphs(self):
        plan = self._op(self._plan(), "split", {"k": 2})
        a, b = plan["unidades"][0]["lecciones"][:2]
        self.assertEqual([(f["id"], f["parrafo"]) for f in a["figuras"]], [("F-1", 0), ("F-3", None)])
        self.assertEqual([(f["id"], f["parrafo"]) for f in b["figuras"]], [("F-2", 0)])
        plan = self._op(self._plan(), "merge", {"con": "L-b"})
        m = plan["unidades"][0]["lecciones"][0]
        self.assertEqual([(f["id"], f["parrafo"]) for f in m["figuras"]],
                         [("F-1", 0), ("F-2", 2), ("F-3", None), ("F-4", 2), ("F-5", 4)])

    def test_figure_ops(self):
        plan = self._op(self._plan(), "figura_quitar", {"figura": "F-2"})
        self.assertEqual([f["id"] for f in plan["unidades"][0]["lecciones"][0]["figuras"]], ["F-1", "F-3"])
        plan = self._op(plan, "figura_pie", {"figura": "F-1", "pie": "Nuevo", "alt": "Alt"})
        self.assertEqual(plan["unidades"][0]["lecciones"][0]["figuras"][0]["pie"], "Nuevo")
        plan = self._op(plan, "figura_mover", {"figura": "F-1", "leccion": "L-b", "parrafo": 0})
        b = plan["unidades"][0]["lecciones"][1]
        self.assertEqual([(f["id"], f["parrafo"]) for f in b["figuras"]], [("F-4", -1), ("F-1", 0), ("F-5", 1)])
        from content_pipeline.review.apply import ReviewConflict
        with self.assertRaises(ReviewConflict):
            self._op(plan, "figura_quitar", {"figura": "F-1"})                # ya no está en L-a


class PlanFiguresAndBrujulaTests(SimpleTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_attach_rebase_and_bundle(self):
        from django.core.management import call_command
        from content_pipeline.media.recursos import rebase_archivos
        from content_pipeline.services.plan_figures import attach_figures_to_plan
        from tests.test_lesson_images import _pdf, _plan as img_plan

        plan = img_plan()
        out_dir = self.tmp / "out"
        resumen = attach_figures_to_plan(plan, _pdf(self.tmp), out_dir / "figuras_B", rel_to=out_dir)
        fig = plan["unidades"][0]["lecciones"][0]["figuras"][0]
        self.assertEqual((fig["parrafo"], fig["mapeo"]), (0, "texto"))
        self.assertTrue(fig["archivo"].startswith("figuras_B/"))
        self.assertTrue((out_dir / fig["archivo"]).exists())
        self.assertEqual(resumen["asignadas"], 2)

        lessons = [{"recursos": [{"archivo_local": fig["archivo"]}]}]
        rebase_archivos(lessons, out_dir, self.tmp / "otro")
        self.assertEqual(lessons[0]["recursos"][0]["archivo_local"], "../out/" + fig["archivo"])

        plan_p = out_dir / "plan.json"
        plan["curso"] = {"codigo": "B"}
        plan_p.write_text(json.dumps(plan), encoding="utf-8")
        zip_p = self.tmp / "b.zip"
        call_command("build_brujula", plan=str(plan_p), out=str(zip_p), stdout=io.StringIO())
        with zipfile.ZipFile(zip_p) as z:
            names = z.namelist()
            html = z.read("brujula.html").decode("utf-8")
        self.assertIn("figuras/" + Path(fig["archivo"]).name, names)
        self.assertIn('"archivo": "figuras/' + Path(fig["archivo"]).name, html)
