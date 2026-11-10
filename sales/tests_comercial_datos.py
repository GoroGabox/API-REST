"""Datos para el análisis comercial: intentos de pago, campos de Venta e
historial de precios (CambioPrecio). Transbank se mockea.

Correr:  manage.py test sales.tests_comercial_datos
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Usuario
from schools.models import PlanCurso
from sales.models import CambioPrecio, IntentoPago, Venta
from sales.services import estado_intento
from sales.tests_payment import make_curso, make_producto, make_student


def make_admin():
    u = Usuario.objects.create_user(email="admin@example.com", nombre="Ad", apellido="Min", password="x")
    u.is_staff = True
    u.is_active = True
    u.save()
    return u


class IntentosPagoCursoTests(TestCase):
    def setUp(self):
        self.student = make_student()
        self.curso = make_curso(costo=15000)
        self.client = APIClient()
        self.client.force_authenticate(self.student)
        self.buy_order = f"order_{self.curso.id}_{self.student.id}_7"

    def _init(self, MockTx, token="TOKc"):
        MockTx.return_value.create.return_value = {"url": "https://tbk", "token": token}
        return self.client.post("/api/v1/sales/pay_init/", {
            "amount": 15000, "session_id": "s", "buy_order": self.buy_order,
            "payment_method": "transbank", "item_type": "curso",
        }, format="json")

    def _confirm(self, token="TOKc"):
        return self.client.post("/api/v1/sales/pay_confirm/", {
            "token_ws": token, "product_id": self.curso.id, "user_id": self.student.id,
            "payment_method": "transbank", "item_type": "curso",
        }, format="json")

    @patch("sales.views.Transaction")
    def test_intento_iniciado_y_autorizado_con_venta(self, MockTx):
        r = self._init(MockTx)
        self.assertEqual(r.status_code, 200, r.data)
        intento = IntentoPago.objects.get(token="TOKc")
        self.assertEqual(intento.estado, "iniciado")
        self.assertEqual((intento.item_type, intento.curso_id, intento.dias, intento.monto),
                         ("curso", self.curso.id, 7, 15000))

        MockTx.return_value.commit.return_value = {
            "status": "AUTHORIZED", "amount": 15000, "buy_order": self.buy_order,
            "transaction_date": timezone.now(), "payment_type_code": "VN",
        }
        r = self._confirm()
        self.assertEqual(r.status_code, 201, r.data)

        venta = Venta.objects.get()
        self.assertEqual((venta.item_type, venta.dias, venta.buy_order), ("curso", 7, self.buy_order))
        intento.refresh_from_db()
        self.assertEqual(intento.estado, "autorizado")
        self.assertEqual(intento.venta_id, venta.id)
        self.assertIsNotNone(intento.resuelto_en)

    @patch("sales.views.Transaction")
    def test_pago_rechazado_queda_registrado_sin_venta(self, MockTx):
        self._init(MockTx, token="TOKr")
        MockTx.return_value.commit.return_value = {"status": "FAILED", "response_code": -1}
        r = self._confirm(token="TOKr")
        self.assertFalse(r.data["success"])
        self.assertFalse(Venta.objects.exists())
        intento = IntentoPago.objects.get(token="TOKr")
        self.assertEqual((intento.estado, intento.codigo_respuesta), ("rechazado", "-1"))

    @patch("sales.views.Transaction")
    def test_intento_sin_confirmar_se_reporta_abandonado(self, MockTx):
        self._init(MockTx, token="TOKa")
        intento = IntentoPago.objects.get(token="TOKa")
        self.assertEqual(estado_intento(intento), "iniciado")
        self.assertEqual(estado_intento(intento, now=timezone.now() + timedelta(hours=3)), "abandonado")


class VentaProductoTests(TestCase):
    @patch("sales.views.Transaction")
    def test_venta_de_producto_guarda_item_type_y_buy_order(self, MockTx):
        from schools.models import Escuela
        escuela = Escuela.objects.create(nombre="Escuela A")
        director = Usuario.objects.create_user(
            email="dir@example.com", nombre="Di", apellido="Rector", password="x", is_director=True,
        )
        director.escuela = escuela
        director.is_active = True
        director.save()
        prod = make_producto(valor_neto=10000)
        client = APIClient()
        client.force_authenticate(director)
        bo = f"order_{prod.id}_{director.id}"
        MockTx.return_value.create.return_value = {"url": "https://tbk", "token": "TOKp"}
        client.post("/api/v1/sales/pay_init/", {
            "amount": 10000, "session_id": "s", "buy_order": bo, "payment_method": "transbank",
        }, format="json")
        MockTx.return_value.commit.return_value = {
            "status": "AUTHORIZED", "amount": 10000, "buy_order": bo,
            "transaction_date": timezone.now(), "payment_type_code": "VN",
        }
        r = client.post("/api/v1/sales/pay_confirm/", {
            "token_ws": "TOKp", "product_id": prod.id, "user_id": director.id, "payment_method": "transbank",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        venta = Venta.objects.get()
        self.assertEqual((venta.item_type, venta.dias, venta.buy_order), ("producto", None, bo))
        intento = IntentoPago.objects.get(token="TOKp")
        self.assertEqual((intento.estado, intento.producto_id, intento.escuela_id), ("autorizado", prod.id, escuela.id))


class CambioPrecioTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_plan_curso_registra_alta_y_cambio_de_precio_con_usuario(self):
        curso = make_curso(costo=15000)  # crea el plan de 7 días (alta)
        plan = PlanCurso.objects.get(curso=curso, dias=7)
        alta = CambioPrecio.objects.get(tipo="plan_curso", objeto_id=plan.id)
        self.assertEqual((alta.precio_anterior, alta.precio_nuevo), (None, 15000))

        r = self.client.patch(f"/api/v1/schools/plan-cursos/{plan.id}/", {"precio": 12000}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        cambio = CambioPrecio.objects.filter(tipo="plan_curso", objeto_id=plan.id).order_by("-id").first()
        self.assertEqual((cambio.precio_anterior, cambio.precio_nuevo), (15000, 12000))
        self.assertEqual(cambio.usuario_id, self.admin.id)

    def test_cambio_sin_precio_no_registra(self):
        curso = make_curso(costo=15000)
        plan = PlanCurso.objects.get(curso=curso, dias=7)
        antes = CambioPrecio.objects.count()
        self.client.patch(f"/api/v1/schools/plan-cursos/{plan.id}/", {"etiqueta": "Popular"}, format="json")
        self.assertEqual(CambioPrecio.objects.count(), antes)

    def test_producto_registra_descuento(self):
        prod = make_producto(valor_neto=10000)
        r = self.client.patch(f"/api/v1/sales/productos/{prod.id}/", {"descuento": "10.00"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        cambio = CambioPrecio.objects.filter(tipo="producto", objeto_id=prod.id).order_by("-id").first()
        self.assertEqual(float(cambio.descuento_nuevo), 10.0)
        self.assertEqual(float(cambio.descuento_anterior), 0.0)
        antes = CambioPrecio.objects.count()
        self.client.patch(f"/api/v1/sales/productos/{prod.id}/", {"descuento": "10"}, format="json")
        self.assertEqual(CambioPrecio.objects.count(), antes)
