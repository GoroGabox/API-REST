"""Lecciones con fuente mayormente gráfica: detección, propagación y aviso."""
from __future__ import annotations

from django.test import SimpleTestCase

from content_pipeline.llm.client import LLMResponse
from content_pipeline.processors.validators import validate_generated_course
from content_pipeline.processors.visual_pages import (
    annotate_plan_visuals,
    classify_pages,
    image_references,
    lesson_visual,
)


def _stats():
    """Libro sintético: págs. 1-3 texto normal, 4 divisoria, 5-7 señales, 8 figura grande."""
    s = {p: {"imagenes": 0, "area_imagen": 0.0, "caracteres": 2200} for p in range(1, 9)}
    s[4] = {"imagenes": 1, "area_imagen": 1.0, "caracteres": 40}          # divisoria de capítulo
    for p in (5, 6, 7):
        s[p] = {"imagenes": 20, "area_imagen": 0.15, "caracteres": 700}   # página de señales
    s[8] = {"imagenes": 1, "area_imagen": 0.3, "caracteres": 1800}        # figura grande
    s[2] = {"imagenes": 3, "area_imagen": 0.05, "caracteres": 2100}       # texto con figuras
    return s


class ClassifyPagesTests(SimpleTestCase):
    def test_signs_pages_are_visual_and_dividers_ignored(self):
        kinds = classify_pages(_stats())
        self.assertEqual({p for p, k in kinds.items() if k == "visual"}, {5, 6, 7, 8})
        self.assertEqual(kinds.get(2), "figuras")
        self.assertNotIn(4, kinds)          # divisoria sin cuerpo de texto
        self.assertNotIn(1, kinds)

    def test_image_references(self):
        texto = "Observa la imagen superior. Ver figura 3. En la siguiente tabla se resume. Sin referencias acá."
        self.assertEqual(image_references(texto), 3)
        self.assertEqual(image_references("El conductor debe mirar los espejos."), 0)


class LessonVisualTests(SimpleTestCase):
    def setUp(self):
        self.stats = _stats()
        self.kinds = classify_pages(self.stats)

    def test_signs_lesson_is_high_priority(self):
        v = lesson_visual([4, 7], "PARE CEDA EL PASO", self.kinds, self.stats)
        self.assertEqual(v["nivel"], "alta")
        self.assertEqual(v["paginas_visuales"], [5, 6, 7])
        self.assertIn("mayormente gráficas", v["motivo"])

    def test_figures_with_reference_is_medium(self):
        v = lesson_visual([1, 3], "Como muestra la imagen superior, el tablero indica…", self.kinds, self.stats)
        self.assertEqual(v["nivel"], "media")
        self.assertEqual(v["paginas_con_figuras"], [2])

    def test_plain_text_lesson_is_not_flagged(self):
        self.assertIsNone(lesson_visual([1, 1], "Texto corrido sin figuras.", self.kinds, self.stats))

    def test_without_page_stats_only_references_count(self):
        plan = {"unidades": [{"lecciones": [
            {"paginas": [1, 1], "texto": "Ver la imagen superior y la siguiente figura."},
            {"paginas": [1, 1], "texto": "Texto normal."},
        ]}]}
        resumen = annotate_plan_visuals(plan, source=["cap1.docx", "cap2.docx"])
        l1, l2 = plan["unidades"][0]["lecciones"]
        self.assertEqual(l1["visual"]["nivel"], "media")
        self.assertNotIn("visual", l2)
        self.assertEqual(resumen, {"alta": 0, "media": 1})


class _LLM:
    meter = None

    def __init__(self):
        self.users = []

    def complete_meta(self, **kw):
        self.users.append(kw["user"])
        if "evaluador" in kw["system"]:
            return LLMResponse('{"questions": []}', stop_reason="end_turn")
        return LLMResponse("# T\n\n## Objetivo\nx\n\n## Desarrollo\ny\n\n## Puntos clave\n- z\n\n## Resumen\nw",
                           stop_reason="end_turn")


class VisualPropagationTests(SimpleTestCase):
    def _plan(self):
        visual = {"nivel": "alta", "paginas_visuales": [150, 151], "paginas_con_figuras": [], "imagenes": 40,
                  "referencias": 0, "motivo": "Las páginas 150–151 son mayormente gráficas."}
        return {"curso": {"codigo": "B"}, "unidades": [{"orden": 1, "nombre": "Señales", "paginas": [150, 151],
                "lecciones": [{"id": "L-s", "nombre": "Señales verticales", "paginas": [150, 151],
                               "palabras_objetivo": 900, "texto": "PARE. CEDA EL PASO.", "visual": visual}]}]}

    def test_flag_reaches_lesson_prompt_and_validation(self):
        from content_pipeline.processors.llm_lesson_writer import generate_lessons_from_plan
        client = _LLM()
        lessons = list(generate_lessons_from_plan(self._plan(), source_name="Libro", client=client, model="x"))
        lec = next(l for l in lessons if l["tipo"] == "texto")
        self.assertEqual(lec["revision_visual"]["nivel"], "alta")
        self.assertIn("mayormente gráficas", client.users[0])       # aviso al redactor
        for l in lessons:
            l.pop("_source_text", None)
        v = validate_generated_course({"unidades": [{"orden": 1}]}, lessons)
        self.assertEqual(v["revision_visual"], ["U1.1 Señales verticales (alta)"])
        self.assertTrue(any("revisión visual" in a for a in v["advertencias"]))
        self.assertNotIn("U1.1 Señales verticales", " ".join(v["errores"]))   # no bloquea
