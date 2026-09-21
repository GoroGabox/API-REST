"""Tests del alta masiva de estudiantes (BulkStudentCreateView).

Cubre: creación por director/admin, omisión de existentes/duplicados,
validación de email, activación de curso opcional (consumo de llaves del
director, sin descuento para admin) y permisos.

Correr:  manage.py test accounts.tests_bulk --settings=autotestAPI.settings.test
"""
from django.core import mail
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Usuario
from accounts.tests import make_user
from schools.models import Curso, Escuela
from sales.models import EstudianteCurso

URL = "/api/v1/accounts/bulk-students/"


class BulkBaseTest(APITestCase):
    def setUp(self):
        self.escuela = Escuela.objects.create(
            nombre="Escuela A", direccion="x", email="a@a.com", telefono="1",
            basic_key=3,
        )
        self.otra = Escuela.objects.create(
            nombre="Escuela B", direccion="y", email="b@b.com", telefono="2",
        )
        self.director = make_user("dir@a.com", is_director=True, escuela=self.escuela)
        self.admin = make_user("adm@a.com", is_admin=True)
        self.estudiante = make_user("est@a.com", is_estudiante=True, escuela=self.escuela)
        self.curso = Curso.objects.create(nombre="Básico", descripcion="d", is_profesional=False)

    def filas(self, n=2):
        return [
            {"nombre": f"Alum{i}", "apellido": "Test", "email": f"alum{i}@x.com"}
            for i in range(n)
        ]


class BulkCreacionTests(BulkBaseTest):
    def test_director_crea_estudiantes_de_su_escuela(self):
        mail.outbox = []
        self.client.force_authenticate(self.director)
        r = self.client.post(URL, {"estudiantes": self.filas(2)}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(r.data["resumen"]["creados"], 2)
        nuevos = Usuario.objects.filter(email__in=["alum0@x.com", "alum1@x.com"])
        self.assertEqual(nuevos.count(), 2)
        for u in nuevos:
            self.assertTrue(u.is_estudiante)
            self.assertTrue(u.is_active)
            self.assertEqual(u.escuela_id, self.escuela.id)
        # Invitación enviada por defecto (una por alumno).
        self.assertEqual(len(mail.outbox), 2)

    def test_no_envia_invitacion_si_se_desactiva(self):
        mail.outbox = []
        self.client.force_authenticate(self.director)
        r = self.client.post(
            URL, {"estudiantes": self.filas(1), "enviar_invitacion": False}, format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(len(mail.outbox), 0)

    def test_omite_existentes_y_duplicados_internos(self):
        self.client.force_authenticate(self.director)
        filas = [
            {"nombre": "Ya", "apellido": "Existe", "email": self.estudiante.email},
            {"nombre": "Dup", "apellido": "Uno", "email": "dup@x.com"},
            {"nombre": "Dup", "apellido": "Dos", "email": "dup@x.com"},
        ]
        r = self.client.post(URL, {"estudiantes": filas}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(r.data["resumen"]["creados"], 1)   # solo dup@x.com una vez
        self.assertEqual(r.data["resumen"]["omitidos"], 2)  # existente + duplicado

    def test_email_invalido_es_error(self):
        self.client.force_authenticate(self.director)
        filas = [{"nombre": "Mal", "apellido": "Email", "email": "no-es-email"}]
        r = self.client.post(URL, {"estudiantes": filas}, format="json")
        self.assertEqual(r.data["resumen"]["errores"], 1)
        self.assertEqual(r.data["resumen"]["creados"], 0)

    def test_lista_vacia_400(self):
        self.client.force_authenticate(self.director)
        r = self.client.post(URL, {"estudiantes": []}, format="json")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)


class BulkActivacionTests(BulkBaseTest):
    def test_director_activa_curso_consume_llaves(self):
        self.client.force_authenticate(self.director)
        r = self.client.post(URL, {
            "estudiantes": self.filas(2),
            "activar_curso": True,
            "curso_id": self.curso.id,
            "source": "key",
            "days": 7,
        }, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(r.data["resumen"]["creados"], 2)
        # 2 inscripciones creadas.
        self.assertEqual(EstudianteCurso.objects.filter(curso_id=self.curso).count(), 2)
        # 2 llaves descontadas (7 días = 1 llave c/u): 3 → 1.
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_key, 1)
        for item in r.data["resultados"]:
            self.assertTrue(item.get("curso_activado"))

    def test_sin_saldo_crea_cuenta_pero_reporta_activacion_fallida(self):
        self.escuela.basic_key = 1
        self.escuela.save(update_fields=["basic_key"])
        self.client.force_authenticate(self.director)
        r = self.client.post(URL, {
            "estudiantes": self.filas(2),
            "activar_curso": True,
            "curso_id": self.curso.id,
            "source": "key",
        }, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        # Ambas cuentas se crean.
        self.assertEqual(r.data["resumen"]["creados"], 2)
        activados = [i for i in r.data["resultados"] if i.get("curso_activado")]
        fallidos = [i for i in r.data["resultados"] if i.get("curso_activado") is False]
        self.assertEqual(len(activados), 1)
        self.assertEqual(len(fallidos), 1)
        self.assertIn("activacion_error", fallidos[0])
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_key, 0)


class BulkPermisosTests(BulkBaseTest):
    def test_estudiante_forbidden(self):
        self.client.force_authenticate(self.estudiante)
        r = self.client.post(URL, {"estudiantes": self.filas(1)}, format="json")
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_requiere_escuela(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post(URL, {"estudiantes": self.filas(1)}, format="json")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_admin_crea_en_escuela_indicada_sin_descontar_saldo(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post(URL, {
            "estudiantes": self.filas(2),
            "escuela": self.escuela.id,
            "activar_curso": True,
            "curso_id": self.curso.id,
        }, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(r.data["resumen"]["creados"], 2)
        self.assertEqual(EstudianteCurso.objects.filter(curso_id=self.curso).count(), 2)
        # Admin no descuenta saldo de la escuela.
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_key, 3)

    def test_director_ignora_escuela_del_body(self):
        """El director siempre crea en SU escuela aunque mande otra."""
        self.client.force_authenticate(self.director)
        r = self.client.post(URL, {
            "estudiantes": self.filas(1),
            "escuela": self.otra.id,
        }, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        u = Usuario.objects.get(email="alum0@x.com")
        self.assertEqual(u.escuela_id, self.escuela.id)
