"""Endpoint web en el flujo del plan: planificar → generar desde plan → importar."""
from __future__ import annotations

import io
import json
import zipfile
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Usuario
from content_pipeline.llm.client import LLMResponse
from schools.models import Curso

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx_book() -> io.BytesIO:
    """DOCX mínimo: 3 capítulos (Heading1) con párrafos de cuerpo."""
    def p(text, style=None):
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"
    body = ""
    for n, tema in enumerate(["Normas de circulación", "Señales de tránsito", "Conducción segura"], start=1):
        body += p(f"Capítulo {n} {tema}", "Heading1")
        body += p(f"El límite urbano es 50 km/h y la regla es mantener la derecha en {tema.lower()}.")
        body += p("Debes respetar la señalización y ceder el paso a los peatones en los cruces.")
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    buf.seek(0)
    buf.name = "libro.docx"
    return buf


CORE_BODY = "# T\n\n## Objetivo\nx\n\n## Desarrollo\ny\n\n## Puntos clave\n- z\n\n## Resumen\nw"


class _Meter:
    calls = 0
    cost_usd = 0.0

    def as_dict(self):
        return {"modelos": ["fake"], "llamadas": 0, "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_creation_tokens": 0, "costo_usd": 0.0}


class _FakeLLM:
    """Redacta, genera quiz con evidencia real y juzga según ``faithfulness``."""

    def __init__(self, faithfulness=1.0):
        self.meter = _Meter()
        self.faithfulness = faithfulness

    def complete_meta(self, **kw):
        if "evaluador" in kw["system"]:
            q = {"question": "¿Cuál es el límite urbano?", "options": ["50 km/h", "60 km/h", "40 km/h", "80 km/h"],
                 "correct_index": 0, "explanation": "", "evidencia": "El límite urbano es 50 km/h"}
            return LLMResponse(json.dumps({"questions": [q]}), stop_reason="end_turn")
        return LLMResponse(CORE_BODY, stop_reason="end_turn")

    def complete(self, **kw):
        if "auditor de evaluaciones" in kw["system"]:
            return json.dumps({"items": [{"n": 1, "ok": True}]})
        return json.dumps({"faithfulness": self.faithfulness,
                           "unsupported_claims": [] if self.faithfulness >= 0.7 else ["consejo inventado"],
                           "coverage": 1.0, "omissions": []})


def _plan():
    return {"curso": {"nombre": "Curso Web", "codigo": "WEB"}, "unidades": [{
        "orden": 1, "nombre": "Normas", "categoria": "General", "paginas": [1, 2],
        "lecciones": [{"id": "L-1", "nombre": "Velocidad urbana", "paginas": [1, 2], "palabras_objetivo": 900,
                       "texto": "El límite urbano es 50 km/h. Mantén la derecha."}]}]}


def _events(response):
    raw = b"".join(response.streaming_content).decode("utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


class CourseWebPlanFlowTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = Usuario.objects.create_user(email="admin@example.com", nombre="Ad", apellido="Min",
                                                 password="secret123")
        self.admin.is_staff = True
        self.admin.is_active = True
        self.admin.save(update_fields=["is_staff", "is_active"])
        self.client.force_authenticate(self.admin)

    def _generate(self, fake, **extra):
        body = {"plan": _plan(), "nombre": "Curso Web", "codigo": "WEB", "precio_unitario": 19990, **extra}
        with mock.patch("content_pipeline.services.plan_generation.LLMClient") as cls:
            cls.return_value = fake
            cls.is_available.return_value = True
            res = self.client.post("/api/v1/schools/courses/generate/", body, format="json")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res["Content-Type"], "application/x-ndjson")
            return _events(res)

    # --- planificar -------------------------------------------------------------
    def test_plan_endpoint_returns_plan_without_redaction(self):
        res = self.client.post("/api/v1/schools/courses/plan/",
                               {"contenido": _docx_book(), "nombre": "Curso Web", "codigo": "web", "largo": "corta"},
                               format="multipart")
        self.assertEqual(res.status_code, 200, res.content)
        plan = res.json()["plan"]
        self.assertEqual(len(plan["unidades"]), 3)
        self.assertTrue(all(l.get("id") and l.get("texto") for u in plan["unidades"] for l in u["lecciones"]))
        self.assertEqual(plan["curso"]["codigo"], "WEB")
        self.assertIsNone(res.json()["ia"])
        self.assertEqual(Curso.objects.count(), 0)                 # nada se persiste al planificar

    def test_plan_endpoint_rejects_other_formats_and_non_admins(self):
        txt = io.BytesIO(b"hola")
        txt.name = "libro.txt"
        res = self.client.post("/api/v1/schools/courses/plan/",
                               {"contenido": txt, "nombre": "X", "codigo": "X"}, format="multipart")
        self.assertEqual(res.status_code, 400)
        estudiante = Usuario.objects.create_user(email="e@example.com", nombre="E", apellido="S",
                                                 password="secret123", is_estudiante=True)
        self.client.force_authenticate(estudiante)
        res = self.client.post("/api/v1/schools/courses/plan/",
                               {"contenido": _docx_book(), "nombre": "X", "codigo": "X"}, format="multipart")
        self.assertEqual(res.status_code, 403)

    # --- generar desde plan -------------------------------------------------------
    def test_generate_from_plan_imports_when_clean(self):
        events = self._generate(_FakeLLM(faithfulness=1.0))
        kinds = [e["event"] for e in events]
        self.assertIn("lesson", kinds)
        done = next(e for e in events if e["event"] == "done")
        self.assertEqual(done["total"], 2)                          # 1 lección + 1 quiz
        curso = Curso.objects.get(codigo="WEB")
        self.assertEqual(done["curso"]["id"], curso.id)
        self.assertTrue(any(e.get("step") == "juez_ok" for e in events))

    def test_done_event_includes_course_json(self):
        done = next(e for e in self._generate(_FakeLLM()) if e["event"] == "done")
        self.assertEqual(len(done["curso_json"]["lessons"]), 2)
        self.assertIn("auditoria", done["curso_json"])

    def test_generate_only_does_not_touch_db(self):
        events = self._generate(_FakeLLM(), guardar=False)
        gen = next(e for e in events if e["event"] == "generado")
        self.assertEqual(gen["curso_json"]["manifest"]["curso"]["codigo"], "WEB")
        self.assertFalse(any(e["event"] in ("done", "bloqueado") for e in events))
        self.assertEqual(Curso.objects.count(), 0)
        # el JSON generado se puede importar después sin forzar (no tiene bloqueos)
        res = self.client.post("/api/v1/schools/courses/import/", {"curso": gen["curso_json"]}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertFalse(res.json()["forzado"])

    def test_generate_only_still_reports_blockers(self):
        events = self._generate(_FakeLLM(faithfulness=0.3), guardar=False)
        self.assertTrue(any(e["event"] == "bloqueado" for e in events))
        self.assertFalse(any(e["event"] == "generado" for e in events))

    def test_generate_from_plan_blocks_on_critical_judge(self):
        events = self._generate(_FakeLLM(faithfulness=0.3))
        blocked = next(e for e in events if e["event"] == "bloqueado")
        self.assertTrue(any("fidelidad crítica" in b for b in blocked["bloqueos"]))
        self.assertIn("manifest", blocked["curso_json"])
        self.assertFalse(any(e["event"] == "done" for e in events))
        self.assertEqual(Curso.objects.count(), 0)                 # no se importó

    def test_generate_validates_input(self):
        res = self.client.post("/api/v1/schools/courses/generate/",
                               {"plan": {"unidades": []}, "nombre": "X", "codigo": "X", "precio_unitario": 10},
                               format="json")
        self.assertEqual(res.status_code, 400)
        res = self.client.post("/api/v1/schools/courses/generate/",
                               {"plan": _plan(), "nombre": "X", "codigo": "X", "precio_unitario": 0}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_legacy_multipart_mode_still_answers(self):
        res = self.client.post("/api/v1/schools/courses/generate/", {"nombre": "X", "codigo": "X"}, format="multipart")
        self.assertEqual(res.status_code, 400)
        self.assertIn("temario", res.json()["detail"])

    # --- importar -----------------------------------------------------------------
    def test_import_endpoint_requires_forzar_for_blocked_course(self):
        events = self._generate(_FakeLLM(faithfulness=0.3))
        curso_json = next(e for e in events if e["event"] == "bloqueado")["curso_json"]
        res = self.client.post("/api/v1/schools/courses/import/", {"curso": curso_json}, format="json")
        self.assertEqual(res.status_code, 409)
        self.assertTrue(res.json()["bloqueos"])
        res = self.client.post("/api/v1/schools/courses/import/", {"curso": curso_json, "forzar": True}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(res.json()["forzado"])
        self.assertTrue(Curso.objects.filter(codigo="WEB").exists())

    def test_large_plan_body_is_accepted(self):
        plan = _plan()
        plan["unidades"][0]["lecciones"][0]["texto"] += " relleno" * 400_000   # ~3,2 MB de JSON
        with mock.patch("content_pipeline.services.plan_generation.LLMClient") as cls:
            cls.return_value = _FakeLLM()
            cls.is_available.return_value = True
            res = self.client.post("/api/v1/schools/courses/generate/",
                                   {"plan": plan, "nombre": "Curso Web", "codigo": "WEB", "precio_unitario": 19990,
                                    "juez": False}, format="json")
        self.assertEqual(res.status_code, 200)
