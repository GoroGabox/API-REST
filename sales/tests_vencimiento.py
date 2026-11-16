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
from sales.models import CupoSuscripcion, GRACIA_DIAS, RETENCION_DIAS
from sales.services import suscripcion_vigente, tiene_seat
from sales.suscripciones import liberar_cupos_vencidos
from sales.testing import dar_suscripcion
from sales.tests_e2e import TransbankFake, crear_curso, crear_escuela, pagar, plan_suscripcion, tiene_acceso

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
    """Un cupo da acceso hasta el fin de la suscripción + 7 días de gracia; se
    corta solo por el paso del tiempo (bordes inclusivos)."""

    def setUp(self):
        super().setUp()
        self.hasta = T0 + timedelta(days=30)
        self.fin_gracia = self.hasta + timedelta(days=GRACIA_DIAS)
        self.escuela = crear_escuela()
        self.sus = dar_suscripcion(self.escuela, cupos=2, fin=self.hasta)
        self.dora = make_user("dora@a.com", is_director=True, escuela=self.escuela)
        self.al = make_user("al@a.com", is_estudiante=True, escuela=self.escuela)
        self.al2 = make_user("al2@a.com", is_estudiante=True, escuela=self.escuela)
        self.curso = crear_curso()
        r = self.cliente(self.dora).post("/api/v1/sales/activar_curso/", {
            "user_id": self.al.id, "curso_id": self.curso.id, "source": "seat",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)

    def test_la_llave_del_cupo_no_vence_la_gobierna_el_cupo(self):
        ak = self.access_key(self.al)
        self.assertEqual(ak.origen, "seat")
        self.assertIsNone(ak.valid_until)

    def test_acceso_hasta_el_fin_de_la_gracia_inclusive(self):
        c = self.cliente(self.al)
        self.avanzar_a(self.hasta + UN_SEG)  # en gracia
        self.assertTrue(tiene_acceso(c, self.curso))
        self.avanzar_a(self.fin_gracia)
        self.assertTrue(tiene_acceso(c, self.curso))
        self.avanzar_a(self.fin_gracia + UN_SEG)  # suspendida
        self.assertFalse(tiene_acceso(c, self.curso))

    def test_suspendida_bloquea_nuevos_cupos(self):
        self.avanzar_a(self.hasta + UN_SEG)
        self.escuela.refresh_from_db()
        self.assertTrue(suscripcion_vigente(self.escuela))  # gracia: sigue operando
        self.assertTrue(tiene_seat(self.escuela))
        self.avanzar_a(self.fin_gracia + UN_SEG)
        self.assertFalse(suscripcion_vigente(self.escuela))
        self.assertFalse(tiene_seat(self.escuela))
        c = self.cliente(self.dora)
        r = c.post("/api/v1/sales/activar_curso/", {
            "user_id": self.al2.id, "curso_id": self.curso.id, "source": "seat",
        }, format="json")
        self.assertEqual(r.status_code, 400)
        st = c.get(f"/api/v1/schools/{self.escuela.id}/subscription-status/").json()
        self.assertFalse(st["basic"]["access"])
        self.assertEqual(st["suscripcion"]["estado"], "suspendida")

    def test_vencida_libera_los_cupos_guardados(self):
        self.avanzar_a(self.fin_gracia + timedelta(days=RETENCION_DIAS))
        self.assertEqual(liberar_cupos_vencidos(), [])  # aún suspendida
        self.avanzar_a(self.fin_gracia + timedelta(days=RETENCION_DIAS) + UN_SEG)
        self.assertEqual(liberar_cupos_vencidos(), [self.escuela.id])
        self.assertFalse(CupoSuscripcion.objects.filter(estudiante=self.al, liberado_en__isnull=True).exists())


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
    """Cupo aprobado por solicitud (estudiante sin escuela) → se suspende por
    reloj → el director renueva por Webpay → el cupo guardado recupera acceso."""

    def setUp(self):
        super().setUp()
        self.hasta = T0 + timedelta(days=30)
        self.escuela = crear_escuela()
        self.plan = plan_suscripcion(cupos=3, precio=90000)
        dar_suscripcion(self.escuela, cupos=3, fin=self.hasta)
        self.dora = make_user("dora@a.com", is_director=True, escuela=self.escuela)
        self.est = make_user("est@x.com", is_estudiante=True)  # sin escuela
        self.curso = crear_curso()

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
        self.assertTrue(tiene_acceso(c_est, self.curso))

        # Solo pasa el tiempo: termina la gracia y se suspende.
        self.avanzar_a(self.hasta + timedelta(days=GRACIA_DIAS + 1))
        self.assertFalse(tiene_acceso(c_est, self.curso))

        # Renovación (suspendida → período nuevo desde hoy): el cupo guardado vuelve.
        _, r = pagar(c_dir, self.dora, buy_order=f"sus_{self.plan.id}_1_{self.dora.id}", amount=90000,
                     item_type="suscripcion")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(tiene_acceso(c_est, self.curso))
        self.escuela.refresh_from_db()
        nuevo_fin = self.escuela.basic_access_until
        self.assertGreater(nuevo_fin, self.ahora + timedelta(days=27))

        # Y vuelve a cortarse al terminar la nueva gracia.
        self.avanzar_a(nuevo_fin + timedelta(days=GRACIA_DIAS) + UN_SEG)
        self.assertFalse(tiene_acceso(c_est, self.curso))
        self.assertFalse(suscripcion_vigente(Escuela.objects.get(pk=self.escuela.pk)))
