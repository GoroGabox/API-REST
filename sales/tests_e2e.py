"""Tests e2e (API) de alta de usuarios, compras y llaves.

Cada clase recorre un journey completo por HTTP, como lo haría un cliente:
login real (JWT vía `accounts/login/`), pago Transbank mockeado en el único
seam disponible (`sales.views.Transaction`) y verificación del acceso REAL al
contenido gateado (`schools/courses/<id>/units/` + `schools/lessons/`), no solo
de las filas creadas.

Journeys:
  J1  Estudiante sin escuela: registro → compra B2C de curso → renovación.
  J1b Social login: estudiante sin escuela, sin acceso.
  J2  Admin crea escuela+director → director compra llaves.
  J3  Director compra suscripción → cupos → vencimiento → renovación.
  J4  Director vincula estudiante → entrega/extiende/revoca llaves.
  J5  Alta masiva con activación + invitación → define contraseña → acceso.
  J6  Estudiante pide acceso con código de escuela → director aprueba.
  J7  Canje de llave suelta (admin) por un estudiante.
  J8  Admin crea usuarios por API.

Correr:  manage.py test sales.tests_e2e
"""
import re
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Usuario
from accounts.tests import make_user
from schools.models import Curso, Escuela, Leccion, PlanCurso
from sales.models import (
    AccessKey, EstudianteCurso, Producto, SolicitudAcceso, TransbankTransaction, Venta,
)
from sales.services import tiene_seat

PASSWORD = "Abcdef12!@#"  # la de accounts.tests.make_user
PRECIOS = {7: 10000, 14: 18000, 35: 40000}


# ---------------------------------------------------------------- helpers

def api_login(email, password=PASSWORD):
    """Cliente autenticado con el JWT que devuelve el login real."""
    c = APIClient()
    r = c.post("/api/v1/accounts/login/", {"email": email, "password": password}, format="json")
    assert r.status_code == 200, (r.status_code, getattr(r, "data", r.content))
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    return c


def crear_curso(nombre="Curso B", codigo="B"):
    curso = Curso.objects.create(nombre=nombre, descripcion="d", codigo=codigo)
    for dias, precio in PRECIOS.items():
        PlanCurso.objects.create(curso=curso, dias=dias, precio=precio, activo=True, orden=dias)
    Leccion.objects.create(curso=curso, nombre="L1", posicion=1, tipo="texto", contenido="PREMIUM")
    return curso


def crear_escuela(nombre="Escuela A", **kw):
    return Escuela.objects.create(nombre=nombre, direccion="x", email=f"{nombre[-1].lower()}@e.com",
                                  telefono="1", **kw)


def producto_llaves(cant=5, precio=50000):
    return Producto.objects.create(nombre=f"{cant} llaves", tipo="llave", valor_neto=precio,
                                   descripcion="d", cant_basic_key=cant)


def producto_suscripcion(cupos=3, dias=30, precio=90000):
    return Producto.objects.create(nombre="Suscripción", tipo="suscripcion", valor_neto=precio,
                                   descripcion="d", basic_access=True, cant_seats=cupos, duracion_dias=dias)


def tiene_acceso(client, curso):
    """Acceso real: units no da 403 y la lección premium es visible."""
    r_units = client.get(f"/api/v1/schools/courses/{curso.id}/units/")
    r_lec = client.get(f"/api/v1/schools/lessons/?curso={curso.id}")
    body = r_lec.json()
    lecciones = body["results"] if isinstance(body, dict) and "results" in body else body
    ve_premium = any(l.get("contenido") == "PREMIUM" for l in lecciones)
    assert (r_units.status_code == 200) == ve_premium, (r_units.status_code, ve_premium)
    return ve_premium


def definir_password(correo, password):
    """Sigue el enlace de invitación del correo y define la contraseña."""
    uid, token = re.search(r"uidb64=([^&\s]+)&token=(\S+)", correo.body).groups()
    r = APIClient().post(f"/api/v1/accounts/new_password/{uid}/{token}/", {
        "uid": uid, "token": token, "new_password": password, "confirm_password": password,
    }, format="json")
    assert r.status_code == 200, r.data


def vencer_llave(ak):
    ak.valid_from = timezone.now() - timedelta(days=40)
    ak.valid_until = timezone.now() - timedelta(days=1)
    ak.save(update_fields=["valid_from", "valid_until"])


class TransbankFake:
    """Pasarela simulada: `create` da token, `commit` autoriza el buy_order/monto
    con que se inició ese token (o lo que el test fuerce)."""

    def __init__(self, mock_tx):
        self.iniciadas = {}
        self.n = 0
        inst = mock_tx.return_value
        inst.create.side_effect = self._create
        inst.commit.side_effect = self._commit

    def _create(self, buy_order, session_id, amount, return_url):
        self.n += 1
        token = f"TOK{self.n}"
        self.iniciadas[token] = (buy_order, amount)
        return {"url": "https://webpay.test/pay", "token": token}

    def _commit(self, token):
        buy_order, amount = self.iniciadas[token]
        return {
            "status": "AUTHORIZED", "amount": amount, "buy_order": buy_order,
            "transaction_date": "2026-10-05T12:00:00.000Z", "payment_type_code": "VN",
            "accounting_date": "1005",
        }


def pagar(client, user, *, buy_order, amount, item_type, product_id=None):
    """pay_init + pay_confirm. Devuelve (resp_init, resp_confirm | None)."""
    r1 = client.post("/api/v1/sales/pay_init/", {
        "amount": amount, "session_id": f"s-{user.id}", "buy_order": buy_order,
        "payment_method": "transbank", "item_type": item_type,
    }, format="json")
    if r1.status_code != 200:
        return r1, None
    r2 = client.post("/api/v1/sales/pay_confirm/", {
        "token_ws": r1.data["token"], "product_id": product_id or int(buy_order.split("_")[1]),
        "user_id": user.id, "payment_method": "transbank", "item_type": item_type,
    }, format="json")
    return r1, r2


# ---------------------------------------------------------------- journeys

@patch("sales.views.Transaction")
class J1EstudianteSinEscuelaCompraCursoTests(TestCase):
    def setUp(self):
        self.curso = crear_curso()
        self.otra_escuela = crear_escuela("Escuela Z")

    def test_journey(self, MockTx):
        tbk = TransbankFake(MockTx)

        # Registro público: estudiante activo, sin escuela aunque la mande.
        r = APIClient().post("/api/v1/accounts/register/", {
            "nombre": "Ana", "apellido": "B", "email": "ana@x.com",
            "password": PASSWORD, "password2": PASSWORD, "escuela": self.otra_escuela.id,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertIn("access", r.data)
        ana = Usuario.objects.get(email="ana@x.com")
        self.assertTrue(ana.is_estudiante and ana.is_active and not ana.is_director)
        self.assertIsNone(ana.escuela_id)
        self.assertTrue(ana.groups.filter(name="Estudiantes").exists())

        c = api_login("ana@x.com")
        self.assertFalse(tiene_acceso(c, self.curso))

        # Monto manipulado → 400 sin tocar la pasarela.
        bo = f"order_{self.curso.id}_{ana.id}_14"
        r1, _ = pagar(c, ana, buy_order=bo, amount=1, item_type="curso")
        self.assertEqual(r1.status_code, 400)
        MockTx.return_value.create.assert_not_called()
        # Plan inexistente → 400.
        r1, _ = pagar(c, ana, buy_order=f"order_{self.curso.id}_{ana.id}_20", amount=1, item_type="curso")
        self.assertEqual(r1.status_code, 400)

        # Compra de 14 días.
        r1, r2 = pagar(c, ana, buy_order=bo, amount=PRECIOS[14], item_type="curso")
        self.assertEqual(r2.status_code, 201, r2.data)
        self.assertTrue(tiene_acceso(c, self.curso))
        ec = EstudianteCurso.objects.get(estudiante_id=ana, curso_id=self.curso)
        ak = ec.access_key_id
        self.assertEqual(ak.origen, "purchase")
        self.assertAlmostEqual((ak.valid_until - timezone.now()).days, 13, delta=1)
        venta = Venta.objects.get(usuario=ana)
        self.assertEqual((venta.curso_id, venta.producto_id, int(venta.monto_pagado)),
                         (self.curso.id, None, PRECIOS[14]))

        # Reconfirmar el mismo token: idempotente, sin segunda venta.
        r = c.post("/api/v1/sales/pay_confirm/", {
            "token_ws": r1.data["token"], "product_id": self.curso.id, "user_id": ana.id,
            "payment_method": "transbank", "item_type": "curso",
        }, format="json")
        self.assertFalse(r.data["success"])
        self.assertEqual(Venta.objects.filter(usuario=ana).count(), 1)

        # Recomprar con acceso vigente → 409.
        _, r2 = pagar(c, ana, buy_order=f"order_{self.curso.id}_{ana.id}_7", amount=PRECIOS[7], item_type="curso")
        self.assertEqual(r2.status_code, 409)
        self.assertEqual(r2.data["code"], "already_enrolled")

        # Vence → sin acceso → renueva 7 días → acceso de nuevo.
        vencer_llave(ak)
        self.assertFalse(tiene_acceso(c, self.curso))
        _, r2 = pagar(c, ana, buy_order=f"order_{self.curso.id}_{ana.id}_7", amount=PRECIOS[7], item_type="curso")
        self.assertEqual(r2.status_code, 201, r2.data)
        self.assertTrue(tiene_acceso(c, self.curso))
        self.assertEqual(EstudianteCurso.objects.filter(estudiante_id=ana).count(), 1)
        self.assertEqual(Venta.objects.filter(usuario=ana).count(), 2)

        # Un estudiante no puede comprar productos de escuela (no recibiría nada).
        llaves = producto_llaves()
        r1, _ = pagar(c, ana, buy_order=f"order_{llaves.id}_{ana.id}", amount=50000, item_type="producto")
        self.assertEqual(r1.status_code, 403)

        # Ni pagar a nombre de otro.
        otro = make_user("otro@x.com", is_estudiante=True)
        r1, _ = pagar(c, ana, buy_order=f"order_{self.curso.id}_{otro.id}_7", amount=PRECIOS[7], item_type="curso")
        self.assertEqual(r1.status_code, 403)


class J1bSocialLoginTests(TestCase):
    def setUp(self):
        self._orig = getattr(settings, "SOCIAL_AUTH_VERIFIERS", None)
        settings.SOCIAL_AUTH_VERIFIERS = {
            **(self._orig or {}),
            "google": lambda tok: {"email": "g@x.com", "nombre": "G", "apellido": "X", "sub": "s1"},
        }

    def tearDown(self):
        if self._orig is None:
            del settings.SOCIAL_AUTH_VERIFIERS
        else:
            settings.SOCIAL_AUTH_VERIFIERS = self._orig

    def test_journey(self):
        curso = crear_curso()
        r = APIClient().post("/api/v1/accounts/social/google/", {"id_token": "FAKE"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        u = Usuario.objects.get(email="g@x.com")
        self.assertTrue(u.is_estudiante and u.is_active)
        self.assertIsNone(u.escuela_id)
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
        self.assertFalse(tiene_acceso(c, curso))


@patch("sales.views.Transaction")
class J2DirectorCompraLlavesTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin@x.com", is_admin=True)
        self.llaves = producto_llaves(cant=5, precio=50000)
        self.caro = producto_llaves(cant=100, precio=900000)

    def test_journey(self, MockTx):
        tbk = TransbankFake(MockTx)

        # Admin crea escuela + director.
        r = api_login("admin@x.com").post("/api/v1/accounts/registrar-escuela-director/", {
            "escuela_nombre": "Escuela A", "escuela_direccion": "Calle 1",
            "escuela_email": "esc@a.com", "escuela_telefono": "123",
            "director_nombre": "Dora", "director_apellido": "D",
            "director_email": "dora@a.com", "director_password": PASSWORD,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        dora = Usuario.objects.get(email="dora@a.com")
        escuela = dora.escuela
        self.assertTrue(dora.is_director and dora.is_active and not dora.is_estudiante)
        self.assertTrue(dora.groups.filter(name="Directores").exists())
        self.assertTrue(escuela.codigo)
        self.assertEqual(escuela.basic_key, 0)

        # Un director no puede crear escuelas.
        c = api_login("dora@a.com")
        r = c.post("/api/v1/accounts/registrar-escuela-director/", {}, format="json")
        self.assertEqual(r.status_code, 403)

        # Compra de 5 llaves; confirma mandando el id de un producto más caro:
        # se ignora, manda el buy_order autoritativo de Transbank.
        r1, r2 = pagar(c, dora, buy_order=f"order_{self.llaves.id}_{dora.id}", amount=50000,
                       item_type="producto", product_id=self.caro.id)
        self.assertEqual(r2.status_code, 201, r2.data)
        escuela.refresh_from_db()
        self.assertEqual(escuela.basic_key, 5)
        venta = Venta.objects.get(usuario=dora)
        self.assertEqual((venta.producto_id, venta.escuela_id), (self.llaves.id, escuela.id))
        self.assertTrue(TransbankTransaction.objects.filter(sale=venta, token=r1.data["token"]).exists())

        # Segunda compra acumula.
        pagar(c, dora, buy_order=f"order_{self.llaves.id}_{dora.id}", amount=50000, item_type="producto")
        escuela.refresh_from_db()
        self.assertEqual(escuela.basic_key, 10)

        # Monto manipulado → 400, saldo intacto.
        r1, _ = pagar(c, dora, buy_order=f"order_{self.caro.id}_{dora.id}", amount=50000, item_type="producto")
        self.assertEqual(r1.status_code, 400)
        escuela.refresh_from_db()
        self.assertEqual(escuela.basic_key, 10)


@patch("sales.views.Transaction")
class J3DirectorCompraSuscripcionTests(TestCase):
    def setUp(self):
        self.escuela = crear_escuela()
        self.dora = make_user("dora@a.com", is_director=True, escuela=self.escuela)
        self.alumnos = [make_user(f"al{i}@a.com", is_estudiante=True, escuela=self.escuela) for i in range(4)]
        self.curso = crear_curso()
        self.sub = producto_suscripcion(cupos=3, dias=30, precio=90000)

    def _activar(self, c, alumno, source="seat", days=None):
        body = {"user_id": alumno.id, "curso_id": self.curso.id, "source": source}
        if days:
            body["days"] = days
        return c.post("/api/v1/sales/activar_curso/", body, format="json")

    def test_journey(self, MockTx):
        TransbankFake(MockTx)
        c = api_login("dora@a.com")

        _, r2 = pagar(c, self.dora, buy_order=f"order_{self.sub.id}_{self.dora.id}", amount=90000,
                      item_type="producto")
        self.assertEqual(r2.status_code, 201, r2.data)
        self.escuela.refresh_from_db()
        self.assertTrue(self.escuela.basic_access)
        self.assertEqual(self.escuela.basic_seats_max, 3)
        self.assertAlmostEqual((self.escuela.basic_access_until - timezone.now()).days, 29, delta=1)

        st = c.get(f"/api/v1/schools/{self.escuela.id}/subscription-status/").json()["basic"]
        self.assertEqual((st["access"], st["seats_max"], st["seats_available"]), (True, 3, 3))
        self.assertIsNotNone(st["access_until"])

        # 3 cupos → 3 alumnos; el 4º no tiene cupo; con auto y sin llaves tampoco.
        for al in self.alumnos[:3]:
            r = self._activar(c, al)
            self.assertEqual(r.status_code, 201, r.data)
            self.assertEqual(r.data["origen"], "seat")
        self.assertEqual(self._activar(c, self.alumnos[3]).status_code, 400)
        self.assertEqual(self._activar(c, self.alumnos[3], source="auto").status_code, 400)
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_seats_used, 3)
        c_al0 = api_login("al0@a.com")
        self.assertTrue(tiene_acceso(c_al0, self.curso))

        # Con llaves en saldo, auto cae a llave cuando no hay cupo.
        Escuela.objects.filter(pk=self.escuela.pk).update(basic_key=1)
        r = self._activar(c, self.alumnos[3], source="auto")
        self.assertEqual((r.status_code, r.data["origen"]), (201, "key"))

        # Liberar un cupo lo devuelve al pool.
        ec = EstudianteCurso.objects.get(estudiante_id=self.alumnos[2], curso_id=self.curso)
        r = c.delete(f"/api/v1/schools/{self.escuela.id}/subscription-seats/{ec.id}/")
        self.assertIn(r.status_code, (200, 204), getattr(r, "data", None))
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_seats_used, 2)
        self.assertFalse(tiene_acceso(api_login("al2@a.com"), self.curso))

        # Vence la suscripción → cupos sin acceso y no se pueden asignar.
        vencida = timezone.now() - timedelta(days=1)
        Escuela.objects.filter(pk=self.escuela.pk).update(basic_access_until=vencida)
        AccessKey.objects.filter(origen="seat").update(valid_until=vencida)
        self.escuela.refresh_from_db()
        self.assertFalse(tiene_seat(self.escuela))
        self.assertFalse(tiene_acceso(c_al0, self.curso))
        self.assertEqual(self._activar(c, self.alumnos[2]).status_code, 400)
        self.assertFalse(c.get(f"/api/v1/schools/{self.escuela.id}/subscription-status/").json()["basic"]["access"])

        # Renovar: nuevo período de 30 días; los cupos asignados recuperan acceso.
        pagar(c, self.dora, buy_order=f"order_{self.sub.id}_{self.dora.id}", amount=90000, item_type="producto")
        self.escuela.refresh_from_db()
        self.assertGreater(self.escuela.basic_access_until, timezone.now() + timedelta(days=28))
        self.assertEqual(self.escuela.basic_seats_max, 3)
        self.assertTrue(tiene_acceso(c_al0, self.curso))
        # Cupo libre (2/3 usados) → al2 se reactiva.
        self.assertEqual(self._activar(c, self.alumnos[2]).status_code, 201)

        # Comprar con la suscripción vigente suma cupos y extiende desde el fin.
        fin = self.escuela.basic_access_until
        pagar(c, self.dora, buy_order=f"order_{self.sub.id}_{self.dora.id}", amount=90000, item_type="producto")
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_seats_max, 6)
        self.assertAlmostEqual((self.escuela.basic_access_until - fin).days, 30, delta=1)
        seat_ak = EstudianteCurso.objects.get(estudiante_id=self.alumnos[0], curso_id=self.curso).access_key_id
        self.assertEqual(seat_ak.valid_until, self.escuela.basic_access_until)


class J4DirectorEntregaLlavesTests(TestCase):
    def setUp(self):
        self.escuela = crear_escuela(basic_key=4)
        self.otra = crear_escuela("Escuela B", basic_key=10)
        self.dora = make_user("dora@a.com", is_director=True, escuela=self.escuela)
        self.dir_b = make_user("dirb@b.com", is_director=True, escuela=self.otra)
        self.curso = crear_curso()

    def test_journey(self):
        c = api_login("dora@a.com")

        # Vincula (crea) estudiante → le llega el enlace para definir su
        # contraseña (nunca una contraseña en texto plano).
        r = c.post("/api/v1/schools/vincular-estudiante/",
                   {"email": "eli@a.com", "nombre": "Eli", "apellido": "E"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        eli = Usuario.objects.get(email="eli@a.com")
        self.assertEqual(eli.escuela_id, self.escuela.id)
        self.assertTrue(eli.is_estudiante and eli.is_active)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("Contraseña temporal", mail.outbox[0].body)
        definir_password(mail.outbox[0], "ClaveEli12!")
        c_eli = api_login("eli@a.com", "ClaveEli12!")
        self.assertFalse(tiene_acceso(c_eli, self.curso))

        activar = lambda cli, days, source="key": cli.post("/api/v1/sales/activar_curso/", {
            "user_id": eli.id, "curso_id": self.curso.id, "days": days, "source": source}, format="json")

        # Validaciones de entrada.
        self.assertEqual(activar(c, "abc").status_code, 400)
        self.assertEqual(activar(c, -7).status_code, 400)
        r = c.post("/api/v1/sales/activar_curso/", {"user_id": self.dir_b.id, "curso_id": self.curso.id,
                                                    "days": 7}, format="json")
        self.assertEqual(r.status_code, 400)  # destino no es estudiante
        ajeno = make_user("ajeno@b.com", is_estudiante=True, escuela=self.otra)
        r = c.post("/api/v1/sales/activar_curso/", {"user_id": ajeno.id, "curso_id": self.curso.id,
                                                    "days": 7}, format="json")
        self.assertEqual(r.status_code, 403)  # estudiante de otra escuela
        # Estudiante no puede activar; director de otra escuela tampoco.
        self.assertEqual(activar(c_eli, 7).status_code, 403)
        self.assertEqual(activar(api_login("dirb@b.com"), 7).status_code, 403)

        # 14 días = 2 llaves.
        r = activar(c, 14)
        self.assertEqual(r.status_code, 201, r.data)
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_key, 2)
        self.assertTrue(tiene_acceso(c_eli, self.curso))
        ak = EstudianteCurso.objects.get(estudiante_id=eli, curso_id=self.curso).access_key_id
        self.assertEqual(ak.origen, "key")
        self.assertAlmostEqual((ak.valid_until - timezone.now()).days, 13, delta=1)

        # Re-activar con acceso vigente → 409 sin cobrar.
        self.assertEqual(activar(c, 7).status_code, 409)
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_key, 2)

        # Extender 21 días necesita 3 llaves: sin saldo → 400 sin descontar.
        ext = lambda days: c.post("/api/v1/sales/extender_llave/",
                                  {"access_key_id": str(ak.id), "days": days}, format="json")
        self.assertEqual(ext(21).status_code, 400)
        # Extender 7 días = 1 llave.
        hasta = ak.valid_until
        r = ext(7)
        self.assertEqual(r.status_code, 200, r.data)
        ak.refresh_from_db()
        self.assertAlmostEqual((ak.valid_until - hasta).days, 7, delta=0)
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_key, 1)
        # Director de otra escuela no puede extender ni revocar.
        c_b = api_login("dirb@b.com")
        self.assertEqual(c_b.post("/api/v1/sales/extender_llave/",
                                  {"access_key_id": str(ak.id), "days": 7}, format="json").status_code, 403)
        self.assertEqual(c_b.post("/api/v1/sales/revocar_llave/",
                                  {"access_key_id": str(ak.id)}, format="json").status_code, 403)

        # Revocar → sin acceso (sin reembolso).
        r = c.post("/api/v1/sales/revocar_llave/", {"access_key_id": str(ak.id)}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(tiene_acceso(c_eli, self.curso))
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_key, 1)

        # Re-activar tras revocar: reutiliza la inscripción con llave nueva.
        r = activar(c, 7)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(tiene_acceso(c_eli, self.curso))
        self.assertEqual(EstudianteCurso.objects.filter(estudiante_id=eli).count(), 1)
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_key, 0)

        # Sin saldo → 400.
        otro = make_user("otro@a.com", is_estudiante=True, escuela=self.escuela)
        r = c.post("/api/v1/sales/activar_curso/", {"user_id": otro.id, "curso_id": self.curso.id,
                                                    "days": 7}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(EstudianteCurso.objects.filter(estudiante_id=otro).count(), 0)

        # Llave vencida → sin acceso.
        vencer_llave(EstudianteCurso.objects.get(estudiante_id=eli, curso_id=self.curso).access_key_id)
        self.assertFalse(tiene_acceso(c_eli, self.curso))


class J5AltaMasivaTests(TestCase):
    def setUp(self):
        self.escuela = crear_escuela(basic_key=3)
        self.dora = make_user("dora@a.com", is_director=True, escuela=self.escuela)
        make_user("ya@a.com", is_estudiante=True, escuela=self.escuela)
        self.curso = crear_curso()

    def test_journey(self):
        c = api_login("dora@a.com")
        r = c.post("/api/v1/accounts/bulk-students/", {
            "estudiantes": [
                {"nombre": "M1", "apellido": "A", "email": "m1@a.com"},
                {"nombre": "M2", "apellido": "A", "email": "m2@a.com"},
                {"nombre": "Ya", "apellido": "A", "email": "ya@a.com"},
            ],
            "enviar_invitacion": True, "activar_curso": True, "curso_id": self.curso.id,
            "source": "key", "days": 7,
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        m1 = Usuario.objects.get(email="m1@a.com")
        self.assertEqual(m1.escuela_id, self.escuela.id)
        self.assertEqual(Usuario.objects.filter(email="ya@a.com").count(), 1)
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_key, 1)  # 2 activaciones × 1 llave

        # Invitación → define contraseña → login → acceso.
        definir_password(next(m for m in mail.outbox if "m1@a.com" in m.to), "NuevaClave12!")
        self.assertTrue(tiene_acceso(api_login("m1@a.com", "NuevaClave12!"), self.curso))


class J6SolicitudAccesoTests(TestCase):
    def setUp(self):
        self.escuela = crear_escuela(basic_key=2)
        self.otra = crear_escuela("Escuela B")
        self.dora = make_user("dora@a.com", is_director=True, escuela=self.escuela)
        self.curso = crear_curso()

    def test_journey(self):
        APIClient().post("/api/v1/accounts/register/", {
            "nombre": "Sol", "apellido": "S", "email": "sol@x.com",
            "password": PASSWORD, "password2": PASSWORD}, format="json")
        sol = Usuario.objects.get(email="sol@x.com")
        c_sol = api_login("sol@x.com")

        self.assertEqual(c_sol.post("/api/v1/sales/solicitudes/", {
            "codigo_escuela": "NOEXISTE", "curso_id": self.curso.id}, format="json").status_code, 404)
        r = c_sol.post("/api/v1/sales/solicitudes/", {
            "codigo_escuela": self.escuela.codigo, "curso_id": self.curso.id}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        sol_id = r.data["id"]
        # Duplicada pendiente → 409.
        self.assertEqual(c_sol.post("/api/v1/sales/solicitudes/", {
            "codigo_escuela": self.escuela.codigo, "curso_id": self.curso.id}, format="json").status_code, 409)

        # Director de otra escuela no la ve; el propio la aprueba (14 días = 2 llaves).
        make_user("dirb@b.com", is_director=True, escuela=self.otra)
        r = api_login("dirb@b.com").post(f"/api/v1/sales/solicitudes/{sol_id}/aprobar/", {}, format="json")
        self.assertEqual(r.status_code, 404)
        r = api_login("dora@a.com").post(f"/api/v1/sales/solicitudes/{sol_id}/aprobar/",
                                         {"days": 14}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        sol.refresh_from_db()
        self.assertEqual(sol.escuela_id, self.escuela.id)
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_key, 0)
        self.assertEqual(SolicitudAcceso.objects.get(pk=sol_id).estado, "aprobada")
        self.assertTrue(tiene_acceso(c_sol, self.curso))

        # Ya pertenece a la escuela A: pedir a la B → 409.
        self.assertEqual(c_sol.post("/api/v1/sales/solicitudes/", {
            "codigo_escuela": self.otra.codigo, "curso_id": self.curso.id}, format="json").status_code, 409)


class J7CanjeLlaveTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin@x.com", is_admin=True)
        self.escuela = crear_escuela(basic_key=1)
        self.dora = make_user("dora@a.com", is_director=True, escuela=self.escuela)
        self.ana = make_user("ana@x.com", is_estudiante=True)
        self.beto = make_user("beto@x.com", is_estudiante=True)
        self.curso = crear_curso()

    def test_journey(self):
        canjear = lambda c, key: c.post("/api/v1/sales/canjear_llave/",
                                        {"access_key": key, "curso_id": self.curso.id}, format="json")
        # Solo admin crea llaves sueltas.
        self.assertEqual(api_login("ana@x.com").post("/api/v1/sales/access_key/", {}, format="json").status_code, 403)
        r = api_login("admin@x.com").post("/api/v1/sales/access_key/", {
            "valid_until": (timezone.now() + timedelta(days=7)).isoformat(), "origen": "key"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        key = AccessKey.objects.get(pk=r.data["id"]).key

        c_ana, c_beto = api_login("ana@x.com"), api_login("beto@x.com")
        self.assertEqual(canjear(c_ana, key).status_code, 201)
        self.assertTrue(tiene_acceso(c_ana, self.curso))  # antes: quedaba 'used' y sin acceso
        # Otro estudiante no puede reusarla, y Ana no pierde el acceso.
        r = canjear(c_beto, key)
        self.assertEqual((r.status_code, r.data["code"]), (409, "key_already_bound"))
        self.assertTrue(tiene_acceso(c_ana, self.curso))

        # Una llave entregada por el director a un alumno no la puede canjear un tercero.
        eli = make_user("eli@a.com", is_estudiante=True, escuela=self.escuela)
        r = api_login("dora@a.com").post("/api/v1/sales/activar_curso/",
                                         {"user_id": eli.id, "curso_id": self.curso.id, "days": 7}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        r = canjear(c_beto, r.data["access_key"])
        self.assertEqual(r.status_code, 409)
        self.assertTrue(tiene_acceso(api_login("eli@a.com"), self.curso))

        # Llave vencida → 400.
        vieja = AccessKey.objects.create(valid_until=timezone.now() - timedelta(days=1))
        self.assertEqual(canjear(c_beto, vieja.key).status_code, 400)
        self.assertFalse(tiene_acceso(c_beto, self.curso))


class J8AdminCreaUsuariosTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin@x.com", is_admin=True)
        self.escuela = crear_escuela()

    def test_journey(self):
        c = api_login("admin@x.com")
        base = {"nombre": "N", "apellido": "A", "password": PASSWORD, "password2": PASSWORD}
        r = c.post("/api/v1/accounts/list_user/", {**base, "email": "d1@x.com", "is_director": True},
                   format="json")
        self.assertEqual(r.status_code, 400)  # director sin escuela
        r = c.post("/api/v1/accounts/list_user/", {**base, "email": "d2@x.com", "is_director": True,
                                                   "escuela": self.escuela.id, "is_active": True}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(Usuario.objects.get(email="d2@x.com").groups.filter(name="Directores").exists())
        r = c.post("/api/v1/accounts/list_user/", {**base, "email": "e1@x.com", "is_estudiante": True,
                                                   "is_active": True}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        api_login("e1@x.com")
        api_login("d2@x.com")
        # Un estudiante no crea usuarios.
        self.assertEqual(api_login("e1@x.com").post("/api/v1/accounts/list_user/",
                                                    {**base, "email": "z@x.com"}, format="json").status_code, 403)


class J9CuposAdminYCatalogoTests(TestCase):
    """Cupos asignados por admin se contabilizan; el catálogo refleja el acceso real."""

    def setUp(self):
        self.admin = make_user("admin@x.com", is_admin=True)
        self.escuela = crear_escuela(basic_access=True, basic_seats_max=2)
        self.dora = make_user("dora@a.com", is_director=True, escuela=self.escuela)
        self.al1 = make_user("al1@a.com", is_estudiante=True, escuela=self.escuela)
        self.al2 = make_user("al2@a.com", is_estudiante=True, escuela=self.escuela)
        self.curso = crear_curso()

    def _catalogo(self, c, user):
        r = c.get(f"/api/v1/sales/escuelas/{self.escuela.id}/usuario/{user.id}/cursos-disponibles/")
        self.assertEqual(r.status_code, 200, r.data)
        return next(x for x in r.json() if x["id"] == self.curso.id)

    def test_journey(self):
        c_al2 = api_login("al2@a.com")
        # Escuela con suscripción pero sin cupo asignado al alumno → no puede acceder.
        item = self._catalogo(c_al2, self.al2)
        self.assertEqual((item["user_can_access"], item["already_owned"]), (False, False))

        # Admin asigna cupo a al1 (sin exigir saldo, pero se contabiliza).
        r = api_login("admin@x.com").post("/api/v1/sales/activar_curso/", {
            "user_id": self.al1.id, "curso_id": self.curso.id, "source": "seat"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_seats_used, 1)

        # Director asigna el otro cupo a al2.
        c = api_login("dora@a.com")
        r = c.post("/api/v1/sales/activar_curso/", {
            "user_id": self.al2.id, "curso_id": self.curso.id, "source": "seat"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(self._catalogo(c_al2, self.al2)["user_can_access"])

        # Revocar el cupo del admin no libera el de al2: queda 1 usado, 1 libre.
        ak1 = EstudianteCurso.objects.get(estudiante_id=self.al1).access_key_id
        c.post("/api/v1/sales/revocar_llave/", {"access_key_id": str(ak1.id)}, format="json")
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_seats_used, 1)
        self.assertTrue(tiene_acceso(c_al2, self.curso))

        # Inscripción vencida: ya "tuyo" pero sin acceso.
        vencer_llave(EstudianteCurso.objects.get(estudiante_id=self.al2).access_key_id)
        item = self._catalogo(c_al2, self.al2)
        self.assertEqual((item["user_can_access"], item["already_owned"]), (False, True))


class LegacyWebpayOptionsTests(TestCase):
    """webpay_init/ usa la config de entorno (TBK_ENVIRONMENT), no TEST fijo."""

    @patch.dict("os.environ", {"TBK_ENVIRONMENT": "LIVE", "TBK_COMMERCE_CODE": "597000000001",
                               "TBK_API_KEY": "live-key"})
    @patch("sales.views.Transaction")
    def test_init_usa_opciones_de_entorno(self, MockTx):
        TransbankFake(MockTx)
        escuela = crear_escuela()
        dora = make_user("dora@a.com", is_director=True, escuela=escuela)
        prod = producto_llaves(cant=1, precio=1000)
        r = api_login("dora@a.com").post("/api/v1/sales/webpay_init/", {
            "amount": 1000, "session_id": "s", "buy_order": f"order_{prod.id}_{dora.id}"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        opts = MockTx.call_args.args[0]
        self.assertEqual((opts.commerce_code, opts.api_key), ("597000000001", "live-key"))
