"""Tests de la restricción: los cursos no pueden ser gratis.

El precio vive en PlanCurso (fuente única). Al crear/generar un curso se exige
`precio_unitario` > 0, que el backend convierte en el PlanCurso de 7 días.

Cubre las dos vías de creación:
  - CRUD clásico (CursoViewSet / CursoSerializer).
  - Generador automatizado (CourseGenerateView) — valida antes del pipeline.

Correr:  manage.py test schools.tests_curso_precio --settings=autotestAPI.settings.test
"""
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.tests import make_user
from schools.models import Curso, PlanCurso


class CursoPrecioCrudTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin@e.com", is_admin=True)
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _payload(self, **over):
        base = {"nombre": "Curso", "codigo": "C1", "descripcion": "d", "precio_unitario": 5000}
        base.update(over)
        return base

    def test_crear_con_precio_positivo_crea_plan_7(self):
        r = self.client.post("/api/v1/schools/courses/", self._payload(), format="json")
        self.assertEqual(r.status_code, 201, r.data)
        curso = Curso.objects.get(codigo="C1")
        plan = PlanCurso.objects.get(curso=curso, dias=7)
        self.assertEqual(plan.precio, 5000)
        # El precio unitario se refleja en la respuesta.
        self.assertEqual(r.data.get("precio_unitario"), 5000)

    def test_crear_con_precio_cero_rechazado(self):
        r = self.client.post("/api/v1/schools/courses/", self._payload(precio_unitario=0), format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("precio_unitario", r.data)

    def test_crear_sin_precio_rechazado(self):
        p = self._payload()
        p.pop("precio_unitario")
        r = self.client.post("/api/v1/schools/courses/", p, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("precio_unitario", r.data)

    def test_editar_precio_actualiza_plan_7(self):
        self.client.post("/api/v1/schools/courses/", self._payload(), format="json")
        curso = Curso.objects.get(codigo="C1")
        r = self.client.patch(
            f"/api/v1/schools/courses/{curso.id}/", {"precio_unitario": 9990}, format="json"
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(PlanCurso.objects.get(curso=curso, dias=7).precio, 9990)


class CursoPrecioGeneradorTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin2@e.com", is_admin=True)
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _files(self):
        return {
            "temario": SimpleUploadedFile("t.pdf", b"%PDF-1.4 fake", content_type="application/pdf"),
            "contenido": SimpleUploadedFile("c.pdf", b"%PDF-1.4 fake", content_type="application/pdf"),
        }

    def test_generar_con_precio_cero_rechazado_antes_del_pipeline(self):
        data = {**self._files(), "nombre": "Curso", "codigo": "GEN", "precio_unitario": 0}
        r = self.client.post("/api/v1/schools/courses/generate/", data, format="multipart")
        self.assertEqual(r.status_code, 400)

    def test_generar_sin_precio_rechazado(self):
        data = {**self._files(), "nombre": "Curso", "codigo": "GEN"}
        r = self.client.post("/api/v1/schools/courses/generate/", data, format="multipart")
        self.assertEqual(r.status_code, 400)
