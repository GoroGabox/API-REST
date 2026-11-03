"""Panel de admin: resumen, estado del sistema y calidad del contenido (solo admin)."""
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Usuario
from sales.models import Venta
from schools.models import Categoria, Curso, Escuela, Leccion, PlanCurso


class AdminPanelTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = Usuario.objects.create_user(
            email="admin@example.com", nombre="Ad", apellido="Min", password="x12345678",
        )
        self.admin.is_staff = True
        self.admin.save(update_fields=["is_staff"])
        self.est = Usuario.objects.create_user(
            email="est@example.com", nombre="Es", apellido="T", password="x12345678", is_estudiante=True,
        )
        self.escuela = Escuela.objects.create(nombre="Escuela Uno", direccion="x", email="e@x.cl", telefono="1", basic_key=2)
        self.curso_ok = Curso.objects.create(nombre="Curso Con Precio", codigo="C1", descripcion="")
        PlanCurso.objects.create(curso=self.curso_ok, dias=7, precio=7000)
        self.cat = Categoria.objects.create(nombre="Legislación")
        Leccion.objects.create(curso=self.curso_ok, categoria=self.cat, nombre="L1", posicion=1, tipo="texto")
        self.curso_sin = Curso.objects.create(nombre="Curso Sin Precio", codigo="C2", descripcion="")
        Venta.objects.create(curso=self.curso_ok, usuario=self.est, monto_pagado=7000,
                             pay_system="webpay", payment_status="AUTHORIZED")
        Venta.objects.create(curso=self.curso_ok, usuario=self.est, monto_pagado=9999,
                             pay_system="webpay", payment_status="FAILED")

    def _get(self, url, user):
        self.client.force_authenticate(user)
        return self.client.get(url)

    def test_solo_admin(self):
        for url in ("/api/v1/accounts/admin/resumen/", "/api/v1/accounts/admin/estado/",
                    "/api/v1/accounts/admin/calidad/"):
            self.assertEqual(self._get(url, self.est).status_code, 403, url)

    def test_resumen_cuenta_solo_ventas_pagadas(self):
        r = self._get("/api/v1/accounts/admin/resumen/?dias=7", self.admin)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["dias"], 7)
        self.assertEqual(r.data["ingresos"], 7000)
        self.assertEqual(r.data["ventas"], 1)
        self.assertEqual(len(r.data["serie"]), 7)
        self.assertEqual(sum(d["monto"] for d in r.data["serie"]), 7000)
        self.assertEqual(r.data["llaves_en_circulacion"], 2)
        self.assertEqual(r.data["escuelas_activas"], 1)
        self.assertEqual(r.data["usuarios_nuevos"]["d7"], 2)
        self.assertEqual([e["nombre"] for e in r.data["escuelas_saldo_bajo"]], ["Escuela Uno"])
        self.assertEqual(r.data["ultimas_ventas"][0]["item"], "Curso Con Precio")

    @mock.patch.dict("os.environ", {"RESEND_API_KEY": "re_secreto_123", "ANTHROPIC_API_KEY": "sk-secreto",
                                    "TBK_ENVIRONMENT": "live"})
    def test_estado_no_expone_secretos(self):
        r = self._get("/api/v1/accounts/admin/estado/", self.admin)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["api"]["db_ok"])
        self.assertEqual(r.data["pagos"]["transbank"], "LIVE")
        self.assertTrue(r.data["email"]["resend_configurado"])
        self.assertTrue(r.data["ia"]["anthropic_configurado"])
        texto = str(r.data)
        self.assertNotIn("re_secreto_123", texto)
        self.assertNotIn("sk-secreto", texto)

    def test_calidad_lista_pendientes(self):
        r = self._get("/api/v1/accounts/admin/calidad/", self.admin)
        self.assertEqual(r.status_code, 200)
        self.assertEqual([c["nombre"] for c in r.data["cursos_sin_precio"]], ["Curso Sin Precio"])
        self.assertEqual([c["nombre"] for c in r.data["cursos_sin_lecciones"]], ["Curso Sin Precio"])
        self.assertEqual(r.data["conteos"]["lecciones_sin_medios"], 1)
        self.assertEqual(r.data["categorias_sin_tematica"][0]["nombre"], "Legislación")
