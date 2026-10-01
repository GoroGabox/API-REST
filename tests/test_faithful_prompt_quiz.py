"""Prompt fiel a la fuente + quiz por lección con evidencia textual verificada."""
from __future__ import annotations

import json

from django.test import SimpleTestCase

from content_pipeline.llm.client import LLMResponse
from content_pipeline.processors.validators import validate_generated_course

CORE_BODY = "# T\n\n## Objetivo\nx\n\n## Desarrollo\ny\n\n## Puntos clave\n- z\n\n## Resumen\nw"


class _RecordingLLM:
    """Registra los prompts; lección con secciones núcleo + una opcional marcada."""
    meter = None

    def __init__(self, quiz_json=None, body=None):
        self.calls = []
        self.quiz_json = quiz_json
        self.body = body or (
            "# T\n\n## Objetivo\nx\n\n## Desarrollo\ny\n\n## Puntos clave\n- z\n\n"
            "## Errores frecuentes (opcional)\n- e\n\n## Resumen\nw")

    def complete_meta(self, **kw):
        self.calls.append(kw)
        if "evaluador" in kw["system"]:
            return LLMResponse(json.dumps(self.quiz_json or {"questions": []}), stop_reason="end_turn")
        return LLMResponse(self.body, stop_reason="end_turn")


def _q(question, correct="50 km/h", evidencia="El límite urbano es 50 km/h", opts=None):
    return {"question": question, "options": opts or [correct, "60 km/h", "40 km/h", "80 km/h"],
            "correct_index": 0, "explanation": "según el manual", "evidencia": evidencia}


def _plan():
    return {"curso": {"nombre": "Curso B", "codigo": "B"}, "unidades": [{
        "orden": 1, "nombre": "Normas", "categoria": "General", "paginas": [10, 12],
        "lecciones": [{"id": "L-x", "nombre": "Velocidad urbana", "paginas": [10, 11],
                       "palabras_objetivo": 900, "texto": "El límite urbano es 50 km/h."}]}]}


class FaithfulPromptTests(SimpleTestCase):
    def _write(self, client, texto="Texto breve."):
        from content_pipeline.processors.llm_lesson_writer import write_lesson_from_source_meta
        return write_lesson_from_source_meta(title="T", tema="T", unidad_nombre="U", source_text=texto,
                                             palabras=1200, fuente_md="Libro, páginas 1-2.",
                                             client=client, model="x")

    def test_prompt_forbids_general_knowledge_and_uses_full_source(self):
        client = _RecordingLLM()
        body, ok = self._write(client, ("Primer párrafo del manual. " * 400) + "FINAL-DEL-EXTRACTO.")
        call = client.calls[0]
        self.assertTrue(ok)
        self.assertIn("FINAL-DEL-EXTRACTO.", call["user"])          # sin truncar a 8.000 caracteres
        self.assertNotIn("redacta lo general", call["system"])
        self.assertIn("NORMAS LEGALES", call["system"])
        self.assertIn("(opcional)", call["system"])
        self.assertNotIn("(opcional)", body)                         # marcador limpiado
        self.assertIn("## Errores frecuentes", body)
        self.assertLessEqual(call["temperature"], 0.3)

    def test_optional_sections_can_be_omitted(self):
        body, ok = self._write(_RecordingLLM(body=CORE_BODY))
        self.assertTrue(ok)
        lesson = {"unidad_orden": 1, "posicion": 1, "nombre": "T", "tipo": "texto", "contenido": body,
                  "fuentes": [{"fuente_nombre": "L", "pagina_inicio": 1, "pagina_fin": 1}]}
        self.assertEqual(validate_generated_course({"unidades": [{"orden": 1}]}, [lesson])["errores"], [])

    def test_missing_core_section_triggers_stub(self):
        body, ok = self._write(_RecordingLLM(body="# T\n\n## Objetivo\nx\n\n## Resumen\nw"))
        self.assertFalse(ok)                                         # sin Desarrollo/Puntos clave → stub

    def test_length_is_proportional_to_source(self):
        from content_pipeline.processors.llm_lesson_writer import lesson_length
        self.assertEqual(lesson_length(700, 1200), (462, 770))       # ya no pide 1.200 sobre 700
        self.assertEqual(lesson_length(5000, 1200)[1], 1200)          # techo = tope del plan
        self.assertEqual(lesson_length(50, 1200)[1], 250)             # piso

    def test_requested_range_reaches_the_prompt(self):
        client = _RecordingLLM()
        self._write(client, "palabra " * 700)
        self.assertIn("entre 462 y 770 palabras", client.calls[0]["user"])


class QuizFromPlanTests(SimpleTestCase):
    SRC = "El límite urbano es 50 km/h. En autopista el máximo es 120 km/h."

    def test_question_validation_rules(self):
        from content_pipeline.processors.llm_lesson_writer import _norm_evidence, validate_quiz_question
        src = _norm_evidence(self.SRC)
        self.assertIsNone(validate_quiz_question(_q("¿Límite urbano?"), src))
        self.assertIsNone(validate_quiz_question(_q("?", evidencia="«el límite urbano es 50 km/h»"), src))
        self.assertEqual(validate_quiz_question(_q("?", evidencia="El límite urbano es 60 km/h"), src),
                         "la evidencia no está en la fuente")
        self.assertEqual(validate_quiz_question(_q("?", opts=["a", "b", "c"]), src), "no tiene 4 opciones")
        self.assertEqual(validate_quiz_question(_q("?", opts=["a", "a", "b", "c"]), src), "opciones repetidas")
        bad = _q("?")
        bad["correct_index"] = 7
        self.assertEqual(validate_quiz_question(bad, src), "correct_index inválido")
        self.assertEqual(validate_quiz_question(_q("?", opts=["a", "b", "c", "Todas las anteriores"]), src),
                         "opción comodín")
        self.assertEqual(validate_quiz_question(_q("?", evidencia="50 km/h"), src), "sin evidencia")

    def test_invalid_questions_are_dropped_and_options_shuffled(self):
        from content_pipeline.processors.llm_lesson_writer import questions_for_lesson
        client = _RecordingLLM({"questions": [
            _q("¿Cuál es el límite urbano?"),
            _q("¿Pregunta inventada?", evidencia="Nunca se debe superar 90 km/h"),
        ]})
        qs, descartes = questions_for_lesson(titulo="Velocidad", texto=self.SRC, n=2, client=client, model="x")
        self.assertEqual(len(qs), 1)
        self.assertTrue(any("evidencia no está" in d for d in descartes))
        q = qs[0]
        self.assertEqual(q["options"][q["correct_index"]], "50 km/h")   # la correcta sigue siéndolo tras barajar
        self.assertEqual((q["leccion"], q["evidencia"]), ("Velocidad", "El límite urbano es 50 km/h"))
        self.assertEqual(len(client.calls), 2)                            # reintento por la descartada

    def test_correct_answers_are_not_all_in_first_position(self):
        from content_pipeline.processors.llm_lesson_writer import _shuffle_options
        positions = {_shuffle_options(_q(f"¿Pregunta número {i}?"))["correct_index"] for i in range(20)}
        self.assertGreater(len(positions), 1)

    def test_every_lesson_contributes_questions(self):
        from content_pipeline.processors.llm_lesson_writer import write_quiz_from_plan
        lecciones = [{"nombre": f"L{i}", "texto": self.SRC} for i in range(3)]
        client = _RecordingLLM({"questions": [
            _q("¿Límite urbano?"),
            _q("¿Máximo en autopista?", correct="120 km/h", evidencia="En autopista el máximo es 120 km/h"),
        ]})
        quiz, meta, ok = write_quiz_from_plan(unidad_nombre="U", lecciones=lecciones, client=client, model="x")
        self.assertTrue(ok)
        self.assertEqual({q["leccion"] for q in quiz["questions"]}, {"L0", "L1", "L2"})
        self.assertEqual(meta["lecciones_sin_preguntas"], [])
        self.assertTrue(all(self.SRC in c["user"] for c in client.calls))   # cada llamada ve la fuente completa

    def test_quiz_without_valid_questions_is_flagged(self):
        from content_pipeline.processors.llm_lesson_writer import generate_lessons_from_plan
        client = _RecordingLLM({"questions": [_q("?", evidencia="esto no existe en la fuente del plan")]},
                               body=CORE_BODY)
        out = list(generate_lessons_from_plan(_plan(), source_name="Libro B", client=client, model="x"))
        quiz = next(l for l in out if l["tipo"] == "quiz")
        self.assertTrue(quiz["fuente_debil"])
        self.assertEqual(quiz["quiz_meta"]["lecciones_sin_preguntas"], ["Velocidad urbana"])

    def test_valid_quiz_passes_course_validation(self):
        from content_pipeline.processors.llm_lesson_writer import generate_lessons_from_plan
        client = _RecordingLLM({"questions": [_q("¿Límite urbano?", evidencia="El límite urbano es 50 km/h")]},
                               body=CORE_BODY)
        lessons = list(generate_lessons_from_plan(_plan(), source_name="Libro B", client=client, model="x"))
        for l in lessons:
            l.pop("_source_text", None)
        v = validate_generated_course({"unidades": [{"orden": 1}]}, lessons)
        self.assertEqual((v["errores"], v["stubs"]), ([], []))
