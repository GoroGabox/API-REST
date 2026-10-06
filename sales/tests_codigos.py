"""Códigos canjeables generados por el director (B13).

El director convierte llaves de su saldo en códigos que reparte fuera de la
plataforma; el estudiante los canjea en el registro o el catálogo.
"""
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.tests import make_user
from schools.models import Curso, Escuela
from sales.models import AccessKey, EstudianteCurso

URL = "/api/v1/sales/codigos/"


class CodigosCanjeTests(APITestCase):
    def setUp(self):
        self.escuela = Escuela.objects.create(
            nombre="E", direccion="x", email="e@e.com", telefono="1", basic_key=10,
        )
        self.otra = Escuela.objects.create(nombre="O", direccion="y", email="o@o.com", telefono="1", basic_key=5)
        self.dir = make_user("dir_cod@x.com", is_director=True, escuela=self.escuela)
        self.dir_otra = make_user("dir_cod2@x.com", is_director=True, escuela=self.otra)
        self.admin = make_user("adm_cod@x.com", is_admin=True)
        self.est = make_user("est_cod@x.com", is_estudiante=True)
        self.curso = Curso.objects.create(nombre="C", descripcion="d")

    def _generar(self, cantidad=2, dias=14, user=None):
        self.client.force_authenticate(user or self.dir)
        return self.client.post(URL, {"cantidad": cantidad, "dias": dias}, format="json")

    def test_generar_descuenta_llaves_y_crea_codigos(self):
        r = self._generar(cantidad=2, dias=14)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(len(r.data["codigos"]), 2)
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_key, 10 - 2 * 2)  # 14 días = 2 llaves c/u
        k = AccessKey.objects.get(key=r.data["codigos"][0]["key"])
        self.assertEqual((k.escuela_id, k.dias, k.llaves, k.status), (self.escuela.id, 14, 2, "active"))
        # El código vence a los 90 días si no se canjea.
        self.assertAlmostEqual((k.valid_until - timezone.now()).days, 89, delta=1)

    def test_sin_saldo_400_y_no_descuenta(self):
        r = self._generar(cantidad=6, dias=14)  # 12 llaves > 10
        self.assertEqual(r.status_code, 400, r.data)
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_key, 10)
        self.assertFalse(AccessKey.objects.filter(escuela=self.escuela).exists())

    def test_validaciones(self):
        self.assertEqual(self._generar(cantidad=0).status_code, 400)
        self.assertEqual(self._generar(cantidad=101).status_code, 400)
        self.assertEqual(self._generar(dias=10).status_code, 400)  # múltiplo de 7

    def test_estudiante_403(self):
        self.client.force_authenticate(self.est)
        self.assertEqual(self.client.post(URL, {"cantidad": 1, "dias": 7}, format="json").status_code, 403)
        self.assertEqual(self.client.get(URL).status_code, 403)

    def test_listado_solo_de_su_escuela(self):
        self._generar(cantidad=1, dias=7)
        self._generar(cantidad=1, dias=7, user=self.dir_otra)
        self.client.force_authenticate(self.dir)
        r = self.client.get(URL)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(r.data["results"][0]["estado"], "disponible")

    def test_canje_inicia_vigencia_y_vincula_escuela(self):
        key = self._generar(cantidad=1, dias=14).data["codigos"][0]["key"]
        self.client.force_authenticate(self.est)
        r = self.client.post("/api/v1/sales/canjear_llave/", {"access_key": key, "curso_id": self.curso.id}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        k = AccessKey.objects.get(key=key)
        self.assertAlmostEqual((k.valid_until - timezone.now()).days, 13, delta=1)
        self.est.refresh_from_db()
        self.assertEqual(self.est.escuela_id, self.escuela.id)
        # Listado del director lo muestra canjeado y por quién.
        self.client.force_authenticate(self.dir)
        row = self.client.get(URL).data["results"][0]
        self.assertEqual(row["estado"], "canjeado")
        self.assertEqual(row["canjeado_por"]["email"], self.est.email)
        self.assertEqual(row["curso"]["id"], self.curso.id)

    def test_canje_no_cambia_escuela_existente(self):
        self.est.escuela = self.otra
        self.est.save()
        key = self._generar(cantidad=1, dias=7).data["codigos"][0]["key"]
        self.client.force_authenticate(self.est)
        r = self.client.post("/api/v1/sales/canjear_llave/", {"access_key": key, "curso_id": self.curso.id}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.est.refresh_from_db()
        self.assertEqual(self.est.escuela_id, self.otra.id)

    def test_anular_devuelve_llaves(self):
        cod = self._generar(cantidad=1, dias=21).data["codigos"][0]
        r = self.client.post(f"{URL}{cod['id']}/anular/")
        self.assertEqual(r.status_code, 200, r.data)
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_key, 10)
        self.assertEqual(AccessKey.objects.get(pk=cod["id"]).status, "revoked")
        # Ya anulado: no se puede canjear ni volver a anular.
        self.assertEqual(self.client.post(f"{URL}{cod['id']}/anular/").status_code, 400)
        self.client.force_authenticate(self.est)
        r = self.client.post("/api/v1/sales/canjear_llave/", {"access_key": cod["key"], "curso_id": self.curso.id}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_no_anula_canjeado(self):
        cod = self._generar(cantidad=1, dias=7).data["codigos"][0]
        self.client.force_authenticate(self.est)
        self.client.post("/api/v1/sales/canjear_llave/", {"access_key": cod["key"], "curso_id": self.curso.id}, format="json")
        self.client.force_authenticate(self.dir)
        self.assertEqual(self.client.post(f"{URL}{cod['id']}/anular/").status_code, 400)
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_key, 9)

    def test_director_otra_escuela_no_anula(self):
        cod = self._generar(cantidad=1, dias=7).data["codigos"][0]
        self.client.force_authenticate(self.dir_otra)
        self.assertEqual(self.client.post(f"{URL}{cod['id']}/anular/").status_code, 404)

    def test_admin_genera_sin_descontar_para_escuela(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post(URL, {"cantidad": 2, "dias": 7, "escuela": self.escuela.id}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_key, 10)
        self.assertEqual(AccessKey.objects.filter(escuela=self.escuela, llaves=0).count(), 2)

    def test_codigo_vencido_sin_canjear(self):
        cod = self._generar(cantidad=1, dias=7).data["codigos"][0]
        AccessKey.objects.filter(pk=cod["id"]).update(valid_until=timezone.now() - timedelta(days=1))
        self.client.force_authenticate(self.dir)
        self.assertEqual(self.client.get(URL).data["results"][0]["estado"], "vencido")
        self.client.force_authenticate(self.est)
        r = self.client.post("/api/v1/sales/canjear_llave/", {"access_key": cod["key"], "curso_id": self.curso.id}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(EstudianteCurso.objects.filter(estudiante_id=self.est).exists())
