"""Tests de control de acceso al contenido premium de cursos.

Verifica que las lecciones/unidades/ejercicios de un curso solo sean accesibles
para quien tiene acceso VIGENTE (compra/llave/cupo no vencido) o para
admin/director — no para cualquier autenticado ni con acceso expirado.

Correr:  manage.py test schools.tests_access --settings=autotestAPI.settings.test
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.tests import make_user
from schools.models import Curso, Leccion, Ejercicio, Escuela
from sales.models import AccessKey, EstudianteCurso


def make_curso(nombre="Curso", costo=10000):
    return Curso.objects.create(nombre=nombre, descripcion="d", costo=costo, codigo="C")


def enrol(student, curso, dias=30):
    ak = AccessKey.objects.create(
        valid_until=timezone.now() + timedelta(days=dias), origen="purchase", status="active",
    )
    EstudianteCurso.objects.create(estudiante_id=student, curso_id=curso, access_key_id=ak)
    return ak


def enrol_expired(student, curso):
    ak = AccessKey.objects.create(
        valid_until=timezone.now() - timedelta(days=1), origen="purchase", status="active",
    )
    EstudianteCurso.objects.create(estudiante_id=student, curso_id=curso, access_key_id=ak)
    return ak


def _as_list(resp):
    body = resp.json()
    if isinstance(body, dict) and "results" in body:
        return body["results"]
    return body


class ContentAccessGatingTests(TestCase):
    def setUp(self):
        self.escuela = Escuela.objects.create(nombre="E", direccion="x", email="e@e.com", telefono="1")
        self.admin = make_user("admin@e.com", is_admin=True)
        self.director = make_user("dir@e.com", is_director=True, escuela=self.escuela)
        self.owner = make_user("owner@e.com", is_estudiante=True)
        self.stranger = make_user("stranger@e.com", is_estudiante=True)
        self.expired = make_user("expired@e.com", is_estudiante=True)

        self.curso = make_curso()
        self.leccion = Leccion.objects.create(
            curso=self.curso, nombre="L1", posicion=1, tipo="texto", contenido="PREMIUM",
        )
        self.ejercicio = Ejercicio.objects.create(
            curso=self.curso, pregunta="q", opcion_a="A", respuesta="a",
        )
        enrol(self.owner, self.curso)
        enrol_expired(self.expired, self.curso)

    def _lessons(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c.get(f"/api/v1/schools/lessons/?curso={self.curso.id}")

    # --- Lecciones ---
    def test_owner_ve_lecciones_con_contenido(self):
        r = self._lessons(self.owner)
        self.assertEqual(r.status_code, 200)
        data = _as_list(r)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["contenido"], "PREMIUM")

    def test_estudiante_sin_acceso_no_ve_lecciones(self):
        r = self._lessons(self.stranger)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(_as_list(r)), 0)

    def test_acceso_expirado_no_ve_lecciones(self):
        r = self._lessons(self.expired)
        self.assertEqual(len(_as_list(r)), 0)

    def test_admin_y_director_ven_lecciones(self):
        self.assertEqual(len(_as_list(self._lessons(self.admin))), 1)
        self.assertEqual(len(_as_list(self._lessons(self.director))), 1)

    # --- courses/<id>/units/ ---
    def test_units_bloqueado_sin_acceso(self):
        c = APIClient()
        c.force_authenticate(self.stranger)
        r = c.get(f"/api/v1/schools/courses/{self.curso.id}/units/")
        self.assertEqual(r.status_code, 403)

    def test_units_permitido_a_owner(self):
        c = APIClient()
        c.force_authenticate(self.owner)
        r = c.get(f"/api/v1/schools/courses/{self.curso.id}/units/")
        self.assertEqual(r.status_code, 200)

    def test_units_anonimo_401(self):
        r = APIClient().get(f"/api/v1/schools/courses/{self.curso.id}/units/")
        self.assertEqual(r.status_code, 401)

    # --- Ejercicios (banco de preguntas) ---
    def test_ejercicios_ocultos_para_estudiante(self):
        c = APIClient()
        c.force_authenticate(self.owner)  # incluso siendo dueño del curso
        r = c.get("/api/v1/schools/exercices/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(_as_list(r)), 0)

    def test_ejercicios_visibles_para_admin(self):
        c = APIClient()
        c.force_authenticate(self.admin)
        r = c.get("/api/v1/schools/exercices/")
        self.assertGreaterEqual(len(_as_list(r)), 1)
