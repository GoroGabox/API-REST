"""Tests de la restricción: los cursos no pueden tener costo 0.

Cubre las dos vías de creación:
  - CRUD clásico (CursoViewSet / CursoSerializer).
  - Generador automatizado (CourseGenerateView) — valida antes de correr el
    pipeline.

Correr:  manage.py test schools.tests_curso_costo --settings=autotestAPI.settings.test
"""
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.tests import make_user
from schools.models import Curso


class CursoCostoCrudTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin@e.com", is_admin=True)
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _payload(self, **over):
        base = {"nombre": "Curso", "codigo": "C1", "descripcion": "d", "costo": 5000}
        base.update(over)
        return base

    def test_crear_con_costo_positivo_ok(self):
        r = self.client.post("/api/v1/schools/courses/", self._payload(), format="json")
        self.assertEqual(r.status_code, 201, r.data)

    def test_crear_con_costo_cero_rechazado(self):
        r = self.client.post("/api/v1/schools/courses/", self._payload(costo=0), format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("costo", r.data)

    def test_crear_sin_costo_rechazado(self):
        p = self._payload()
        p.pop("costo")
        r = self.client.post("/api/v1/schools/courses/", p, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("costo", r.data)

    def test_editar_costo_a_cero_rechazado(self):
        curso = Curso.objects.create(nombre="C", descripcion="d", costo=5000, codigo="C2")
        r = self.client.patch(f"/api/v1/schools/courses/{curso.id}/", {"costo": 0}, format="json")
        self.assertEqual(r.status_code, 400)


class CursoCostoGeneradorTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin2@e.com", is_admin=True)
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _files(self):
        return {
            "temario": SimpleUploadedFile("t.pdf", b"%PDF-1.4 fake", content_type="application/pdf"),
            "contenido": SimpleUploadedFile("c.pdf", b"%PDF-1.4 fake", content_type="application/pdf"),
        }

    def test_generar_con_costo_cero_rechazado_antes_del_pipeline(self):
        data = {**self._files(), "nombre": "Curso", "codigo": "GEN", "costo": 0}
        r = self.client.post("/api/v1/schools/courses/generate/", data, format="multipart")
        self.assertEqual(r.status_code, 400)

    def test_generar_sin_costo_rechazado(self):
        data = {**self._files(), "nombre": "Curso", "codigo": "GEN"}
        r = self.client.post("/api/v1/schools/courses/generate/", data, format="multipart")
        self.assertEqual(r.status_code, 400)
