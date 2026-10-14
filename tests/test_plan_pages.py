"""Páginas del libro para un plan sin páginas (alineación) y su traspaso al curso generado."""
from __future__ import annotations

import io
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from content_pipeline.processors.outline import Chapter, Para
from content_pipeline.services import plan_pages as pp

# Capítulo "Normas" = páginas 10-15 del libro; "Alcohol" = 20-21.
PAGINAS = {
    10: "Los semáforos regulan el cruce: luz roja detenerse, luz verde avanzar, luz amarilla prevención.",
    11: "El carabinero dirige el tránsito con señales de brazos; sus indicaciones prevalecen sobre el semáforo.",
    12: "El adelantamiento se hace por la izquierda, con visibilidad suficiente y sin exceder la velocidad.",
    13: "Al adelantar revise espejos, señalice y vuelva a su pista dejando distancia con el vehículo adelantado.",
    14: "Está prohibido estacionar frente a grifos, en pasos de cebra y a menos de diez metros de una esquina.",
    15: "Para estacionar en pendiente gire las ruedas hacia la solera y aplique el freno de mano.",
    20: "La alcoholemia mide los gramos de alcohol por litro de sangre; bajo la influencia y estado de ebriedad.",
    21: "Las sanciones por alcohol incluyen multa, suspensión de licencia y presidio.",
}
CHAPTERS = [
    Chapter(titulo="Normas de circulación", page_start=10, page_end=15, paras=[Para(text="x", page=10)]),
    Chapter(titulo="Alcohol en la conducción", page_start=20, page_end=21, paras=[Para(text="x", page=20)]),
]


def _plan():
    def lec(i, nombre, texto):
        return {"id": f"L-{i}", "nombre": nombre, "paginas": [1, 1], "texto": texto}
    return {"curso": {"codigo": "B"}, "unidades": [
        {"orden": 1, "nombre": "NORMAS DE CIRCULACION", "paginas": [1, 1], "lecciones": [
            lec(1, "Semáforos y carabineros", "Semáforo: luz roja detenerse, luz verde avanzar. El carabinero prevalece."),
            lec(2, "Adelantamiento", "Adelantar por la izquierda con visibilidad; revisar espejos y señalizar."),
            lec(3, "Estacionamiento", "Prohibido estacionar frente a grifos; en pendiente girar las ruedas a la solera."),
        ]},
        {"orden": 2, "nombre": "ALCOHOL EN LA CONDUCCION", "paginas": [1, 1], "lecciones": [
            lec(4, "Alcoholemia y sanciones", "Alcoholemia en gramos por litro; multa y suspensión de licencia.")]},
        {"orden": 3, "nombre": "TEMA QUE NO ESTA EN EL LIBRO", "paginas": [1, 1], "lecciones": [
            lec(5, "Extra", "Contenido propio de la guía.")]},
        {"orden": 4, "nombre": "Anexo: Señales de tránsito", "paginas": [149, 160], "lecciones": [
            {"id": "L-x", "nombre": "Señales", "anexo": True, "paginas": [149, 160], "texto": "PARE"}]},
    ]}


def _fake_pages(_pdf, desde, hasta):
    return [PAGINAS[p] for p in range(desde, hasta + 1)]


class AlignPagesTests(SimpleTestCase):
    def _align(self, plan, **kw):
        with patch.object(pp, "build_outline", return_value=CHAPTERS), patch.object(pp, "page_texts", _fake_pages):
            return pp.align_plan_pages(plan, "libro.pdf", **kw)

    def test_lessons_get_contiguous_ordered_book_pages(self):
        plan = _plan()
        textos = [l["texto"] for u in plan["unidades"] for l in u["lecciones"]]
        res = self._align(plan, source_name="Libro oficial")
        normas = plan["unidades"][0]
        self.assertEqual([l["paginas"] for l in normas["lecciones"]], [[10, 11], [12, 13], [14, 15]])
        self.assertEqual(normas["paginas"], [10, 15])
        self.assertEqual(plan["unidades"][1]["lecciones"][0]["paginas"], [20, 21])
        self.assertTrue(all(l["paginas_origen"] == "alineacion" and 0 < l["paginas_afinidad"] <= 1
                            for l in normas["lecciones"]))
        # Unidad sin capítulo y anexo: sin cambio; textos intactos; fuente registrada.
        self.assertEqual(plan["unidades"][2]["lecciones"][0]["paginas"], [1, 1])
        self.assertEqual(plan["unidades"][3]["lecciones"][0]["paginas"], [149, 160])
        self.assertEqual(res["unidades_sin_capitulo"], ["TEMA QUE NO ESTA EN EL LIBRO"])
        self.assertEqual(textos, [l["texto"] for u in plan["unidades"] for l in u["lecciones"]])
        self.assertEqual(plan["curso"]["fuente_nombre"], "Libro oficial")

    def test_neighbours_may_share_the_border_page_and_fewer_pages_than_lessons(self):
        tramos = pp.align_lessons_to_pages(
            ["semáforo luz roja verde", "carabinero brazos señales tránsito prevalecen"],
            ["semáforo luz roja verde carabinero brazos señales tránsito prevalecen"])
        self.assertEqual([(a, b) for a, b, _s in tramos], [(0, 0), (0, 0)])
        self.assertEqual(pp.align_lessons_to_pages([], ["x"]), [])


class SyncCoursePagesTests(SimpleTestCase):
    def test_updates_sources_and_fuente_line_only(self):
        from schools.management.commands.sync_course_pages import sync_course_pages
        plan = {"curso": {"fuente_nombre": "Libro oficial"}, "unidades": [
            {"orden": 1, "paginas": [10, 15], "lecciones": [{"id": "L-1", "paginas": [10, 11]}]}]}
        cuerpo = "# T\n\n## Desarrollo\nTexto con {{figura:F-a}}.\n\n## Fuente\nLibro: Curso B, páginas 1-1.\n"
        course = {"lessons": [
            {"unidad_orden": 1, "tipo": "texto", "nombre": "A", "plan_id": "L-1", "contenido": cuerpo,
             "recursos": [{"clave": "F-a"}], "fuentes": [{"fuente_nombre": "Libro: Curso B", "pagina_inicio": 1, "pagina_fin": 1}]},
            {"unidad_orden": 1, "tipo": "quiz", "nombre": "Q", "contenido": {"questions": []},
             "fuentes": [{"fuente_nombre": "Libro: Curso B", "pagina_inicio": 1, "pagina_fin": 1}]},
            {"unidad_orden": 1, "tipo": "texto", "nombre": "Huérfana", "plan_id": "L-zz", "contenido": cuerpo, "fuentes": []},
        ]}
        res = sync_course_pages(plan, course)
        a, q, h = course["lessons"]
        self.assertEqual(a["fuentes"][0], {"fuente_nombre": "Libro oficial", "pagina_inicio": 10, "pagina_fin": 11})
        self.assertTrue(a["contenido"].endswith("## Fuente\nLibro oficial, páginas 10-11.\n"))
        self.assertIn("Texto con {{figura:F-a}}.", a["contenido"])
        self.assertEqual(a["recursos"], [{"clave": "F-a"}])
        self.assertEqual((q["fuentes"][0]["pagina_inicio"], q["fuentes"][0]["pagina_fin"]), (10, 15))
        self.assertEqual(h["contenido"], cuerpo)
        self.assertEqual(res, {"lecciones": 1, "quizzes": 1, "sin_plan": ["Huérfana"]})


class SoloPaginasCommandTests(SimpleTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_solo_paginas_needs_no_ia_and_keeps_figures(self):
        from django.core.management import call_command
        plan = _plan()
        plan["unidades"][0]["lecciones"][0]["figuras"] = [{"id": "F-a", "parrafo": 0, "archivo": "figuras_B/a.png"}]
        plan_p, pdf, out = self.tmp / "plan.json", self.tmp / "libro.pdf", self.tmp / "out.json"
        plan_p.write_text(json.dumps(plan), encoding="utf-8")
        pdf.write_bytes(b"%PDF-1.4")
        with patch.object(pp, "build_outline", return_value=CHAPTERS), patch.object(pp, "page_texts", _fake_pages), \
             patch("content_pipeline.llm.client.LLMClient.is_available", return_value=False):
            call_command("attach_figures", plan=str(plan_p), contenido=str(pdf), out=str(out), solo_paginas=True,
                         stdout=io.StringIO(), stderr=io.StringIO())
        res = json.loads(out.read_text(encoding="utf-8"))
        lec = res["unidades"][0]["lecciones"][0]
        self.assertEqual(lec["paginas"], [10, 11])
        self.assertEqual(lec["figuras"], [{"id": "F-a", "parrafo": 0, "archivo": "figuras_B/a.png"}])
        self.assertEqual(res["resumen"]["paginas"]["lecciones"], 4)
