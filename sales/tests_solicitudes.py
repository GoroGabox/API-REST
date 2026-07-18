"""Tests del flujo in-app "Pedir acceso a mi escuela" (SolicitudAcceso).

Estudiante crea la solicitud con el código de la escuela + curso; el director la
aprueba (enrola, consume llave/cupo, vincula la escuela) o la rechaza.

Correr:  manage.py test sales.tests_solicitudes --settings=autotestAPI.settings.test
"""
from datetime import timedelta

from django.core import mail
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Notificacion
from accounts.tests import make_user
from schools.models import Curso, Escuela
from sales.models import AccessKey, EstudianteCurso, SolicitudAcceso


def enrol(student, curso, dias=30):
    """Inscribe al estudiante con acceso vigente (llave a `dias`)."""
    ak = AccessKey.objects.create(valid_until=timezone.now() + timedelta(days=dias), origen="key")
    return EstudianteCurso.objects.create(estudiante_id=student, curso_id=curso, access_key_id=ak)


class SolicitudBaseTest(APITestCase):
    def setUp(self):
        self.escuela = Escuela.objects.create(
            nombre="Escuela A", direccion="x", email="a@a.com", telefono="1",
            basic_key=2, professional_key=1,
        )
        self.otra = Escuela.objects.create(
            nombre="Escuela B", direccion="y", email="b@b.com", telefono="2", basic_key=1,
        )
        self.director = make_user("dir@a.com", is_director=True, escuela=self.escuela)
        self.admin = make_user("adm@a.com", is_admin=True)
        # Estudiante SIN escuela (flujo escuela+curso: la aprobación lo vincula).
        self.est = make_user("est@x.com", is_estudiante=True)
        self.curso = Curso.objects.create(nombre="Básico", descripcion="d", costo=10000, is_profesional=False)

    def crear_solicitud(self, codigo=None, curso_id=None):
        self.client.force_authenticate(self.est)
        return self.client.post("/api/v1/sales/solicitudes/", {
            "codigo_escuela": codigo or self.escuela.codigo,
            "curso_id": curso_id or self.curso.id,
            "mensaje": "Quiero acceso",
        }, format="json")


class CrearSolicitudTests(SolicitudBaseTest):
    def test_escuela_genera_codigo_automatico(self):
        self.assertTrue(self.escuela.codigo)
        self.assertNotEqual(self.escuela.codigo, self.otra.codigo)

    def test_estudiante_crea_solicitud(self):
        r = self.crear_solicitud()
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(r.data["estado"], "pendiente")
        self.assertEqual(SolicitudAcceso.objects.count(), 1)
        # El director recibió notificación.
        self.assertTrue(Notificacion.objects.filter(usuario=self.director, tipo="access_request").exists())

    def test_crear_solicitud_envia_email_al_director(self):
        mail.outbox = []
        r = self.crear_solicitud()
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.director.email, mail.outbox[0].to)

    def test_no_envia_email_si_director_desactivo_preferencia(self):
        self.director.email_notifications = False
        self.director.save(update_fields=["email_notifications"])
        mail.outbox = []
        r = self.crear_solicitud()
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(len(mail.outbox), 0)

    def test_codigo_invalido_404(self):
        r = self.crear_solicitud(codigo="ZZZZZZ")
        self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)

    def test_solicitud_duplicada_pendiente_409(self):
        self.crear_solicitud()
        r = self.crear_solicitud()
        self.assertEqual(r.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(SolicitudAcceso.objects.count(), 1)

    def test_ya_tiene_acceso_vigente_409(self):
        enrol(self.est, self.curso)
        r = self.crear_solicitud()
        self.assertEqual(r.status_code, status.HTTP_409_CONFLICT)

    def test_pertenece_a_otra_escuela_409(self):
        self.est.escuela = self.otra
        self.est.save(update_fields=["escuela"])
        r = self.crear_solicitud()  # pide con código de self.escuela
        self.assertEqual(r.status_code, status.HTTP_409_CONFLICT)

    def test_director_no_puede_crear(self):
        self.client.force_authenticate(self.director)
        r = self.client.post("/api/v1/sales/solicitudes/", {
            "codigo_escuela": self.escuela.codigo, "curso_id": self.curso.id,
        }, format="json")
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)


class ResolverSolicitudTests(SolicitudBaseTest):
    def _pendiente(self):
        return SolicitudAcceso.objects.create(estudiante=self.est, escuela=self.escuela, curso=self.curso)

    def test_director_aprueba_enrola_y_vincula(self):
        sol = self._pendiente()
        self.client.force_authenticate(self.director)
        r = self.client.post(f"/api/v1/sales/solicitudes/{sol.id}/aprobar/", {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        sol.refresh_from_db()
        self.est.refresh_from_db()
        self.escuela.refresh_from_db()
        self.assertEqual(sol.estado, "aprobada")
        self.assertEqual(self.est.escuela_id, self.escuela.id)  # vinculado
        self.assertTrue(EstudianteCurso.objects.filter(estudiante_id=self.est, curso_id=self.curso).exists())
        self.assertEqual(self.escuela.basic_key, 1)  # consumió 1 llave básica
        self.assertTrue(Notificacion.objects.filter(usuario=self.est, tipo="access_request_resolved").exists())

    def test_aprobar_sin_saldo_400_y_queda_pendiente(self):
        self.escuela.basic_key = 0
        self.escuela.basic_access = False
        self.escuela.save()
        sol = self._pendiente()
        self.client.force_authenticate(self.director)
        r = self.client.post(f"/api/v1/sales/solicitudes/{sol.id}/aprobar/", {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        sol.refresh_from_db()
        self.assertEqual(sol.estado, "pendiente")
        self.assertFalse(EstudianteCurso.objects.filter(estudiante_id=self.est).exists())

    def test_director_rechaza(self):
        sol = self._pendiente()
        self.client.force_authenticate(self.director)
        r = self.client.post(f"/api/v1/sales/solicitudes/{sol.id}/rechazar/",
                             {"motivo": "Sin cupos este mes"}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        sol.refresh_from_db()
        self.assertEqual(sol.estado, "rechazada")
        self.assertEqual(sol.motivo_rechazo, "Sin cupos este mes")
        self.assertFalse(EstudianteCurso.objects.filter(estudiante_id=self.est).exists())
        self.assertTrue(Notificacion.objects.filter(usuario=self.est, tipo="access_request_resolved").exists())

    def test_director_de_otra_escuela_no_aprueba(self):
        sol = self._pendiente()
        dir_b = make_user("dirb@b.com", is_director=True, escuela=self.otra)
        self.client.force_authenticate(dir_b)
        r = self.client.post(f"/api/v1/sales/solicitudes/{sol.id}/aprobar/", {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)
        sol.refresh_from_db()
        self.assertEqual(sol.estado, "pendiente")

    def test_estudiante_no_aprueba(self):
        sol = self._pendiente()
        self.client.force_authenticate(self.est)
        r = self.client.post(f"/api/v1/sales/solicitudes/{sol.id}/aprobar/", {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_estudiante_cancela_su_pendiente(self):
        sol = self._pendiente()
        self.client.force_authenticate(self.est)
        r = self.client.post(f"/api/v1/sales/solicitudes/{sol.id}/cancelar/", {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        sol.refresh_from_db()
        self.assertEqual(sol.estado, "cancelada")

    def test_no_reaprobar_resuelta(self):
        sol = self._pendiente()
        sol.estado = "aprobada"
        sol.save(update_fields=["estado"])
        self.client.force_authenticate(self.director)
        r = self.client.post(f"/api/v1/sales/solicitudes/{sol.id}/aprobar/", {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_409_CONFLICT)


class ListarSolicitudesTests(SolicitudBaseTest):
    def test_estudiante_ve_solo_las_suyas(self):
        SolicitudAcceso.objects.create(estudiante=self.est, escuela=self.escuela, curso=self.curso)
        otro = make_user("otro@x.com", is_estudiante=True)
        SolicitudAcceso.objects.create(estudiante=otro, escuela=self.escuela, curso=self.curso)
        self.client.force_authenticate(self.est)
        r = self.client.get("/api/v1/sales/solicitudes/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        ids = [s["estudiante"] for s in (r.data.get("results") or r.data)]
        self.assertTrue(all(i == self.est.id for i in ids))

    def test_director_ve_las_de_su_escuela(self):
        SolicitudAcceso.objects.create(estudiante=self.est, escuela=self.escuela, curso=self.curso)
        self.client.force_authenticate(self.director)
        r = self.client.get("/api/v1/sales/solicitudes/?estado=pendiente")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        rows = r.data.get("results") or r.data
        self.assertEqual(len(rows), 1)
