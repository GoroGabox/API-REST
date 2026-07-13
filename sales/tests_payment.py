"""Tests del flujo de pago (Transbank) — foco en seguridad e idempotencia.

Cubre:
  - Anti price-tampering: el monto se valida contra el precio autoritativo del
    Producto server-side (no se confía en el `amount` del cliente).
  - Ownership: no se puede iniciar/confirmar un pago suplantando a otro usuario.
  - Idempotencia: un mismo token no puede confirmar dos ventas.

Transbank se mockea (no toca la pasarela real).

Correr:  manage.py test sales.tests_payment --settings=autotestAPI.settings.test
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Usuario
from sales.models import Producto, TransbankTransaction


def make_student(email="comprador@example.com"):
    u = Usuario.objects.create_user(
        email=email, nombre="Comp", apellido="Rador", password="x", is_estudiante=True
    )
    u.is_active = True
    u.save()
    return u


def make_producto(valor_neto=10000, descuento=0):
    return Producto.objects.create(
        nombre="Llave básica", tipo="llave", valor_neto=valor_neto,
        descuento=descuento, descripcion="d", cant_basic_key=1,
    )


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
