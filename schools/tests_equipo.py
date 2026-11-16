"""Equipo del plan Pro: directores, sedes y alcance de las listas.

Correr:  manage.py test schools.tests_equipo
"""
from django.core import mail
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Usuario
from accounts.tests import make_user
from sales.models import Suscripcion
from sales.testing import dar_suscripcion, plan_de
from schools.models import Escuela, Sede


def cliente(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


class EquipoProTests(TestCase):
    def setUp(self):
        self.escuela = Escuela.objects.create(nombre="E", direccion="x", email="e@e.com", telefono="1")
        self.sus = dar_suscripcion(self.escuela, cupos=25, tier="pro")
        self.dora = make_user("dora@a.com", is_director=True, escuela=self.escuela)
        self.c = cliente(self.dora)
        self.al1 = make_user("al1@a.com", is_estudiante=True, escuela=self.escuela)
        self.al2 = make_user("al2@a.com", is_estudiante=True, escuela=self.escuela)

    def _visibles(self, user):
        r = cliente(user).get(f"/api/v1/sales/escuelas/{self.escuela.id}/estudiantes-cursos-activos/")
        return r.status_code, sorted(x["id"] for x in r.json()) if r.status_code == 200 else None

    def test_invitar_director_envia_correo(self):
        r = self.c.post("/api/v1/schools/equipo/invitar/", {"email": "beto@a.com", "nombre": "Beto"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(any("contraseña" in m.subject.lower() for m in mail.outbox))
        beto = Usuario.objects.get(email="beto@a.com")
        self.assertEqual((beto.is_director, beto.escuela_id), (True, self.escuela.id))

    def test_director_con_sede_ve_solo_su_sede(self):
        r = self.c.post("/api/v1/schools/equipo/sedes/", {"nombre": "Norte"}, format="json")
        sede = Sede.objects.get(pk=r.data["id"])
        self.c.patch(f"/api/v1/schools/equipo/miembros/{self.al1.id}/", {"sede": sede.id}, format="json")
        r = self.c.post("/api/v1/schools/equipo/invitar/",
                        {"email": "beto@a.com", "nombre": "Beto", "sede": sede.id}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        beto = Usuario.objects.get(email="beto@a.com")
        self.assertEqual(self._visibles(beto), (200, [self.al1.id]))
        self.assertEqual(self._visibles(self.dora), (200, sorted([self.al1.id, self.al2.id])))
        eq = self.c.get("/api/v1/schools/equipo/").data
        self.assertEqual(len(eq["directores"]), 2)
        self.assertEqual(eq["sedes"][0]["estudiantes"], 1)

    def test_esencial_no_puede_usar_funciones_pro(self):
        Suscripcion.objects.filter(pk=self.sus.pk).update(plan=plan_de(25, "esencial"))
        r = self.c.post("/api/v1/schools/equipo/invitar/", {"email": "x@a.com", "nombre": "X"},
                        format="json")
        self.assertEqual((r.status_code, r.data["code"]), (403, "requiere_pro"))
        self.assertEqual(self.c.post("/api/v1/schools/equipo/sedes/", {"nombre": "N"}, format="json").status_code, 403)
        self.assertEqual(self.c.get("/api/v1/schools/equipo/").data["pro"], False)

    def test_no_puede_quitarse_a_si_mismo_y_si_a_otro(self):
        otro = make_user("otro@a.com", is_director=True, escuela=self.escuela)
        self.assertEqual(self.c.delete(f"/api/v1/schools/equipo/miembros/{self.dora.id}/").status_code, 400)
        self.assertEqual(self.c.delete(f"/api/v1/schools/equipo/miembros/{otro.id}/").status_code, 200)
        otro.refresh_from_db()
        self.assertEqual((otro.escuela_id, otro.is_director, otro.is_active), (None, False, False))
