"""Vencimiento con el reloj avanzando (no editando fechas en la BD).

El acceso se evalúa al leer contra `timezone.now()`; acá se parchea ese reloj
y solo se mueve el tiempo: ninguna fila se toca a mano entre asignar y
verificar. Así se comprueba que la fecha guardada al asignar (cupo, llave) es
la que realmente corta el acceso, y los bordes exactos (`>=` / `<=`).

Correr:  manage.py test sales.tests_vencimiento
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.tests import make_user
from schools.models import Escuela
from sales.models import EstudianteCurso
from sales.services import suscripcion_vigente, tiene_seat
from sales.tests_e2e import (
    TransbankFake, crear_curso, crear_escuela, pagar, producto_suscripcion, tiene_acceso,
)

T0 = timezone.now().replace(microsecond=0)
UN_SEG = timedelta(seconds=1)


class RelojMixin:
    """Parchea `timezone.now` con un reloj controlable (`self.ahora`)."""

    def setUp(self):
        super().setUp()
        self.ahora = T0
        p = patch("django.utils.timezone.now", side_effect=lambda: self.ahora)
        p.start()
        self.addCleanup(p.stop)

    def avanzar_a(self, momento):
        self.ahora = momento

    def cliente(self, user):
        # force_authenticate: el JWT (TTL 15 min) no debe interferir con el reloj.
        c = APIClient()
        c.force_authenticate(user)
        return c

    def access_key(self, alumno):
        return EstudianteCurso.objects.get(estudiante_id=alumno, curso_id=self.curso).access_key_id


class CupoVencePorRelojTests(RelojMixin, TestCase):
    """Un cupo asignado con la suscripción vigente vence solo por el paso del
    tiempo, en el instante exacto de `basic_access_until`."""

    def setUp(self):
        super().setUp()
        self.hasta = T0 + timedelta(days=30)
        self.escuela = crear_escuela(basic_access=True, basic_seats_max=2, basic_access_until=self.hasta)
        self.dora = make_user("dora@a.com", is_director=True, escuela=self.escuela)
        self.al = make_user("al@a.com", is_estudiante=True, escuela=self.escuela)
        self.al2 = make_user("al2@a.com", is_estudiante=True, escuela=self.escuela)
        self.curso = crear_curso()
        r = self.cliente(self.dora).post("/api/v1/sales/activar_curso/", {
            "user_id": self.al.id, "curso_id": self.curso.id, "source": "seat",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)

    def test_cupo_guarda_el_vencimiento_de_la_suscripcion(self):
        ak = self.access_key(self.al)
        self.assertEqual(ak.origen, "seat")
        self.assertEqual(ak.valid_until, self.hasta)

    def test_acceso_hasta_el_instante_de_vencimiento_inclusive(self):
        c = self.cliente(self.al)
        self.avanzar_a(self.hasta - UN_SEG)
        self.assertTrue(tiene_acceso(c, self.curso))
        self.avanzar_a(self.hasta)
        self.assertTrue(tiene_acceso(c, self.curso))
        self.assertTrue(self.access_key(self.al).is_valid())
        self.avanzar_a(self.hasta + UN_SEG)
        self.assertFalse(tiene_acceso(c, self.curso))
        self.assertFalse(self.access_key(self.al).is_valid())

    def test_suscripcion_vencida_por_reloj_bloquea_nuevos_cupos(self):
        self.escuela.refresh_from_db()
        self.assertTrue(tiene_seat(self.escuela))
        self.avanzar_a(self.hasta + UN_SEG)
        self.escuela.refresh_from_db()
        self.assertFalse(suscripcion_vigente(self.escuela))
        self.assertFalse(tiene_seat(self.escuela))
        c = self.cliente(self.dora)
        r = c.post("/api/v1/sales/activar_curso/", {
            "user_id": self.al2.id, "curso_id": self.curso.id, "source": "seat",
        }, format="json")
        self.assertEqual(r.status_code, 400)
        st = c.get(f"/api/v1/schools/{self.escuela.id}/subscription-status/").json()["basic"]
        self.assertFalse(st["access"])


class LlaveVencePorRelojTests(RelojMixin, TestCase):
    """Una llave de 7 días vence 7 días después de asignarla, por reloj."""

    def setUp(self):
        super().setUp()
        self.escuela = crear_escuela(basic_key=1)
        self.dora = make_user("dora@a.com", is_director=True, escuela=self.escuela)
        self.al = make_user("al@a.com", is_estudiante=True, escuela=self.escuela)
        self.curso = crear_curso()
        r = self.cliente(self.dora).post("/api/v1/sales/activar_curso/", {
            "user_id": self.al.id, "curso_id": self.curso.id, "source": "key", "days": 7,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)

    def test_vence_a_los_7_dias_exactos(self):
        ak = self.access_key(self.al)
        self.assertEqual(ak.valid_until, T0 + timedelta(days=7))
        c = self.cliente(self.al)
        self.avanzar_a(T0 + timedelta(days=6))
        self.assertTrue(tiene_acceso(c, self.curso))
        self.avanzar_a(ak.valid_until)
        self.assertTrue(tiene_acceso(c, self.curso))
        self.avanzar_a(ak.valid_until + UN_SEG)
        self.assertFalse(tiene_acceso(c, self.curso))


@patch("sales.views.Transaction")
class CupoDeSolicitudVenceYRenuevaTests(RelojMixin, TestCase):
    """Cupo aprobado por solicitud (estudiante sin escuela) → vence por reloj →
    el director renueva la suscripción → el cupo recupera acceso."""

    def setUp(self):
        super().setUp()
        self.hasta = T0 + timedelta(days=30)
        self.escuela = crear_escuela(basic_access=True, basic_seats_max=3, basic_access_until=self.hasta)
        self.dora = make_user("dora@a.com", is_director=True, escuela=self.escuela)
        self.est = make_user("est@x.com", is_estudiante=True)  # sin escuela
        self.curso = crear_curso()
        self.sub = producto_suscripcion(cupos=3, dias=30, precio=90000)

    def test_journey(self, MockTx):
        TransbankFake(MockTx)
        c_est = self.cliente(self.est)
        r = c_est.post("/api/v1/sales/solicitudes/", {
            "codigo_escuela": self.escuela.codigo, "curso_id": self.curso.id, "mensaje": "hola",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        c_dir = self.cliente(self.dora)
        r = c_dir.post(f"/api/v1/sales/solicitudes/{r.data['id']}/aprobar/", {"source": "seat"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.est.refresh_from_db()
        self.assertEqual(self.est.escuela_id, self.escuela.id)
        ak = self.access_key(self.est)
        self.assertEqual((ak.origen, ak.valid_until), ("seat", self.hasta))
        self.assertTrue(tiene_acceso(c_est, self.curso))

        # Solo pasa el tiempo: el cupo vence con la suscripción.
        self.avanzar_a(self.hasta + timedelta(days=1))
        self.assertFalse(tiene_acceso(c_est, self.curso))

        # Renovación (vencida → período nuevo desde hoy): el cupo se extiende.
        _, r = pagar(c_dir, self.dora, buy_order=f"order_{self.sub.id}_{self.dora.id}", amount=90000,
                     item_type="producto")
        self.assertEqual(r.status_code, 201, r.data)
        self.escuela.refresh_from_db()
        nuevo_fin = self.ahora + timedelta(days=30)
        self.assertEqual(self.escuela.basic_access_until, nuevo_fin)
        self.assertEqual(self.access_key(self.est).valid_until, nuevo_fin)
        self.assertTrue(tiene_acceso(c_est, self.curso))

        # Y vuelve a vencer con el nuevo fin.
        self.avanzar_a(nuevo_fin + UN_SEG)
        self.assertFalse(tiene_acceso(c_est, self.curso))
        self.assertFalse(suscripcion_vigente(Escuela.objects.get(pk=self.escuela.pk)))
