"""Tests del flujo de pago (Transbank) — foco en seguridad e idempotencia.

Cubre:
  - Anti price-tampering: el monto se valida contra el precio autoritativo del
    Producto server-side (no se confía en el `amount` del cliente).
  - Ownership: no se puede iniciar/confirmar un pago suplantando a otro usuario.
  - Idempotencia: un mismo token no puede confirmar dos ventas.

Transbank se mockea (no toca la pasarela real).

Correr:  manage.py test sales.tests_payment --settings=autotestAPI.settings.test
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Usuario
from schools.models import Curso, Escuela
from sales.models import (
    Producto, TransbankTransaction, AccessKey, EstudianteCurso, Venta,
)


def make_student(email="comprador@example.com", escuela=None):
    u = Usuario.objects.create_user(
        email=email, nombre="Comp", apellido="Rador", password="x", is_estudiante=True
    )
    u.is_active = True
    u.escuela = escuela
    u.save()
    return u


def make_producto(valor_neto=10000, descuento=0):
    return Producto.objects.create(
        nombre="Llave básica", tipo="llave", valor_neto=valor_neto,
        descuento=descuento, descripcion="d", cant_basic_key=1,
    )


def make_curso(nombre="Curso X", costo=15000, profesional=False):
    return Curso.objects.create(
        nombre=nombre, descripcion="d", costo=costo, codigo="CX",
        is_profesional=profesional,
    )


def enrol_purchase(student, curso, dias=30):
    """Crea una inscripción de compra individual (origen='purchase')."""
    ak = AccessKey.objects.create(
        valid_until=timezone.now() + timedelta(days=dias), origen="purchase",
    )
    return EstudianteCurso.objects.create(
        estudiante_id=student, curso_id=curso, access_key_id=ak,
    ), ak


class PayInitSecurityTest(TestCase):
    def setUp(self):
        self.user = make_student()
        self.prod = make_producto(valor_neto=10000, descuento=0)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _payload(self, amount, student_id=None):
        sid = student_id if student_id is not None else self.user.id
        return {
            "amount": amount,
            "session_id": f"sess-{self.user.id}",
            "buy_order": f"order_{self.prod.id}_{sid}",
            "payment_method": "transbank",
        }

    @patch("sales.views.Transaction")
    def test_rechaza_monto_manipulado(self, MockTx):
        # Intenta pagar 1 en vez de 10000.
        r = self.client.post("/api/v1/sales/pay_init/", self._payload(1), format="json")
        self.assertEqual(r.status_code, 400)
        # Nunca se llamó a la pasarela.
        MockTx.assert_not_called()

    @patch("sales.views.Transaction")
    def test_acepta_monto_correcto(self, MockTx):
        instance = MockTx.return_value
        instance.create.return_value = {"url": "https://webpay.test/pay", "token": "TOK123"}

        r = self.client.post("/api/v1/sales/pay_init/", self._payload(10000), format="json")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["token"], "TOK123")
        self.assertIn("url", body)
        instance.create.assert_called_once()

    @patch("sales.views.Transaction")
    def test_respeta_descuento_en_precio(self, MockTx):
        instance = MockTx.return_value
        instance.create.return_value = {"url": "u", "token": "T"}
        prod = make_producto(valor_neto=10000, descuento=20)  # precio final 8000
        payload = {
            "amount": 8000,
            "session_id": "s",
            "buy_order": f"order_{prod.id}_{self.user.id}",
            "payment_method": "transbank",
        }
        r = self.client.post("/api/v1/sales/pay_init/", payload, format="json")
        self.assertEqual(r.status_code, 200)
        # Con el precio sin descuento (10000) debe rechazar.
        payload["amount"] = 10000
        r2 = self.client.post("/api/v1/sales/pay_init/", payload, format="json")
        self.assertEqual(r2.status_code, 400)

    @patch("sales.views.Transaction")
    def test_ownership_bloquea_suplantacion(self, MockTx):
        otro = make_student("otro@example.com")
        # Paga con buy_order que dice ser de 'otro' → 403.
        r = self.client.post(
            "/api/v1/sales/pay_init/", self._payload(10000, student_id=otro.id), format="json"
        )
        self.assertEqual(r.status_code, 403)
        MockTx.assert_not_called()

    @patch("sales.views.Transaction")
    def test_producto_inexistente_rechaza_antes_de_cobrar(self, MockTx):
        payload = {
            "amount": 10000,
            "session_id": "s",
            "buy_order": f"order_999999_{self.user.id}",
            "payment_method": "transbank",
        }
        r = self.client.post("/api/v1/sales/pay_init/", payload, format="json")
        self.assertEqual(r.status_code, 404)
        MockTx.assert_not_called()


class PayConfirmIdempotencyTest(TestCase):
    def setUp(self):
        self.user = make_student()
        self.prod = make_producto(valor_neto=10000)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _commit_result(self, token="TOKX"):
        return {
            "status": "AUTHORIZED",
            "amount": 10000,
            "buy_order": f"order_{self.prod.id}_{self.user.id}",
            # Transbank devuelve un datetime ISO; usamos un datetime real para
            # el DateTimeField de TransbankTransaction.
            "transaction_date": timezone.now(),
            "payment_type_code": "VN",
        }

    @patch("sales.views.Transaction")
    def test_confirmacion_doble_no_duplica_venta(self, MockTx):
        instance = MockTx.return_value
        instance.commit.return_value = self._commit_result()

        payload = {
            "token_ws": "TOKX",
            "product_id": self.prod.id,
            "user_id": self.user.id,
            "payment_method": "transbank",
        }
        r1 = self.client.post("/api/v1/sales/pay_confirm/", payload, format="json")
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(TransbankTransaction.objects.filter(token="TOKX").count(), 1)

        # Segundo intento con el mismo token → rechazado, sin segunda venta.
        r2 = self.client.post("/api/v1/sales/pay_confirm/", payload, format="json")
        self.assertIn(r2.status_code, (400, 401))
        self.assertEqual(TransbankTransaction.objects.filter(token="TOKX").count(), 1)

    @patch("sales.views.Transaction")
    def test_confirm_ownership_bloquea_suplantacion(self, MockTx):
        otro = make_student("otro2@example.com")
        payload = {
            "token_ws": "TOKY",
            "product_id": self.prod.id,
            "user_id": otro.id,  # intenta confirmar a nombre de otro
            "payment_method": "transbank",
        }
        r = self.client.post("/api/v1/sales/pay_confirm/", payload, format="json")
        self.assertEqual(r.status_code, 403)


class CompraCursoIndividualTests(TestCase):
    """Compra individual de un curso por un estudiante → acceso 30 días."""

    def setUp(self):
        self.student = make_student()  # sin escuela
        self.curso = make_curso(costo=15000)
        self.client = APIClient()
        self.client.force_authenticate(self.student)

    def _init_payload(self, amount, curso=None, student=None):
        curso = curso or self.curso
        sid = (student or self.student).id
        return {
            "amount": amount,
            "session_id": "s",
            "buy_order": f"order_{curso.id}_{sid}",
            "payment_method": "transbank",
            "item_type": "curso",
        }

    def _commit(self, curso=None, student=None, amount=15000):
        curso = curso or self.curso
        sid = (student or self.student).id
        return {
            "status": "AUTHORIZED",
            "amount": amount,
            "buy_order": f"order_{curso.id}_{sid}",
            "transaction_date": timezone.now(),
            "payment_type_code": "VN",
        }

    def _confirm_payload(self, token="TOKc", curso=None):
        return {
            "token_ws": token,
            "product_id": (curso or self.curso).id,
            "user_id": self.student.id,
            "payment_method": "transbank",
            "item_type": "curso",
        }

    @patch("sales.views.Transaction")
    def test_init_rechaza_precio_incorrecto(self, MockTx):
        r = self.client.post("/api/v1/sales/pay_init/", self._init_payload(1), format="json")
        self.assertEqual(r.status_code, 400)
        MockTx.assert_not_called()

    @patch("sales.views.Transaction")
    def test_compra_otorga_acceso_30_dias(self, MockTx):
        MockTx.return_value.commit.return_value = self._commit()
        r = self.client.post("/api/v1/sales/pay_confirm/", self._confirm_payload(), format="json")
        self.assertEqual(r.status_code, 201, r.data)

        ec = EstudianteCurso.objects.get(estudiante_id=self.student, curso_id=self.curso)
        ak = ec.access_key_id
        self.assertEqual(ak.origen, "purchase")
        # ~30 días de validez.
        dias = (ak.valid_until - timezone.now()).days
        self.assertTrue(29 <= dias <= 30)
        # Venta con curso (sin producto).
        venta = Venta.objects.get(usuario=self.student, curso=self.curso)
        self.assertIsNone(venta.producto)

    @patch("sales.views.Transaction")
    def test_estudiante_sin_escuela_compra_varios_cursos(self, MockTx):
        curso2 = make_curso(nombre="Curso Y", costo=15000)
        MockTx.return_value.commit.return_value = self._commit()
        r1 = self.client.post("/api/v1/sales/pay_confirm/", self._confirm_payload("T1"), format="json")
        self.assertEqual(r1.status_code, 201)

        MockTx.return_value.commit.return_value = self._commit(curso=curso2)
        r2 = self.client.post("/api/v1/sales/pay_confirm/", self._confirm_payload("T2", curso=curso2), format="json")
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(EstudianteCurso.objects.filter(estudiante_id=self.student).count(), 2)

    @patch("sales.views.Transaction")
    def test_no_puede_comprar_dos_veces_el_mismo_curso(self, MockTx):
        MockTx.return_value.commit.return_value = self._commit()
        self.client.post("/api/v1/sales/pay_confirm/", self._confirm_payload("T1"), format="json")

        MockTx.return_value.commit.return_value = self._commit()
        r2 = self.client.post("/api/v1/sales/pay_confirm/", self._confirm_payload("T2"), format="json")
        self.assertEqual(r2.status_code, 409)
        self.assertEqual(EstudianteCurso.objects.filter(estudiante_id=self.student).count(), 1)

    @patch("sales.views.Transaction")
    def test_confirm_revalida_monto_contra_curso(self, MockTx):
        # El commit autoritativo trae un monto que NO coincide con el precio del
        # curso del buy_order → rechazo (defensa ante swap de precio/curso).
        MockTx.return_value.commit.return_value = self._commit(amount=99)
        r = self.client.post("/api/v1/sales/pay_confirm/", self._confirm_payload(), format="json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(EstudianteCurso.objects.filter(estudiante_id=self.student).exists())


class CursoCompradoYEscuelaTests(TestCase):
    """Adopción, desvinculación y extensión sobre cursos comprados."""

    def setUp(self):
        from accounts.tests import make_user
        self.escuela = Escuela.objects.create(
            nombre="E", direccion="x", email="e@e.com", telefono="1",
            basic_key=1,
        )
        self.director = make_user("dir@e.com", is_director=True, escuela=self.escuela)
        self.client = APIClient()

    def test_desvincular_conserva_curso_comprado(self):
        student = make_student("s1@e.com", escuela=self.escuela)
        curso_comprado = make_curso("Comprado", costo=10000)
        curso_escuela = make_curso("De escuela", costo=0)
        ec_p, ak_p = enrol_purchase(student, curso_comprado)
        # Curso otorgado por la escuela (origen='key').
        ak_k = AccessKey.objects.create(valid_until=timezone.now() + timedelta(days=30), origen="key")
        EstudianteCurso.objects.create(estudiante_id=student, curso_id=curso_escuela, access_key_id=ak_k)

        self.client.force_authenticate(self.director)
        r = self.client.post("/api/v1/schools/desvincular-estudiante/", {"user_id": student.id}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

        # El curso comprado permanece; el de escuela se retira.
        self.assertTrue(EstudianteCurso.objects.filter(id=ec_p.id).exists())
        ak_p.refresh_from_db()
        self.assertEqual(ak_p.status, "active")
        self.assertFalse(
            EstudianteCurso.objects.filter(estudiante_id=student, curso_id=curso_escuela).exists()
        )
        student.refresh_from_db()
        self.assertIsNone(student.escuela_id)

    def test_adopcion_conserva_cursos_y_no_consume_cupo(self):
        # Estudiante SIN escuela con un curso comprado.
        student = make_student("s2@e.com", escuela=None)
        curso = make_curso("Comprado2", costo=10000)
        ec, ak = enrol_purchase(student, curso)

        self.client.force_authenticate(self.director)
        r = self.client.post("/api/v1/schools/vincular-estudiante/", {"email": student.email}, format="json")
        self.assertIn(r.status_code, (200, 201), r.data)

        student.refresh_from_db()
        self.assertEqual(student.escuela_id, self.escuela.id)  # adoptado
        self.assertTrue(EstudianteCurso.objects.filter(id=ec.id).exists())  # curso conservado
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_seats_used, 0)  # no consumió cupo

    def test_escuela_extiende_curso_comprado(self):
        student = make_student("s3@e.com", escuela=self.escuela)
        curso = make_curso("Comprado3", costo=10000, profesional=False)
        ec, ak = enrol_purchase(student, curso, dias=10)
        vu_antes = ak.valid_until

        self.client.force_authenticate(self.director)
        r = self.client.post(
            "/api/v1/sales/extender_llave/",
            {"access_key_id": str(ak.id), "days": 30}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        ak.refresh_from_db()
        self.assertGreater(ak.valid_until, vu_antes)  # extendido
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_key, 0)  # consumió 1 llave de la escuela
