"""Juez de fidelidad + cobertura, juez de claves de quiz y detector de cifras ampliado."""
from __future__ import annotations

import json

from django.test import SimpleTestCase

from content_pipeline.processors.faithfulness import (
    JUDGE_SYSTEM,
    _num_core,
    audit_lessons_from_plan,
    judge_lessons_llm,
    judge_quiz_llm,
    unsupported_figures,
)
from content_pipeline.processors.validators import import_blockers


class FigureGuardTests(SimpleTestCase):
    def test_canonical_numbers(self):
        self.assertEqual(_num_core("82.000"), "82000")
        self.assertEqual(_num_core("0,3"), "0.3")
        self.assertEqual(_num_core("0.30"), "0.3")
        self.assertEqual(_num_core("2,5"), "2.5")
        self.assertEqual(_num_core("tres"), "3")
        self.assertEqual(_num_core("1.600"), "1600")

    def test_alcohol_limits_decimal_is_not_confused(self):
        source = "Con 0,3 gramos por litro de alcohol en la sangre ya hay manejo bajo la influencia."
        self.assertEqual(unsupported_figures("El límite es 0,3 g/l.", source), [])
        self.assertEqual(unsupported_figures("El límite es 0,8 g/l.", source), ["0,8 g/l"])
        # antes "3 g/l" coincidía con el 3 de "0,3" (se quitaba la coma)
        self.assertEqual(unsupported_figures("Hasta 3 g/l.", source), ["3 g/l"])

    def test_meters_seconds_and_weights_are_checked(self):
        source = "Mantén una distancia de 2 segundos. A 50 km/h recorres 14 metros por segundo."
        figs = unsupported_figures("Deja 3 segundos y frena en 30 metros; el auto pesa 1.200 kg.", source)
        self.assertEqual(figs, ["3 segundos", "30 metros", "1.200 kg"])
        self.assertEqual(unsupported_figures("Guarda 2 segundos: son 14 metros.", source), [])

    def test_numbers_written_in_words(self):
        source = "La regla de los dos segundos."
        self.assertEqual(unsupported_figures("Usa la regla de los 2 segundos.", source), [])
        self.assertEqual(unsupported_figures("Deja tres segundos de distancia.", source), ["tres segundos"])

    def test_same_number_with_other_unit_is_flagged(self):
        source = "En zona urbana el límite es 50 km/h."
        figs = unsupported_figures("Frena a 50 metros del cruce.", source)
        self.assertEqual(figs, ["50 metros (en la fuente: km/h)"])
        self.assertEqual(unsupported_figures("Circula a 50 kilómetros por hora.", source), [])

    def test_plain_words_are_not_units(self):
        self.assertEqual(unsupported_figures("Hay 2 motos y 3 mas.", "texto sin números"), [])

    def test_plan_audit_uses_each_lesson_own_source(self):
        lessons = [
            {"tipo": "texto", "unidad_orden": 1, "nombre": "A", "_source_text": "Urbano: 50 km/h.",
             "contenido": "Urbano 50 km/h y autopista 120 km/h."},
            {"tipo": "texto", "unidad_orden": 2, "nombre": "B", "_source_text": "Autopista: 120 km/h.",
             "contenido": "Autopista 120 km/h."},
        ]
        res = audit_lessons_from_plan(lessons)
        self.assertEqual(res["figuras"], [{"leccion": "U1 · A", "cifras": ["120 km/h"]}])


class _Judge:
    def __init__(self, payload):
        self.raw = json.dumps(payload, ensure_ascii=False)
        self.calls = []

    def complete(self, **kw):
        self.calls.append(kw)
        return self.raw


def _pairs():
    return [({"tipo": "texto", "unidad_orden": 1, "nombre": "L", "contenido": "c"}, "fuente")]


class JudgeCoverageTests(SimpleTestCase):
    def test_prompt_audits_examples_and_advice(self):
        self.assertNotIn("IGNORA el marco pedagógico", JUDGE_SYSTEM)
        self.assertIn("ejemplos", JUDGE_SYSTEM)
        self.assertIn("AUNQUE sea", JUDGE_SYSTEM)
        self.assertIn("omissions", JUDGE_SYSTEM)

    def test_low_coverage_is_reported_separately(self):
        client = _Judge({"faithfulness": 0.95, "unsupported_claims": [], "coverage": 0.4,
                         "omissions": ["prohibición de estacionar en curvas"]})
        res = judge_lessons_llm(_pairs(), client=client, model="x")
        self.assertEqual(res["criticas"], [])
        self.assertEqual(res["promedio_cobertura"], 0.4)
        self.assertEqual(res["cobertura_baja"][0]["omissions"], ["prohibición de estacionar en curvas"])

    def test_old_judge_payload_without_coverage_still_works(self):
        res = judge_lessons_llm(_pairs(), client=_Judge({"faithfulness": 0.5, "unsupported_claims": ["x"]}),
                                model="x")
        self.assertEqual(len(res["criticas"]), 1)
        self.assertIsNone(res["promedio_cobertura"])
        self.assertEqual(res["cobertura_baja"], [])

    def test_long_source_reaches_the_judge(self):
        client = _Judge({"faithfulness": 1.0, "unsupported_claims": [], "coverage": 1.0, "omissions": []})
        fuente = "palabra " * 3000 + "FINAL"
        judge_lessons_llm([({"tipo": "texto", "unidad_orden": 1, "nombre": "L", "contenido": "c"}, fuente)],
                          client=client, model="x")
        self.assertIn("FINAL", client.calls[0]["user"])


def _quiz(evidencia="El límite urbano es 50 km/h"):
    return {"tipo": "quiz", "unidad_orden": 1, "nombre": "Evaluación del módulo 1", "contenido": {"questions": [
        {"question": "¿Límite urbano?", "options": ["60 km/h", "50 km/h", "40 km/h", "80 km/h"],
         "correct_index": 0, "explanation": "", "evidencia": evidencia},
        {"question": "Sin evidencia", "options": ["a", "b", "c", "d"], "correct_index": 1},
    ]}}


class QuizJudgeTests(SimpleTestCase):
    def test_wrong_key_is_reported(self):
        client = _Judge({"items": [{"n": 1, "ok": False, "problema": "La evidencia dice 50 km/h, no 60."}]})
        res = judge_quiz_llm([_quiz()], client=client, model="x")
        self.assertEqual((res["evaluadas"], res["sin_evidencia"]), (1, 1))
        self.assertEqual(res["problemas"][0]["leccion"], "U1 · Evaluación del módulo 1")
        self.assertIn("Correcta: A", client.calls[0]["user"])
        self.assertIn("Evidencia:", client.calls[0]["user"])

    def test_ok_items_produce_no_problems(self):
        res = judge_quiz_llm([_quiz()], client=_Judge({"items": [{"n": 1, "ok": True}]}), model="x")
        self.assertEqual(res["problemas"], [])

    def test_quiz_problems_block_import(self):
        data = {"manifest": {"unidades": []}, "lessons": [],
                "auditoria": {"juez_quiz": {"problemas": [{"leccion": "U1 · Evaluación", "pregunta": "¿?",
                                                           "problema": "clave errónea"}]}}}
        bloqueos, _ = import_blockers(data)
        self.assertTrue(any("clave dudosa" in b for b in bloqueos))


class _SeqJudge:
    """Devuelve respuestas en secuencia (primera consulta, reconfirmación…)."""

    def __init__(self, *payloads):
        self.payloads = [json.dumps(p) for p in payloads]
        self.calls = 0

    def complete(self, **kw):
        out = self.payloads[min(self.calls, len(self.payloads) - 1)]
        self.calls += 1
        return out


class QuizJudgeConsistencyTests(SimpleTestCase):
    def test_false_without_concrete_problem_is_ignored(self):
        res = judge_quiz_llm([_quiz()], client=_SeqJudge({"items": [{"n": 1, "ok": False, "problema": ""}]}),
                             model="x")
        self.assertEqual(res["problemas"], [])

    def test_unconfirmed_flag_is_discarded(self):
        client = _SeqJudge({"items": [{"n": 1, "ok": False, "problema": "dudosa"}]},
                           {"items": [{"n": 1, "ok": True, "problema": ""}]})
        res = judge_quiz_llm([_quiz()], client=client, model="x")
        self.assertEqual(res["problemas"], [])
        self.assertEqual(len(res["descartados"]), 1)
        self.assertEqual(client.calls, 2)

    def test_prompt_asks_analysis_before_verdict(self):
        from content_pipeline.processors.faithfulness import QUIZ_JUDGE_SYSTEM
        self.assertLess(QUIZ_JUDGE_SYSTEM.index('"analisis"'), QUIZ_JUDGE_SYSTEM.index('"ok": true, "problema"'))
