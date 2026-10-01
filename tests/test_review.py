from __future__ import annotations

import copy
import json
import tempfile
import zipfile
from pathlib import Path

from django.core.management import call_command
from django.test import SimpleTestCase

from content_pipeline.review.apply import apply_review, merge_reviews, split_segments
from content_pipeline.review.ids import course_key, ensure_plan_ids, lesson_id


def _plan():
    def lec(texto, pags, nombre):
        return {"nombre": nombre, "seccion": None, "paginas": pags, "palabras_objetivo": 1200,
                "palabras_fuente": len(texto.split()), "texto": texto}
    return ensure_plan_ids({
        "curso": {"nombre": "Curso B", "codigo": "B"},
        "unidades": [
            {"orden": 1, "nombre": "Frenos", "categoria": "General", "paginas": [1, 4], "palabras": 0,
             "lecciones": [lec("Uno dos tres.\n\nCuatro cinco seis siete.", [1, 3], "Frenos — parte 1"),
                           lec("Ocho nueve.", [3, 4], "Frenos — parte 2")]},
            {"orden": 2, "nombre": "Peatones", "categoria": "General", "paginas": [5, 6], "palabras": 0,
             "lecciones": [lec("Diez once doce.", [5, 6], "Peatones")]},
        ],
        "resumen": {"banda": {"largo": "media", "min": 700, "objetivo": 950, "max": 1200}},
    })


def _item(fase, target, cambio, estado="aceptada", idx=1):
    return {"id": f"r-{idx}", "autor": "Ana", "fecha": f"2026-09-29T10:00:0{idx}Z", "fase": fase,
            "target": target, "tipo": "propuesta", "severidad": "menor", "motivo": "",
            "cambio": cambio, "estado": estado}


def _course():
    return {
        "manifest": {"curso": {"codigo": "B"}, "unidades": [{"orden": 1, "nombre": "Frenos"}]},
        "lessons": [
            {"unidad_orden": 1, "posicion": 1, "nombre": "Frenos", "tipo": "texto", "plan_id": "L-abc",
             "contenido": "La distancia de frenado es de 50 metros a 60 km/h."},
            {"unidad_orden": 1, "posicion": 2, "nombre": "Evaluación", "tipo": "quiz",
             "contenido": {"questions": [{"question": "¿Qué es?", "options": ["a", "b", "c"],
                                          "correct_index": 0, "explanation": "Porque sí."}]}},
        ],
    }


class IdsTests(SimpleTestCase):
    def test_ids_are_deterministic_and_unique(self):
        a, b = _plan(), _plan()
        ids = [l["id"] for u in a["unidades"] for l in u["lecciones"]]
        self.assertEqual(ids, [l["id"] for u in b["unidades"] for l in u["lecciones"]])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids[0], lesson_id("Uno dos tres.\n\nCuatro cinco seis siete."))
        self.assertTrue(all(u["id"].startswith("U-") for u in a["unidades"]))

    def test_duplicate_texts_get_suffix(self):
        plan = {"unidades": [{"nombre": "X", "lecciones": [{"texto": "igual"}, {"texto": "igual"}]}]}
        ensure_plan_ids(plan)
        l1, l2 = plan["unidades"][0]["lecciones"]
        self.assertEqual(l2["id"], l1["id"] + "~2")

    def test_course_key(self):
        self.assertEqual(course_key({"unidad_orden": 3, "posicion": 2}), "U3.2")


class StructureReviewTests(SimpleTestCase):
    def setUp(self):
        self.plan = _plan()
        self.u1, self.u2 = self.plan["unidades"]
        self.l1, self.l2 = self.u1["lecciones"]

    def _apply(self, *items):
        return apply_review(self.plan, list(items), fase="estructura")

    def test_rename_and_categoria(self):
        res = self._apply(
            _item("estructura", {"tipo": "leccion", "id": self.l1["id"]}, {"op": "rename", "args": {"nombre": "ABS"}}, idx=1),
            _item("estructura", {"tipo": "unidad", "id": self.u1["id"]}, {"op": "categoria", "args": {"categoria": "Conducción Defensiva"}}, idx=2),
        )
        u = res["data"]["unidades"][0]
        self.assertEqual(u["lecciones"][0]["nombre"], "ABS")
        self.assertEqual(u["categoria"], "Conducción Defensiva")
        self.assertEqual(len(res["aplicadas"]), 2)
        self.assertEqual(self.plan["unidades"][0]["lecciones"][0]["nombre"], "Frenos — parte 1")  # original intacto

    def test_split_matches_segments_and_pages(self):
        res = self._apply(_item("estructura", {"tipo": "leccion", "id": self.l1["id"]}, {"op": "split", "args": {"k": 1}}))
        a, b = res["data"]["unidades"][0]["lecciones"][:2]
        self.assertEqual(a["texto"], "Uno dos tres.")
        self.assertEqual(b["nombre"], "Frenos (cont.)")
        self.assertEqual((a["id"], b["id"]), (self.l1["id"] + "-a", self.l1["id"] + "-b"))
        self.assertEqual(a["paginas"][0], 1)
        self.assertEqual(b["paginas"][1], 3)
        self.assertEqual(res["data"]["resumen"]["lecciones"], 4)

    def test_merge_move_delete_and_renumber(self):
        res = self._apply(
            _item("estructura", {"tipo": "leccion", "id": self.l1["id"]}, {"op": "merge", "args": {"con": self.l2["id"]}}, idx=1),
            _item("estructura", {"tipo": "leccion", "id": self.u2["lecciones"][0]["id"]},
                  {"op": "move", "args": {"unidad_id": self.u1["id"]}}, idx=2),
        )
        units = res["data"]["unidades"]
        self.assertEqual(len(units), 1)  # la unidad 2 quedó vacía y se eliminó
        self.assertEqual([l["nombre"] for l in units[0]["lecciones"]], ["Frenos", "Peatones"])
        self.assertEqual(units[0]["paginas"], [1, 6])

    def test_new_unit_and_move_unit(self):
        res = self._apply(
            _item("estructura", {"tipo": "leccion", "id": self.l2["id"]}, {"op": "new_unit"}, idx=1),
            _item("estructura", {"tipo": "unidad", "id": self.u2["id"]}, {"op": "move_unit", "args": {"dir": -1}}, idx=2),
        )
        self.assertEqual([u["nombre"] for u in res["data"]["unidades"]], ["Frenos", "Peatones", "Frenos"])
        self.assertEqual([u["orden"] for u in res["data"]["unidades"]], [1, 2, 3])

    def test_only_accepted_items_apply_and_conflicts_reported(self):
        res = self._apply(
            _item("estructura", {"tipo": "leccion", "id": self.l1["id"]}, {"op": "delete"}, estado="pendiente", idx=1),
            _item("estructura", {"tipo": "leccion", "id": "L-noexiste"}, {"op": "delete"}, idx=2),
        )
        self.assertEqual(res["ignoradas"], 1)
        self.assertEqual(len(res["conflictos"]), 1)
        self.assertEqual(sum(len(u["lecciones"]) for u in res["data"]["unidades"]), 3)
        self.assertEqual(res["data"]["revision_humana"][-1]["conflictos"][0]["id"], "r-2")

    def test_texto_op_cleans_source_and_keeps_id(self):
        res = self._apply(_item("estructura", {"tipo": "leccion", "id": self.l1["id"]},
                                {"op": "texto", "args": {"buscar": "Uno dos tres.", "reemplazar": "Uno."}}))
        lec = res["data"]["unidades"][0]["lecciones"][0]
        self.assertTrue(lec["texto"].startswith("Uno.\n\n"))
        self.assertEqual((lec["id"], lec["palabras_fuente"]), (self.l1["id"], 5))

    def test_split_segments_falls_back_to_sentences(self):
        self.assertEqual(split_segments("Hola. Chao. Fin"), ["Hola.", "Chao.", "Fin"])


class ContentReviewTests(SimpleTestCase):
    def test_replace_once_and_quiz_changes(self):
        items = [
            _item("contenido", {"tipo": "leccion", "clave": "U1.1", "nombre": "Frenos"},
                  {"campo": "contenido", "buscar": "50 metros", "reemplazar": "36 metros"}, idx=1),
            _item("contenido", {"tipo": "pregunta", "clave": "U1.2", "nombre": "Evaluación", "q": 0},
                  {"campo": "correcta", "valor": 2}, idx=2),
            _item("contenido", {"tipo": "pregunta", "clave": "U1.2", "nombre": "Evaluación", "q": 0},
                  {"campo": "opcion", "i": 1, "valor": "b corregida"}, idx=3),
        ]
        res = apply_review(_course(), items, fase="contenido")
        lessons = res["data"]["lessons"]
        self.assertIn("36 metros", lessons[0]["contenido"])
        q = lessons[1]["contenido"]["questions"][0]
        self.assertEqual((q["correct_index"], q["options"][1]), (2, "b corregida"))
        self.assertEqual(res["conflictos"], [])

    def test_ambiguous_or_missing_text_is_conflict(self):
        course = _course()
        course["lessons"][0]["contenido"] += " Otra vez 50 metros."
        items = [
            _item("contenido", {"tipo": "leccion", "clave": "U1.1"}, {"campo": "contenido", "buscar": "50 metros", "reemplazar": "x"}, idx=1),
            _item("contenido", {"tipo": "leccion", "clave": "U1.1"}, {"campo": "contenido", "buscar": "no está", "reemplazar": "x"}, idx=2),
        ]
        res = apply_review(course, items, fase="contenido")
        self.assertEqual(len(res["conflictos"]), 2)
        self.assertIn("2 veces", res["conflictos"][0]["motivo"])
        self.assertEqual(res["data"]["lessons"][0]["contenido"], course["lessons"][0]["contenido"])

    def test_lookup_by_plan_id_survives_renumbering(self):
        course = _course()
        course["lessons"][0]["posicion"] = 9
        item = _item("contenido", {"tipo": "leccion", "id": "L-abc", "clave": "U1.1"},
                     {"campo": "contenido", "buscar": "60 km/h", "reemplazar": "60 km/h en seco"})
        res = apply_review(course, [item], fase="contenido")
        self.assertIn("en seco", res["data"]["lessons"][0]["contenido"])

    def test_merge_reviews_last_wins(self):
        a = {"items": [_item("contenido", {}, None, estado="pendiente", idx=1)]}
        b = {"items": [_item("contenido", {}, None, estado="aceptada", idx=1)]}
        self.assertEqual(merge_reviews([a, b])[0]["estado"], "aceptada")


class ReviewCommandTests(SimpleTestCase):
    def test_apply_review_and_build_brujula_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            plan = _plan()
            (tmp / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            lid = plan["unidades"][0]["lecciones"][0]["id"]
            review = {"version": 1, "curso": "B", "items": [
                _item("estructura", {"tipo": "leccion", "id": lid}, {"op": "rename", "args": {"nombre": "ABS"}})]}
            (tmp / "review.json").write_text(json.dumps(review), encoding="utf-8")
            call_command("apply_review", "--review", str(tmp / "review.json"), "--plan", str(tmp / "plan.json"),
                         "--out", str(tmp / "plan_rev.json"), stdout=open(tmp / "log.txt", "w", encoding="utf-8"))
            out = json.loads((tmp / "plan_rev.json").read_text(encoding="utf-8"))
            self.assertEqual(out["unidades"][0]["lecciones"][0]["nombre"], "ABS")

            del plan["unidades"][0]["lecciones"][0]["id"]  # plan viejo sin ids → build_brujula los rellena
            (tmp / "old.json").write_text(json.dumps(plan), encoding="utf-8")
            call_command("build_brujula", "--plan", str(tmp / "old.json"), "--out", str(tmp / "b.zip"),
                         stdout=open(tmp / "log2.txt", "w", encoding="utf-8"))
            with zipfile.ZipFile(tmp / "b.zip") as zf:
                self.assertEqual(sorted(zf.namelist()), ["LEEME.txt", "brujula.html"])
                html = zf.read("brujula.html").decode("utf-8")
            self.assertTrue(html.startswith("<!doctype html>"))
            self.assertIn('id="bundle"', html)
            self.assertIn(lid, html)
            self.assertIn('"modo": "auditor"', html)
