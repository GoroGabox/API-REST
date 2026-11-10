"""Sección Comercial del admin (sales/admin/...): panorama, ventas, precios,
cartera y oportunidades. Solo admin; solo cuentan ventas pagadas."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Usuario
from sales import comercial
from sales.models import AccessKey, EstudianteCurso, IntentoPago, MovimientoLlaves, Producto, Venta
from sales.services import generar_codigos
from schools.models import Curso, Escuela, PlanCurso

BASE = "/api/v1/sales/admin/"


def _usuario(email, **kw):
    u = Usuario.objects.create_user(email=email, nombre="N", apellido=email.split("@")[0], password="x12345678", **kw)
    u.is_active = True
    u.save()
    return u


def _venta(fecha=None, **kw):
    v = Venta.objects.create(pay_system="TRANSBANK", payment_status=kw.pop("estado", "AUTHORIZED"), **kw)
    if fecha:
        Venta.objects.filter(pk=v.pk).update(fecha_venta=fecha)
    return v


class ComercialBase(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.client = APIClient()
        self.admin = _usuario("admin@example.com")
        self.admin.is_staff = True
        self.admin.save()
        self.escuela = Escuela.objects.create(nombre="Escuela Uno", email="e@x.cl", telefono="+569", basic_key=3)
        self.director = _usuario("dir@example.com", is_director=True)
        self.director.escuela = self.escuela
        self.director.save()
        self.est = _usuario("est@example.com", is_estudiante=True)
        self.curso = Curso.objects.create(nombre="Clase B", codigo="B", descripcion="")
        self.plan7 = PlanCurso.objects.create(curso=self.curso, dias=7, precio=7000)
        PlanCurso.objects.create(curso=self.curso, dias=14, precio=12000)
        self.p10 = Producto.objects.create(nombre="Pack 10", tipo="llave", valor_neto=11000, descripcion="d", cant_basic_key=10)
        self.p40 = Producto.objects.create(nombre="Pack 40", tipo="llave", valor_neto=42000, descripcion="d", cant_basic_key=40)
        self.client.force_authenticate(self.admin)

    def get(self, url):
        return self.client.get(BASE + url)


class PermisosTests(ComercialBase):
    def test_solo_admin(self):
        self.client.force_authenticate(self.director)
        for url in ("panorama/", "ventas/", "intentos/", "precios/historial/", "cartera/escuelas/", "oportunidades/"):
            self.assertEqual(self.get(url).status_code, 403, url)


class PanoramaTests(ComercialBase):
    def test_kpis_solo_pagadas_y_variacion(self):
        _venta(curso=self.curso, usuario=self.est, monto_pagado=7000, item_type="curso", dias=7)
        _venta(producto=self.p40, escuela=self.escuela, usuario=self.director, monto_pagado=42000, item_type="producto")
        _venta(curso=self.curso, usuario=self.est, monto_pagado=99999, item_type="curso", estado="FAILED")
        # Período anterior (31-60 días atrás): una venta de 24.500.
        _venta(fecha=self.now - timedelta(days=45), curso=self.curso, usuario=self.est,
               monto_pagado=24500, item_type="curso", dias=7)

        r = self.get("panorama/?dias=30")
        self.assertEqual(r.status_code, 200)
        k = r.data["kpis"]
        self.assertEqual((k["ingresos"], k["ventas"], k["compradores"]), (49000, 2, 2))
        self.assertEqual(r.data["anterior"]["ingresos"], 24500)
        self.assertEqual(r.data["variacion"]["ingresos"], 100)
        self.assertEqual(k["recompra"], 50)  # el estudiante ya había comprado
        self.assertEqual(r.data["canales"]["b2c"]["ingresos"], 7000)
        self.assertEqual(r.data["canales"]["b2b"]["ingresos"], 42000)
        self.assertEqual(len(r.data["serie"]), 30)
        self.assertEqual(r.data["mezcla_planes"], [{"dias": 7, "ventas": 1, "ingresos": 7000}])

    def test_conversion_de_intentos(self):
        base = dict(buy_order="order_1_1", item_type="curso", monto=7000, pay_system="TRANSBANK", usuario=self.est)
        IntentoPago.objects.create(estado="autorizado", **base)
        IntentoPago.objects.create(estado="rechazado", **base)
        viejo = IntentoPago.objects.create(estado="iniciado", **base)
        IntentoPago.objects.filter(pk=viejo.pk).update(creado_en=self.now - timedelta(hours=5))
        IntentoPago.objects.create(estado="iniciado", **base)  # en curso: no cuenta
        i = self.get("panorama/?dias=7").data["intentos"]
        self.assertEqual((i["autorizados"], i["rechazados"], i["abandonados"], i["en_curso"]), (1, 1, 1, 1))
        self.assertEqual(i["conversion"], 33)

    def test_rango_invalido(self):
        self.assertEqual(self.get("panorama/?desde=2026-02-30").status_code, 400)


class VentasTests(ComercialBase):
    def test_filtros_y_csv(self):
        _venta(curso=self.curso, usuario=self.est, monto_pagado=7000, item_type="curso", dias=7, buy_order="order_x")
        _venta(producto=self.p40, escuela=self.escuela, usuario=self.director, monto_pagado=42000, item_type="producto")
        r = self.get("ventas/?canal=b2b")
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(r.data["results"][0]["escuela"]["nombre"], "Escuela Uno")
        self.assertEqual(r.data["total_monto"], 42000)
        self.assertEqual(self.get("ventas/?q=order_x").data["count"], 1)
        self.assertEqual(self.get("ventas/?dias=7&canal=b2c").data["results"][0]["dias"], 7)
        csv = b"".join(self.get("ventas/?formato=csv").streaming_content).decode("utf-8")
        self.assertIn("Escuela Uno", csv)
        self.assertEqual(csv.count("\n"), 3)  # encabezado + 2 ventas

    def test_detalle_trae_intentos(self):
        v = _venta(curso=self.curso, usuario=self.est, monto_pagado=7000, item_type="curso", buy_order="order_9_9_7")
        IntentoPago.objects.create(buy_order="order_9_9_7", item_type="curso", monto=7000, pay_system="T",
                                   estado="autorizado", venta=v)
        r = self.get(f"ventas/{v.id}/")
        self.assertEqual(len(r.data["intentos"]), 1)


class PreciosTests(ComercialBase):
    def test_evolucion_usa_historial_y_ventas(self):
        self.client.patch(f"/api/v1/schools/plan-cursos/{self.plan7.id}/", {"precio": 6000}, format="json")
        _venta(curso=self.curso, usuario=self.est, monto_pagado=6000, item_type="curso", dias=7)
        r = self.get(f"precios/evolucion/?tipo=plan_curso&id={self.plan7.id}&dias=30")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([c["precio"] for c in r.data["cambios"]], [7000, 6000])
        self.assertEqual(r.data["cambios"][-1]["usuario"], "N admin")
        self.assertEqual(sum(s["ventas"] for s in r.data["semanas"]), 1)

        h = self.get("precios/historial/").data["results"]
        self.assertEqual(len(h), 1)  # sin las altas
        self.assertEqual((h[0]["precio_anterior"], h[0]["precio_nuevo"], h[0]["variacion"]), (7000, 6000, -14))

    def test_evolucion_404_y_tipo_invalido(self):
        self.assertEqual(self.get("precios/evolucion/?tipo=producto&id=999").status_code, 404)
        self.assertEqual(self.get("precios/evolucion/?tipo=otro&id=1").status_code, 400)


class CarteraYOportunidadesTests(ComercialBase):
    def test_consumo_semanas_saldo_y_sugerencia(self):
        self.escuela.basic_key = 20
        self.escuela.save()
        generar_codigos(escuela_id=self.escuela.id, cantidad=2, dias=7)  # gasta 2 llaves
        self.assertEqual(MovimientoLlaves.objects.get().cantidad, -2)
        MovimientoLlaves.objects.create(escuela=self.escuela, cantidad=-78, motivo="activacion")  # 80 en 8 semanas
        e = next(f for f in self.get("cartera/escuelas/").data["results"] if f["id"] == self.escuela.id)
        self.assertEqual((e["saldo"], e["consumo_semanal"], e["semanas_saldo"]), (18, 10.0, 1.8))

        reglas = {r["clave"]: r for r in self.get("oportunidades/").data["reglas"]}
        caso = reglas["saldo_por_agotarse"]["casos"][0]
        self.assertEqual(caso["email"], "e@x.cl")
        # Faltan 22 llaves para 4 semanas: 3 × Pack 10 ($33.000) sale más barato que Pack 40.
        self.assertIn("3 × Pack 10 ($33.000)", caso["sugerencia"])
        self.assertEqual(caso["valor"], 33000)

    def test_upsell_volumen(self):
        for _ in range(4):
            _venta(producto=self.p10, escuela=self.escuela, usuario=self.director, monto_pagado=11000, item_type="producto")
        caso = next(r for r in self.get("oportunidades/").data["reglas"] if r["clave"] == "upsell_volumen")["casos"][0]
        self.assertEqual(caso["valor"], 2000)  # 44.000 pagados vs Pack 40 a 42.000

    def test_acceso_por_vencer_y_pago_no_concretado(self):
        self.est.last_active_date = timezone.localdate()
        self.est.save()
        ak = AccessKey.objects.create(valid_until=self.now + timedelta(days=3), origen="purchase")
        EstudianteCurso.objects.create(estudiante_id=self.est, curso_id=self.curso, access_key_id=ak)
        IntentoPago.objects.create(buy_order="order_b", item_type="producto", producto=self.p40, monto=42000,
                                   pay_system="T", estado="rechazado", usuario=self.director, escuela=self.escuela)
        reglas = {r["clave"]: r for r in self.get("oportunidades/").data["reglas"]}
        vence = reglas["acceso_por_vencer"]["casos"][0]
        self.assertEqual(vence["email"], "est@example.com")
        self.assertIn("14 días", vence["sugerencia"])
        self.assertEqual(reglas["pago_no_concretado"]["casos"][0]["valor"], 42000)

        csv = b"".join(self.get("oportunidades/?regla=acceso_por_vencer&formato=csv").streaming_content).decode()
        self.assertIn("est@example.com", csv)

    def test_cartera_estudiantes(self):
        _venta(curso=self.curso, usuario=self.est, monto_pagado=7000, item_type="curso", dias=7)
        _venta(curso=self.curso, usuario=self.est, monto_pagado=12000, item_type="curso", dias=14)
        f = self.get("cartera/estudiantes/").data["results"][0]
        self.assertEqual((f["compras"], f["renovaciones"], f["total"]), (2, 1, 19000))


class RecomendarCompraTests(ComercialBase):
    def test_paridad_con_el_front(self):
        packs = [self.p10, self.p40]
        self.assertEqual(comercial.recomendar_compra(36, packs)["costo"], 42000)
        rec = comercial.recomendar_compra(90, packs)
        self.assertEqual(rec["costo"], 95000)  # 2×40 + 10
        self.assertEqual([(p.cant_basic_key, v) for p, v in rec["items"]], [(40, 2), (10, 1)])
