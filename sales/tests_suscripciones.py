"""Suscripciones de escuelas: cotización, cambios de plan, transferencia, admin.

Correr:  manage.py test sales.tests_suscripciones
"""
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.tests import make_user
from sales import suscripciones as S
from sales.models import CambioPrecio, DatosFacturacion, PeriodoSuscripcion, Suscripcion, Venta
from sales.testing import dar_cupo, dar_suscripcion, plan_de
from sales.tests_e2e import TransbankFake, crear_escuela, pagar


def cliente(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


class CotizacionYProrrateoTests(TestCase):
    def setUp(self):
        self.escuela = crear_escuela()
        self.e25 = plan_de(25, 'esencial', 90000)
        self.e50 = plan_de(50, 'esencial', 160000)
        self.p25 = plan_de(25, 'pro', 117000)

    def test_sin_suscripcion_es_alta_a_precio_lista(self):
        cot = S.cotizar(self.escuela, self.e25, 12)
        self.assertEqual((cot['tipo'], cot['monto']), ('alta', self.e25.precio_anual))

    def test_periodo_invalido(self):
        with self.assertRaises(S.SuscripcionError):
            S.cotizar(self.escuela, self.e25, 3)

    def test_subida_a_mitad_de_periodo_cobra_la_mitad_de_la_diferencia(self):
        now = timezone.now()
        inicio = now - timedelta(days=15)
        dar_suscripcion(self.escuela, cupos=25, periodo_inicio=inicio, fin=inicio + timedelta(days=30))
        cot = S.cotizar(self.escuela, self.e50, 1, now)
        self.assertEqual(cot['tipo'], 'subida')
        # Tarifa pagada 90.000/30 d; nueva 160.000/mes (≈30–31 d); quedan 15 d.
        self.assertTrue(32000 <= cot["monto"] <= 36000, cot["monto"])
        self.assertEqual(cot['monto'] % 10, 0)

    def test_mismo_plan_vigente_es_renovacion_y_extiende_desde_el_fin(self):
        sus = dar_suscripcion(self.escuela, cupos=25)
        fin = sus.fin
        S.aplicar_compra(self.escuela, self.e25, 1, 90000)
        sus.refresh_from_db()
        self.assertEqual(sus.fin, S.sumar_meses(fin, 1))
        self.assertEqual(sus.precio_periodo, 180000)

    def test_plan_mas_barato_pagado_aplica_desde_el_fin(self):
        sus = dar_suscripcion(self.escuela, cupos=50, precio=160000)
        fin = sus.fin
        cot = S.cotizar(self.escuela, self.e25, 1)
        self.assertEqual(cot['tipo'], 'cambio_renovacion')
        S.aplicar_compra(self.escuela, self.e25, 1, cot['monto'])
        sus.refresh_from_db()
        self.assertEqual((sus.plan_id, sus.plan_siguiente_id, sus.cambio_en), (self.e50.id, self.e25.id, fin))
        # Llega la fecha: el plan cambia solo al leer.
        normal = S.suscripcion_de(self.escuela, fin + timedelta(seconds=1))
        self.assertEqual((normal.plan_id, normal.plan_siguiente_id), (self.e25.id, None))
        self.assertEqual(normal.precio_periodo, 90000)

    def test_bajada_exige_cupos_libres(self):
        sus = dar_suscripcion(self.escuela, cupos=50, precio=160000)
        for i in range(30):
            dar_cupo(sus, make_user(f"c{i}@x.com", is_estudiante=True, escuela=self.escuela))
        with self.assertRaises(S.SuscripcionError) as ctx:
            S.cotizar(self.escuela, self.e25, 1)
        self.assertEqual(ctx.exception.code, 'cupos_insuficientes')

    def test_funciones_por_tier(self):
        self.assertEqual(S.funciones_de(self.escuela), [])
        dar_suscripcion(self.escuela, cupos=25)
        self.assertIn('reasignar_cupos', S.funciones_de(self.escuela))
        self.assertNotIn('sedes', S.funciones_de(self.escuela))
        Suscripcion.objects.filter(escuela=self.escuela).update(plan=self.p25)
        self.assertIn('sedes', S.funciones_de(self.escuela))


@patch("sales.views.Transaction")
class SubidaPorWebpayTests(TestCase):
    def setUp(self):
        self.escuela = crear_escuela()
        self.dora = make_user("dora@a.com", is_director=True, escuela=self.escuela)
        self.e50 = plan_de(50, 'esencial', 160000)
        inicio = timezone.now() - timedelta(days=10)
        self.sus = dar_suscripcion(self.escuela, cupos=25, periodo_inicio=inicio, fin=inicio + timedelta(days=30))

    def test_subida_aplica_al_tiro_y_mantiene_el_fin(self, MockTx):
        TransbankFake(MockTx)
        c = cliente(self.dora)
        cot = c.post("/api/v1/sales/suscripcion/cotizar/", {"plan_id": self.e50.id, "meses": 1}, format="json")
        self.assertEqual(cot.status_code, 200, cot.data)
        self.assertEqual((cot.data["tipo"], cot.data["item_type"]), ("subida", "subida"))
        fin = self.sus.fin
        _, r = pagar(c, self.dora, buy_order=cot.data["buy_order"], amount=cot.data["monto"], item_type="subida")
        self.assertEqual(r.status_code, 201, r.data)
        self.sus.refresh_from_db()
        self.assertEqual((self.sus.plan_id, self.sus.fin), (self.e50.id, fin))
        venta = Venta.objects.get(buy_order=cot.data["buy_order"])
        self.assertEqual(venta.item_type, "subida")
        self.assertTrue(PeriodoSuscripcion.objects.filter(suscripcion=self.sus, motivo="subida").exists())

    def test_plan_inactivo_no_se_vende(self, MockTx):
        TransbankFake(MockTx)
        self.e50.activo = False
        self.e50.save()
        r1, _ = pagar(cliente(self.dora), self.dora, buy_order=f"sus_{self.e50.id}_1_{self.dora.id}",
                      amount=160000, item_type="suscripcion")
        self.assertEqual(r1.status_code, 400)

    def test_estudiante_no_puede_comprar(self, MockTx):
        TransbankFake(MockTx)
        est = make_user("e@x.com", is_estudiante=True, escuela=self.escuela)
        r1, _ = pagar(cliente(est), est, buy_order=f"sus_{self.e50.id}_1_{est.id}", amount=160000,
                      item_type="suscripcion")
        self.assertEqual(r1.status_code, 403)


class DirectorEndpointsTests(TestCase):
    def setUp(self):
        self.escuela = crear_escuela()
        self.dora = make_user("dora@a.com", is_director=True, escuela=self.escuela)
        self.admin = make_user("adm@x.com", is_admin=True)
        self.e25 = plan_de(25, 'esencial', 90000)
        self.e50 = plan_de(50, 'esencial', 160000)
        self.c = cliente(self.dora)

    def test_planes_publicos_sin_login(self):
        self.e50.activo = False
        self.e50.save()
        r = APIClient().get("/api/v1/sales/suscripcion/planes/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([p["id"] for p in r.json()], [self.e25.id])

    def test_bajada_programada_y_deshacer(self):
        dar_suscripcion(self.escuela, cupos=50, precio=160000)
        r = self.c.post("/api/v1/sales/suscripcion/bajada/", {"plan_id": self.e25.id, "meses": 1}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["suscripcion"]["cambio"]["plan"]["id"], self.e25.id)
        self.assertFalse(r.data["suscripcion"]["cambio"]["pagado"])
        r = self.c.delete("/api/v1/sales/suscripcion/bajada/")
        self.assertIsNone(r.data["suscripcion"]["cambio"])

    def test_no_renovar_registra_motivo(self):
        dar_suscripcion(self.escuela, cupos=25)
        r = self.c.post("/api/v1/sales/suscripcion/no-renovar/", {"no_renovara": True, "motivo": "caro"},
                        format="json")
        self.assertTrue(r.data["suscripcion"]["no_renovara"])
        self.assertEqual(Suscripcion.objects.get(escuela=self.escuela).motivo_no_renovar, "caro")

    def test_transferencia_pide_facturacion_y_admin_confirma(self):
        r = self.c.post("/api/v1/sales/suscripcion/transferencia/", {"plan_id": self.e25.id, "meses": 12},
                        format="json")
        self.assertEqual((r.status_code, r.data["code"]), (400, "sin_facturacion"))
        r = self.c.put("/api/v1/sales/suscripcion/facturacion/", {
            "rut": "76.123.456-7", "razon_social": "Escuela A SpA", "giro": "Educación",
            "direccion": "Calle 1", "email": "fact@a.cl",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        r = self.c.post("/api/v1/sales/suscripcion/transferencia/", {"plan_id": self.e25.id, "meses": 1},
                        format="json")
        self.assertEqual(r.data["code"], "periodo_transferencia")
        r = self.c.post("/api/v1/sales/suscripcion/transferencia/", {"plan_id": self.e25.id, "meses": 12},
                        format="json")
        self.assertEqual(r.status_code, 201, r.data)
        pendiente = r.data["transferencias_pendientes"][0]
        self.assertEqual(pendiente["monto"], self.e25.precio_anual)
        self.assertTrue(any("transferencia" in m.subject.lower() for m in mail.outbox))
        self.assertIsNone(r.data["suscripcion"])  # aún no activa

        r = cliente(self.admin).post(f"/api/v1/sales/admin/ventas/{pendiente['id']}/transferencia/",
                                     {"accion": "confirmar", "folio": "F-123"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        sus = Suscripcion.objects.get(escuela=self.escuela)
        self.assertEqual((sus.plan_id, sus.periodo_meses), (self.e25.id, 12))
        self.assertEqual(Venta.objects.get(pk=pendiente["id"]).folio_factura, "F-123")
        self.assertTrue(DatosFacturacion.objects.filter(escuela=self.escuela).exists())

    def test_otro_rol_no_accede(self):
        est = make_user("e@x.com", is_estudiante=True, escuela=self.escuela)
        self.assertEqual(cliente(est).get("/api/v1/sales/suscripcion/").status_code, 403)


class AdminSuscripcionTests(TestCase):
    def setUp(self):
        self.escuela = crear_escuela()
        self.admin = make_user("adm@x.com", is_admin=True)
        self.dora = make_user("dora@a.com", is_director=True, escuela=self.escuela)
        self.c = cliente(self.admin)

    def test_crud_planes_registra_historial_de_precio(self):
        r = self.c.post("/api/v1/sales/admin/planes-suscripcion/", {
            "tier": "esencial", "cupos": 25, "precio_mensual": 90000,
            "precio_semestral": 486000, "precio_anual": 864000,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        pid = r.data["id"]
        self.c.patch(f"/api/v1/sales/admin/planes-suscripcion/{pid}/", {"precio_mensual": 95000}, format="json")
        cambios = CambioPrecio.objects.filter(tipo="plan_suscripcion", objeto_id=pid).order_by("fecha")
        self.assertEqual([c.precio_nuevo for c in cambios], [90000, 95000])
        self.assertEqual(cambios.last().usuario_id, self.admin.id)

    def test_no_borra_plan_en_uso(self):
        sus = dar_suscripcion(self.escuela, cupos=25)
        r = self.c.delete(f"/api/v1/sales/admin/planes-suscripcion/{sus.plan_id}/")
        self.assertEqual(r.status_code, 409)

    def test_cortesia_dias_y_plan(self):
        r = self.c.post(f"/api/v1/sales/admin/suscripciones/{self.escuela.id}/", {"dias": 10}, format="json")
        self.assertEqual(r.data["code"], "sin_suscripcion")
        plan = plan_de(25)
        r = self.c.post(f"/api/v1/sales/admin/suscripciones/{self.escuela.id}/",
                        {"plan_id": plan.id, "meses": 1}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        fin = Suscripcion.objects.get(escuela=self.escuela).fin
        self.c.post(f"/api/v1/sales/admin/suscripciones/{self.escuela.id}/", {"dias": 10}, format="json")
        self.assertEqual(Suscripcion.objects.get(escuela=self.escuela).fin, fin + timedelta(days=10))
        self.assertEqual(PeriodoSuscripcion.objects.filter(motivo="cortesia").count(), 2)

    def test_director_no_usa_endpoints_admin(self):
        r = cliente(self.dora).get(f"/api/v1/sales/admin/suscripciones/{self.escuela.id}/")
        self.assertEqual(r.status_code, 403)


class ComercialSuscripcionesTests(TestCase):
    def setUp(self):
        self.admin = make_user("adm@x.com", is_admin=True)
        self.c = cliente(self.admin)
        self.a = crear_escuela("Escuela A")
        self.b = crear_escuela("Escuela B")
        self.sus_a = dar_suscripcion(self.a, cupos=2, tier="pro")
        self.sus_b = dar_suscripcion(self.b, cupos=5, fin=timezone.now() - timedelta(days=3))  # en gracia
        make_user("dir_a@x.com", is_director=True, escuela=self.a)
        for i in range(2):
            dar_cupo(self.sus_a, make_user(f"al{i}@x.com", is_estudiante=True, escuela=self.a))

    def test_kpis_de_suscripciones_en_panorama(self):
        r = self.c.get("/api/v1/sales/admin/panorama/?dias=30")
        self.assertEqual(r.status_code, 200, r.data)
        k = r.data["suscripciones"]
        self.assertEqual((k["activas"], k["en_gracia"]), (2, 1))
        self.assertEqual(k["por_tier"], {"esencial": 1, "pro": 1})
        self.assertEqual(k["mrr"], self.sus_a.plan.precio_mensual + self.sus_b.plan.precio_mensual)

    def test_oportunidades_de_suscripcion(self):
        r = self.c.get("/api/v1/sales/admin/oportunidades/")
        reglas = {x["clave"]: x for x in r.data["reglas"]}
        self.assertEqual([c["id"] for c in reglas["suscripcion_en_gracia"]["casos"]], [self.b.id])
        self.assertEqual([c["id"] for c in reglas["cupos_llenos"]["casos"]], [self.a.id])
        self.assertEqual([c["id"] for c in reglas["suscripcion_por_vencer"]["casos"]], [])

    def test_ventas_pendientes_por_transferencia(self):
        Venta.objects.create(escuela=self.a, monto_pagado=1000, pay_system="TRANSFERENCIA",
                             payment_status="pendiente", item_type="suscripcion", plan_suscripcion=self.sus_a.plan,
                             periodo_meses=12)
        r = self.c.get("/api/v1/sales/admin/ventas/?estado=pendiente")
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(r.data["results"][0]["item"], f"Plan {self.sus_a.plan.nombre} anual")
        self.assertEqual(self.c.get("/api/v1/sales/admin/ventas/").data["count"], 0)


class TareaDiariaTests(TestCase):
    def setUp(self):
        self.escuela = crear_escuela()
        self.dora = make_user("dora@a.com", is_director=True, escuela=self.escuela)

    def _correos(self, texto):
        return [m for m in mail.outbox if texto.lower() in m.subject.lower()]

    def test_aviso_de_vencimiento_una_sola_vez(self):
        dar_suscripcion(self.escuela, cupos=5, fin=timezone.now() + timedelta(days=2, hours=1))
        r1 = S.tareas_diarias()
        r2 = S.tareas_diarias()
        self.assertEqual((r1["avisos"], r2["avisos"]), (1, 0))
        self.assertEqual(len(self._correos("vence pronto")), 1)

    def test_no_renovara_apaga_los_recordatorios(self):
        dar_suscripcion(self.escuela, cupos=5, fin=timezone.now() + timedelta(days=2))
        Suscripcion.objects.filter(escuela=self.escuela).update(no_renovara=True)
        self.assertEqual(S.tareas_diarias()["avisos"], 0)

    def test_gracia_suspension_y_liberacion(self):
        sus = dar_suscripcion(self.escuela, cupos=5, fin=timezone.now() - timedelta(days=2))
        dar_cupo(sus, make_user("al@a.com", is_estudiante=True, escuela=self.escuela))
        S.tareas_diarias()
        self.assertEqual(len(self._correos("gracia")), 1)
        Suscripcion.objects.filter(pk=sus.pk).update(fin=timezone.now() - timedelta(days=10))
        S.tareas_diarias()
        self.assertEqual(len(self._correos("suspendida")), 1)
        Suscripcion.objects.filter(pk=sus.pk).update(fin=timezone.now() - timedelta(days=40))
        r = S.tareas_diarias()
        self.assertEqual(r["cupos_liberados"], 1)
        self.assertEqual(len(self._correos("liberamos")), 1)
        self.assertFalse(sus.cupos.filter(liberado_en__isnull=True).exists())

    def test_comando(self):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command("tareas_suscripciones", stdout=out)
        self.assertIn("Avisos", out.getvalue())
