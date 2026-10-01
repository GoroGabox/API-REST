"""Estructura del plan por IA: cortes de lección por tema/densidad (rangos de párrafos)."""
from __future__ import annotations

import json
from unittest.mock import patch

from django.test import SimpleTestCase

from content_pipeline.processors.outline import Chapter, Para
from content_pipeline.services import course_planner as cp
from content_pipeline.services.plan_structure import (
    LLMSegmenter, repair_groups, segment_chapter_llm, segment_units_llm, validate_groups,
)


class _Meter:
    calls = 0
    cost_usd = 0.0


class _ScriptedLLM:
    """Cliente falso: devuelve las respuestas en orden y registra los prompts."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.meter = _Meter()

    def complete(self, **kw):
        self.prompts.append(kw["user"])
        r = self.responses.pop(0)
        return r if isinstance(r, str) else json.dumps(r)


def _paras(*texts, page=1):
    return [Para(text=t, page=page + i) for i, t in enumerate(texts)]


CAP6 = _paras(
    "En este capítulo se verá lo sig.",
    "1)Señales de carabineros:",
    "Las indicaciones de un carabinero prevalecen ante un semáforo.",
    "2) Semáforos:",
    "Luz roja es detenerse antes de la línea de detención.",
    "III) LAS REGLAS DEL TRANSITO La obligación de ceder el paso:",
    "-Frente a una señal PARE debes detenerte y ceder el paso.",
)


# Mismos párrafos, más largos: las lecciones superan el mínimo y no se fusionan.
CAP6_BIG = [Para(text=" ".join([p.text] * 15), page=p.page) for p in CAP6]


def _lec(desde, hasta, titulo, **kw):
    return {"desde": desde, "hasta": hasta, "titulo": titulo, **kw}


class ValidateGroupsTests(SimpleTestCase):
    def test_valid_contiguous_ranges(self):
        grupos, err = validate_groups([_lec(0, 2, "A"), _lec(3, 4, "B", densidad="ALTA")], 5)
        self.assertIsNone(err)
        self.assertEqual([(g.desde, g.hasta) for g in grupos], [(0, 2), (3, 4)])
        self.assertEqual(grupos[1].densidad, "alta")

    def test_rejects_gap_overlap_out_of_range_and_incomplete(self):
        for items in ([_lec(0, 1, "A"), _lec(3, 4, "B")],      # hueco
                      [_lec(0, 2, "A"), _lec(2, 4, "B")],      # solape
                      [_lec(0, 9, "A")],                       # fuera de rango
                      [_lec(0, 2, "A")],                       # no cubre todo
                      [_lec(0, 4, "")],                        # sin título
                      []):
            _, err = validate_groups(items, 5)
            self.assertIsNotNone(err, items)


class SegmentChapterTests(SimpleTestCase):
    def test_groups_by_ranges(self):
        llm = _ScriptedLLM({"lecciones": [
            _lec(0, 4, "Señales de carabineros y semáforos", temas=["Señales de carabineros", "Semáforos"],
                 densidad="alta", motivo="cambia a reglas de preferencia"),
            _lec(5, 6, "La obligación de ceder el paso"),
        ]})
        grupos = segment_chapter_llm("Normas de circulación", CAP6, "media", 1200, client=llm, model="m")
        self.assertEqual([(g.desde, g.hasta) for g in grupos], [(0, 4), (5, 6)])
        self.assertIn("[5] III) LAS REGLAS DEL TRANSITO", llm.prompts[0])     # párrafos numerados
        self.assertIn("Límite duro", llm.prompts[0])

    def test_retries_once_with_the_error(self):
        llm = _ScriptedLLM({"lecciones": [_lec(0, 2, "A"), _lec(4, 6, "B")]},
                           {"lecciones": [_lec(0, 3, "A"), _lec(4, 6, "B")]})
        grupos = segment_chapter_llm("C", CAP6, "media", 1200, client=llm, model="m")
        self.assertEqual(len(grupos), 2)
        self.assertIn("inválida", llm.prompts[1])
        self.assertIn("hueco", llm.prompts[1])

    def test_truncated_answer_retries_with_bigger_budget(self):
        from content_pipeline.llm.client import LLMResponse
        budgets = []
        class _Thinker(_ScriptedLLM):
            def complete_meta(self, **kw):
                budgets.append(kw["max_tokens"])
                if len(budgets) == 1:
                    return LLMResponse("", stop_reason="max_tokens")      # gastó todo razonando
                return LLMResponse(json.dumps({"lecciones": [_lec(0, 6, "Normas")]}), stop_reason="end_turn")
        grupos = segment_chapter_llm("C", CAP6, "media", 1200, client=_Thinker(), model="m")
        self.assertEqual(len(grupos), 1)
        self.assertGreater(budgets[1], budgets[0])                          # más presupuesto (con tope)

    def test_returns_none_after_two_unreadable_answers(self):
        llm = _ScriptedLLM("esto no es json", "tampoco")
        self.assertIsNone(segment_chapter_llm("C", CAP6, "media", 1200, client=llm, model="m"))

    def test_second_answer_with_bad_borders_is_repaired(self):
        # Solape (2-4 sobre 0-2) y hueco final: se hace contiguo y se extiende al final.
        bad = {"lecciones": [_lec(0, 2, "A"), _lec(2, 4, "B")]}
        llm = _ScriptedLLM(bad, bad)
        grupos = segment_chapter_llm("C", CAP6, "media", 1200, client=llm, model="m")
        self.assertEqual([(g.desde, g.hasta, g.titulo) for g in grupos], [(0, 2, "A"), (3, 6, "B")])

    def test_repair_drops_contained_ranges(self):
        grupos = repair_groups([_lec(3, 6, "B"), _lec(0, 4, "A"), _lec(1, 2, "dentro")], 7)
        self.assertEqual([(g.desde, g.hasta, g.titulo) for g in grupos], [(0, 4, "A"), (5, 6, "B")])

    def test_large_chapter_is_processed_in_stitched_windows(self):
        paras = _paras(*[("palabra " * 1000).strip() + "." for _ in range(12)])   # 12k palabras
        # 1ª ventana (8 párrafos): se descarta su última lección y la 2ª ventana arranca en 6.
        llm = _ScriptedLLM(
            {"lecciones": [_lec(0, 2, "A"), _lec(3, 5, "B"), _lec(6, 7, "C")]},
            {"lecciones": [_lec(0, 2, "C completa"), _lec(3, 5, "D")]},
        )
        grupos = segment_chapter_llm("C", paras, "media", 99999, client=llm, model="m")
        self.assertEqual([(g.desde, g.hasta, g.titulo) for g in grupos],
                         [(0, 2, "A"), (3, 5, "B"), (6, 8, "C completa"), (9, 11, "D")])
        self.assertIn("[0] palabra", llm.prompts[1])                         # reindexado por ventana

    def test_units(self):
        llm = _ScriptedLLM({"unidades": [{"desde": 0, "hasta": 4, "nombre": "Señales"},
                                         {"desde": 5, "hasta": 6, "nombre": "Reglas del tránsito"}]})
        grupos = segment_units_llm(CAP6, client=llm, model="m")
        self.assertEqual([g.titulo for g in grupos], ["Señales", "Reglas del tránsito"])


class MergeTinyTests(SimpleTestCase):
    def test_tiny_lesson_joins_its_neighbour_without_exceeding_cap(self):
        from content_pipeline.services.plan_structure import Grupo, merge_tiny
        paras = _paras(("a " * 200).strip(), ("b " * 30).strip(), ("c " * 200).strip(), ("d " * 1100).strip())
        grupos = [Grupo(0, 0, "Tema A", ["A"]), Grupo(1, 1, "Mini", ["B"]),
                  Grupo(2, 2, "Tema C"), Grupo(3, 3, "Tema D")]
        out = merge_tiny(grupos, paras, minimo=125, tope=1200)
        self.assertEqual([(g.desde, g.hasta, g.titulo) for g in out],
                         [(0, 1, "Tema A"), (2, 2, "Tema C"), (3, 3, "Tema D")])
        self.assertEqual(out[0].temas, ["A", "B"])


class ColonBorderTests(SimpleTestCase):
    def test_paragraph_introducing_a_list_moves_to_next_lesson(self):
        from content_pipeline.services.plan_structure import Grupo, fix_colon_borders
        # La 1ª lección cierra con "2) Semáforos:" (índice 3) → pasa a la 2ª.
        out = fix_colon_borders([Grupo(0, 3, "A"), Grupo(4, 6, "B")], CAP6)
        self.assertEqual([(g.desde, g.hasta) for g in out], [(0, 2), (3, 6)])


class BuildPlanWithSegmenterTests(SimpleTestCase):
    def _chapter(self, paras=CAP6_BIG):
        return Chapter(titulo="Normas de circulación", page_start=1, page_end=7, paras=list(paras))

    def _plan(self, llm, chapters, source="dummy"):
        seg = LLMSegmenter(client=llm, model="m", pausa=0)
        with patch.object(cp, "build_outline", return_value=chapters), \
             patch.object(cp, "annotate_plan_visuals", return_value={}):
            return cp.build_plan(source, nombre="Curso B", codigo="B", largo="media", segmentador=seg)

    def test_lessons_keep_exact_source_and_ia_metadata(self):
        llm = _ScriptedLLM({"lecciones": [
            _lec(0, 4, "Señales de carabineros y semáforos", temas=["Semáforos"], densidad="alta", motivo="m"),
            _lec(5, 6, "La obligación de ceder el paso"),
        ]})
        plan = self._plan(llm, [self._chapter(), self._chapter()], source=["a.docx", "b.docx"])
        lecs = plan["unidades"][0]["lecciones"]
        self.assertEqual([l["nombre"] for l in lecs],
                         ["Señales de carabineros y semáforos", "La obligación de ceder el paso"])
        self.assertEqual(lecs[0]["texto"], "\n\n".join(p.text for p in CAP6_BIG[:5]))   # procedencia 1:1
        self.assertEqual((lecs[0]["corte"], lecs[0]["densidad"], lecs[0]["temas"]), ("ia", "alta", ["Semáforos"]))
        self.assertEqual(lecs[1]["paginas"], [6, 7])
        self.assertTrue(all(l.get("id") for l in lecs))
        # 2º capítulo: el cliente ya no tiene respuestas → la IA falla → corte por palabras.
        self.assertEqual(plan["unidades"][1]["lecciones"][0]["corte"], "palabras")
        est = plan["resumen"]["estructura"]
        self.assertEqual((est["modo"], est["capitulos_ia"], est["fallback"]), ("ia", 1, ["Normas de circulación"]))
        self.assertIn("error del LLM", est["errores"]["Normas de circulación"])

    def test_transient_llm_error_retries_the_chapter(self):
        class _Flaky(_ScriptedLLM):
            def complete(self, **kw):
                if not self.prompts:
                    self.prompts.append("caída")
                    raise RuntimeError("529 overloaded")
                return super().complete(**kw)
        llm = _Flaky({"lecciones": [_lec(0, 6, "Normas")]})
        plan = self._plan(llm, [self._chapter()], source=["a"])
        self.assertEqual(plan["unidades"][0]["lecciones"][0]["corte"], "ia")
        self.assertEqual(plan["resumen"]["estructura"]["errores"], {})

    def test_lesson_over_word_cap_is_subsplit(self):
        big = _paras(*[("x " * 500).strip() for _ in range(4)])             # 2000 palabras > 1200
        llm = _ScriptedLLM({"lecciones": [_lec(0, 3, "Tema largo")]})
        plan = self._plan(llm, [self._chapter(big), self._chapter(big)], source=["a", "b"])
        lecs = plan["unidades"][0]["lecciones"]
        self.assertGreater(len(lecs), 1)
        self.assertTrue(all(l["corte"] == "tope" and l["palabras_fuente"] <= 1200 for l in lecs))
        self.assertTrue(lecs[0]["nombre"].startswith("Tema largo — parte 1"))

    def test_single_unformatted_book_gets_units_from_ia(self):
        whole = self._chapter()
        llm = _ScriptedLLM(
            {"unidades": [{"desde": 0, "hasta": 4, "nombre": "Señales"},
                          {"desde": 5, "hasta": 6, "nombre": "Reglas del tránsito"}]},
            {"lecciones": [_lec(0, 4, "Señales de carabineros y semáforos")]},
            {"lecciones": [_lec(0, 1, "Ceder el paso")]},
        )
        with patch.object(cp, "_is_single_file", return_value=True):
            plan = self._plan(llm, [whole])
        self.assertEqual([u["nombre"] for u in plan["unidades"]], ["Señales", "Reglas del tránsito"])
        self.assertTrue(plan["resumen"]["estructura"]["unidades_ia"])
        self.assertEqual(plan["unidades"][1]["lecciones"][0]["texto"], "\n\n".join(p.text for p in CAP6_BIG[5:]))

    def test_without_segmenter_cuts_by_words(self):
        with patch.object(cp, "build_outline", return_value=[self._chapter()]), \
             patch.object(cp, "annotate_plan_visuals", return_value={}):
            plan = cp.build_plan("dummy", nombre="Curso B", codigo="B")
        self.assertEqual(plan["resumen"]["estructura"], {"modo": "palabras"})
        self.assertEqual(plan["unidades"][0]["lecciones"][0]["corte"], "palabras")


class EnrichKeepsIaTitlesTests(SimpleTestCase):
    def test_names_not_overwritten_for_ia_cuts(self):
        from content_pipeline.services.plan_enrich import enrich_plan
        plan = {"unidades": [{"orden": 1, "nombre": "U", "lecciones": [
            {"nombre": "Semáforos", "corte": "ia", "texto": "x"}]}]}
        llm = _ScriptedLLM({"1": "Señales de Tránsito"})        # solo la llamada de categorías
        enrich_plan(plan, client=llm, model="m")
        self.assertEqual(plan["unidades"][0]["lecciones"][0]["nombre"], "Semáforos")
        self.assertEqual(len(llm.prompts), 1)
