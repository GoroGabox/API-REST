"""Figuras de OTRO documento → plan ya revisado (attach_figures): mapeo por capítulo + IA,
galería (`en_texto: false`) y anexo de señales sin redacción."""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from content_pipeline.llm.client import LLMResponse
from content_pipeline.media.images import Figura
from content_pipeline.processors.outline import Chapter, Para
from content_pipeline.services import plan_figures as pf


def _fig(n: int, pagina: int, pie: str) -> Figura:
    return Figura(data=b"img%d" % n, ext="png", ancho=10, alto=10, orden=n, origen="pdf", pagina=pagina,
                  pie_libro=pie, ancla_antes="texto del libro oficial que no está en la guía resumida")


CHAPTERS = [
    Chapter(titulo="Normas de circulación", page_start=1, page_end=2, paras=[Para(text="cuerpo", page=1)]),
    Chapter(titulo="Señales de tránsito verticales", page_start=3, page_end=4,
            paras=[Para(text="PARE", page=3), Para(text="CEDA EL PASO", page=3)]),
    Chapter(titulo="Glosario y referencias", page_start=5, page_end=5, paras=[Para(text="g", page=5)]),
]
FIGS = [_fig(1, 1, "Tablero"), _fig(2, 2, "Foto de carretera"), _fig(3, 3, "PARE"), _fig(4, 3, "NO ADELANTAR"),
        _fig(5, 5, "Logo del glosario")]


def _plan():
    return {"curso": {"codigo": "B"}, "resumen": {"banda": {"max": 1200}}, "revision_humana": [{"fase": "estructura"}],
            "unidades": [{"id": "U-1", "orden": 1, "nombre": "NORMAS DE CIRCULACION", "lecciones": [
                {"id": "L-sen", "nombre": "Señales verticales y demarcaciones", "paginas": [1, 1],
                 "texto": "Las señales reglamentarias obligan.\n\nFrente a una señal PARE debes detenerte."},
                {"id": "L-vel", "nombre": "Velocidad y tablero", "paginas": [1, 1],
                 "texto": "El tablero muestra la velocidad.\n\nRespeta los límites."},
            ]}]}


class _MapLLM:
    """Responde en orden; registra los prompts."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def complete(self, **kw):
        self.calls.append(kw)
        a = self.answers.pop(0)
        return a if isinstance(a, str) else json.dumps({"asignaciones": a})


class AttachCrossTests(SimpleTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _attach(self, plan, llm, **kw):
        with patch.object(pf, "extract_figures", return_value=(copy.deepcopy(FIGS), {})), \
             patch.object(pf, "build_outline", return_value=CHAPTERS):
            return pf.attach_figures_cross(plan, "libro.pdf", self.tmp / "figuras_B", rel_to=self.tmp,
                                           map_client=llm, map_model="m", **kw)

    def _answers(self):
        return ([{"figura": "F1", "leccion": 1, "parrafo": 0, "motivo": "habla del tablero"},
                 {"figura": "F2", "leccion": None}],
                [{"figura": "F1", "leccion": 0, "parrafo": 1, "motivo": "nombra la señal PARE"},
                 {"figura": "F2", "leccion": None}])

    def test_maps_by_chapter_and_ia_keeps_plan_intact(self):
        plan = _plan()
        antes = copy.deepcopy(plan["unidades"][0])
        llm = _MapLLM(*self._answers())
        res = self._attach(plan, llm)
        sen, vel = plan["unidades"][0]["lecciones"]
        self.assertEqual((sen["texto"], vel["texto"], sen["id"]), (antes["lecciones"][0]["texto"],
                                                                   antes["lecciones"][1]["texto"], "L-sen"))
        self.assertEqual(plan["revision_humana"], [{"fase": "estructura"}])
        # Tablero → lección de velocidad, tras el párrafo 0, por IA.
        self.assertEqual([(f["pie"], f["parrafo"], f["mapeo"]) for f in vel["figuras"]], [("Tablero", 0, "ia")])
        # Señales: la que el texto nombra va en el texto; la otra, galería en la lección de señales.
        self.assertEqual([(f["pie"], f["parrafo"], f.get("en_texto", True)) for f in sen["figuras"]],
                         [("PARE", 1, True), ("NO ADELANTAR", None, False)])
        # Anexo con TODAS las señales.
        anexo = plan["unidades"][1]
        self.assertEqual(anexo["nombre"], "Anexo: Señales de tránsito")
        lec = anexo["lecciones"][0]
        self.assertTrue(lec["anexo"] and lec["id"])
        self.assertEqual([f["pie"] for f in lec["figuras"]], ["PARE", "NO ADELANTAR"])
        self.assertTrue((self.tmp / lec["figuras"][0]["archivo"]).exists())
        # Reporte: foto sin lección, glosario descartado, conteos.
        self.assertEqual([s["pie"] for s in res["sin_leccion"]], ["Foto de carretera"])
        self.assertEqual(res["descartadas"], {"capitulo_sin_unidad: Glosario y referencias": 1})
        self.assertEqual((res["en_texto"], res["galeria"], res["anexo"]), (2, 1, 2))
        self.assertIn("NOMBRA esa señal", llm.calls[1]["system"])            # modo estricto para el anexo
        self.assertIn("[1] Frente a una señal PARE", llm.calls[0]["user"])    # párrafos numerados

    def test_vision_descriptions_are_cached_by_hash(self):
        class _Vision:
            def __init__(self):
                self.n = 0

            def complete(self, **kw):
                self.n += 1
                return json.dumps({"relevante": True, "pie": f"Pie IA {self.n}", "alt": "alt"})
        v = _Vision()
        res = self._attach(_plan(), _MapLLM(*self._answers()), vision_client=v, vision_model="v")
        self.assertEqual(v.n, 4)                                              # el glosario no se describe
        self.assertEqual(len(res["ia"]), 0)                                   # clientes falsos sin medidor
        self.assertTrue((self.tmp / "figuras_B" / "descripciones.json").exists())
        plan = _plan()
        self._attach(plan, _MapLLM(*self._answers()), vision_client=v, vision_model="v")
        self.assertEqual(v.n, 4)                                              # 2ª corrida: todo desde la caché
        self.assertTrue(plan["unidades"][0]["lecciones"][1]["figuras"][0]["pie"].startswith("Pie IA"))

    def test_summary_reports_cost_meters(self):
        from content_pipeline.llm.client import CostMeter
        llm = _MapLLM(*self._answers())
        llm.meter = CostMeter()                                               # dataclass no hashable
        res = self._attach(_plan(), llm)
        self.assertEqual(len(res["ia"]), 1)

    def test_chapter_names_match_approximately(self):
        m = pf._match_chapters(
            ["Los siniestros de tránsito", "Convivencia Vial", "Señales de tránsito verticales", "Glosario y referencias"],
            ["LOS SINIESTROS DEL TRANSITO", "CONVIVENCIA VIAL", "NORMAS DE CIRCULACION"])
        self.assertEqual(m, {0: 0, 1: 1, 2: None, 3: None})

    def test_llm_call_failure_propagates_instead_of_writing_an_empty_plan(self):
        from content_pipeline.llm.client import LLMError

        class _Caido:
            def complete(self, **kw):
                raise LLMError("401 invalid x-api-key")
        with self.assertRaises(LLMError):
            self._attach(_plan(), _Caido())
        with self.assertRaises(LLMError):                                      # también la visión
            self._attach(_plan(), _MapLLM(*self._answers()), vision_client=_Caido(), vision_model="v")

    def test_annex_sign_enters_text_only_if_named_and_density_is_capped(self):
        # La IA propone las dos señales para el texto; solo PARE está nombrada en la lección.
        ambas = [{"figura": "F1", "leccion": 0, "parrafo": 1}, {"figura": "F2", "leccion": 0, "parrafo": 0}]
        plan = _plan()
        self._attach(plan, _MapLLM(self._answers()[0], ambas))
        sen = plan["unidades"][0]["lecciones"][0]
        self.assertEqual([(f["pie"], f.get("en_texto", True)) for f in sen["figuras"]],
                         [("PARE", True), ("NO ADELANTAR", False)])
        # Tope por párrafo: 5 figuras tras el mismo párrafo → 3 en el texto, 2 a galería.
        asignadas = {"L": [(_fig(i, 1, f"f{i}"), "ia", 0) for i in range(1, 6)]}
        extra = {}
        self.assertEqual(pf._limitar_densidad(asignadas, extra, set()), 2)
        self.assertEqual([extra[("L", f.hash)]["en_texto"] for f, _m, _p in asignadas["L"]],
                         [True, True, True, False, False])

    def test_rerun_is_idempotent_and_sin_anexo(self):
        plan = _plan()
        self._attach(plan, _MapLLM(*self._answers()))
        self._attach(plan, _MapLLM(*self._answers()), anexo=False)
        self.assertEqual(len(plan["unidades"]), 1)                            # el anexo previo se reemplaza/omite
        self.assertEqual(len(plan["unidades"][0]["lecciones"][0]["figuras"]), 2)

    def test_invalid_answer_retries_then_leaves_unassigned(self):
        malo = [{"figura": "F1", "leccion": 7, "parrafo": 0}]
        plan = _plan()
        llm = _MapLLM(malo, malo, *self._answers()[1:])
        res = self._attach(plan, llm)
        self.assertIn("no existe", llm.calls[1]["user"])                      # reintento con el error
        self.assertNotIn("figuras", plan["unidades"][0]["lecciones"][1])      # nunca a ciegas
        self.assertEqual(len(res["sin_leccion"]), 2)


BODY = "# T\n\n## Objetivo\nx\n\n## Desarrollo\ny\n\n{{figura:F-a}}\n\n## Puntos clave\n- z\n\n## Resumen\nw"


class _Writer:
    meter = None

    def __init__(self):
        self.calls = []

    def complete_meta(self, **kw):
        self.calls.append(kw)
        if "evaluador" in kw["system"]:
            return LLMResponse(json.dumps({"questions": []}), stop_reason="end_turn")
        return LLMResponse(BODY, stop_reason="end_turn")


class WriterGalleryAndAnnexTests(SimpleTestCase):
    def _plan(self):
        return {"curso": {"codigo": "B"}, "unidades": [
            {"orden": 1, "nombre": "Normas", "categoria": "General", "paginas": [1, 2], "lecciones": [
                {"id": "L-1", "nombre": "Señales", "paginas": [1, 2], "palabras_objetivo": 900, "texto": "Texto.",
                 "figuras": [{"id": "F-a", "pie": "PARE", "parrafo": 0, "archivo": "figuras_B/a.png"},
                             {"id": "F-g", "pie": "Galería", "parrafo": None, "en_texto": False,
                              "archivo": "figuras_B/g.png"}]}]},
            {"orden": 2, "nombre": "Anexo: Señales de tránsito", "categoria": "Señales de Tránsito",
             "paginas": [149, 160], "lecciones": [
                {"id": "L-x", "nombre": "Señales de tránsito", "anexo": True, "paginas": [149, 160],
                 "palabras_objetivo": 1200, "texto": "PARE\n\nCEDA EL PASO",
                 "figuras": [{"id": "F-a", "pie": "PARE", "archivo": "figuras_B/a.png"},
                             {"id": "F-b", "pie": "CEDA EL PASO", "archivo": "figuras_B/b.png"}]}]},
        ]}

    def test_gallery_figure_skips_prompt_and_annex_skips_llm_and_quiz(self):
        from content_pipeline.processors.faithfulness import audit_lessons_from_plan, build_lesson_sources_from_plan
        from content_pipeline.processors.figure_markers import figure_ids
        from content_pipeline.processors.llm_lesson_writer import generate_lessons_from_plan
        from content_pipeline.processors.validators import validate_generated_course

        llm = _Writer()
        lessons = list(generate_lessons_from_plan(self._plan(), source_name="Libro", client=llm, model="m"))
        self.assertEqual([(l["tipo"], l["nombre"]) for l in lessons],
                         [("texto", "Señales"), ("quiz", "Evaluación del módulo 1"), ("texto", "Señales de tránsito")])
        normal, _quiz, anexo = lessons
        redaccion = [c for c in llm.calls if "evaluador" not in c["system"]]
        self.assertEqual(len(redaccion), 1)                                   # el anexo no se redacta
        self.assertIn("{{figura:F-a}}", redaccion[0]["user"])
        self.assertNotIn("F-g", redaccion[0]["user"])                         # la de galería no va al prompt
        self.assertEqual(figure_ids(normal["contenido"]), ["F-a"])            # ni se auto-inserta
        recs = {r["clave"]: r for r in normal["recursos"]}
        self.assertTrue(recs["F-g"]["meta"]["galeria"])
        # Anexo: todas las figuras, en orden, marcado y sin auditar.
        self.assertTrue(anexo["anexo"])
        self.assertEqual(figure_ids(anexo["contenido"]), ["F-a", "F-b"])
        self.assertEqual([r["clave"] for r in anexo["recursos"]], ["F-a", "F-b"])
        self.assertEqual(audit_lessons_from_plan(lessons)["auditadas"], 1)
        self.assertEqual(len(build_lesson_sources_from_plan(lessons)), 1)
        manifest = {"curso": {"codigo": "B"}, "unidades": [{"orden": 1, "nombre": "Normas"},
                                                           {"orden": 2, "nombre": "Anexo"}]}
        v = validate_generated_course(manifest, lessons)
        self.assertEqual([e for e in v["errores"] if "U2" in e], [])          # sin secciones núcleo exigidas
        self.assertFalse(any("U2" in a and "quiz" in a for a in v["advertencias"]))

    def test_moving_a_gallery_figure_into_a_paragraph_puts_it_in_the_text(self):
        from content_pipeline.review.apply import apply_structure_op
        plan = {"unidades": [{"id": "U-1", "lecciones": [
            {"id": "L-1", "nombre": "A", "texto": "P0.\n\nP1.", "palabras_fuente": 2,
             "figuras": [{"id": "F-g", "parrafo": None, "en_texto": False}]}]}]}
        apply_structure_op(plan, {"op": "figura_mover", "args": {"figura": "F-g", "parrafo": 1}},
                           {"tipo": "leccion", "id": "L-1"})
        fig = plan["unidades"][0]["lecciones"][0]["figuras"][0]
        self.assertEqual((fig["parrafo"], "en_texto" in fig), (1, False))
