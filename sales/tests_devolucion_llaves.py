"""Devolución de llaves asignadas que el estudiante nunca usó.

Reloj parcheado (como `tests_vencimiento`): se asigna, se avanza el tiempo y se
corre `devolver_llaves_no_usadas`.

Correr:  manage.py test sales.tests_devolucion_llaves
"""
import importlib
from datetime import timedelta

from django.test import TestCase

from accounts.models import EstudianteLeccion, Notificacion
from accounts.tests import make_user
from schools.models import Leccion
from sales.models import AccessKey, EstudianteCurso, MovimientoLlaves
from sales.services import devolver_llaves_no_usadas, generar_codigos
from sales.tests_e2e import crear_curso, crear_escuela
from sales.tests_vencimiento import RelojMixin, T0, UN_SEG


class DevolucionLlavesTests(RelojMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.escuela = crear_escuela(basic_key=10)
        self.director = make_user("dir@e.com", is_director=True, escuela=self.escuela)
        self.alumno = make_user("al@e.com", is_estudiante=True, escuela=self.escuela)
        self.curso = crear_curso()

    def saldo(self):
        self.escuela.refresh_from_db()
        return self.escuela.basic_key

    def activar(self, alumno=None, dias=14, user=None):
        r = self.cliente(user or self.director).post("/api/v1/sales/activar_curso/", {
            "user_id": (alumno or self.alumno).id, "curso_id": self.curso.id, "source": "key", "days": dias,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return self.access_key(alumno or self.alumno)

    def test_vencida_sin_abrir_devuelve_todas_sus_llaves(self):
        ak = self.activar(dias=14)
        self.assertEqual((ak.llaves, ak.cobrada_a_id), (2, self.escuela.pk))
        self.assertEqual(self.saldo(), 8)

        # Aún vigente: no se devuelve.
        self.avanzar_a(ak.valid_until)
        self.assertEqual(devolver_llaves_no_usadas(), [])

        self.avanzar_a(ak.valid_until + UN_SEG)
        filas = devolver_llaves_no_usadas()
        self.assertEqual([f["llaves"] for f in filas], [2])
        self.assertEqual(self.saldo(), 10)
        ak.refresh_from_db()
        self.assertEqual(ak.status, "revoked")
        self.assertIsNotNone(ak.devuelta_en)
        self.assertTrue(MovimientoLlaves.objects.filter(escuela=self.escuela, cantidad=2,
                                                        motivo="devolucion").exists())
        self.assertTrue(Notificacion.objects.filter(usuario=self.director, titulo="Llaves devueltas").exists())

        # Idempotente.
        self.assertEqual(devolver_llaves_no_usadas(), [])
        self.assertEqual(self.saldo(), 10)

    def test_dry_run_no_toca_nada(self):
        ak = self.activar(dias=7)
        self.avanzar_a(ak.valid_until + UN_SEG)
        self.assertEqual(len(devolver_llaves_no_usadas(dry_run=True)), 1)
        self.assertEqual(self.saldo(), 9)
        ak.refresh_from_db()
        self.assertIsNone(ak.devuelta_en)

    def test_abrir_el_curso_cuenta_como_uso(self):
        ak = self.activar(dias=7)
        r = self.cliente(self.alumno).get(f"/api/v1/schools/courses/{self.curso.id}/units/")
        self.assertEqual(r.status_code, 200, r.data)
        ak.refresh_from_db()
        self.assertIsNotNone(ak.usada_en)
        self.avanzar_a(ak.valid_until + UN_SEG)
        self.assertEqual(devolver_llaves_no_usadas(), [])
        self.assertEqual(self.saldo(), 9)

    def test_avance_previo_cuenta_como_uso(self):
        # Llave anterior a `usada_en`: una lección completada basta.
        ak = self.activar(dias=7)
        EstudianteLeccion.objects.create(estudiante=self.alumno, curso=self.curso,
                                         leccion=Leccion.objects.filter(curso=self.curso).first())
        self.avanzar_a(ak.valid_until + UN_SEG)
        self.assertEqual(devolver_llaves_no_usadas(), [])

    def test_alumno_sin_escuela_no_aplica(self):
        ak = self.activar(dias=7)
        self.alumno.escuela = None
        self.alumno.save(update_fields=["escuela"])
        self.avanzar_a(ak.valid_until + UN_SEG)
        self.assertEqual(devolver_llaves_no_usadas(), [])
        self.assertEqual(self.saldo(), 9)

    def test_activacion_admin_no_cobra_ni_devuelve(self):
        admin = make_user("adm@e.com", is_admin=True)
        ak = self.activar(dias=7, user=admin)
        self.assertEqual(ak.llaves, 0)
        self.avanzar_a(ak.valid_until + UN_SEG)
        self.assertEqual(devolver_llaves_no_usadas(), [])
        self.assertEqual(self.saldo(), 10)

    def test_codigo_vencido_sin_canjear_no_se_devuelve(self):
        codigo = generar_codigos(escuela_id=self.escuela.pk, cantidad=1, dias=7)[0]
        self.avanzar_a(codigo.valid_until + UN_SEG)
        self.assertEqual(devolver_llaves_no_usadas(), [])
        self.assertEqual(self.saldo(), 9)

    def test_extension_suma_y_reactiva_tras_devolucion(self):
        ak = self.activar(dias=7)
        r = self.cliente(self.director).post("/api/v1/sales/extender_llave/",
                                             {"access_key_id": str(ak.id), "days": 7}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        ak.refresh_from_db()
        self.assertEqual(ak.llaves, 2)
        self.assertEqual(self.saldo(), 8)

        self.avanzar_a(ak.valid_until + UN_SEG)
        self.assertEqual([f["llaves"] for f in devolver_llaves_no_usadas()], [2])
        self.assertEqual(self.saldo(), 10)

        # Extender una llave ya devuelta la cobra de nuevo desde cero.
        r = self.cliente(self.director).post("/api/v1/sales/extender_llave/",
                                             {"access_key_id": str(ak.id), "days": 7}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        ak.refresh_from_db()
        self.assertEqual((ak.llaves, ak.status, ak.devuelta_en), (1, "active", None))
        self.assertEqual(self.saldo(), 9)

    def test_reactivar_curso_tras_devolucion_reusa_inscripcion(self):
        ak = self.activar(dias=7)
        self.avanzar_a(ak.valid_until + UN_SEG)
        devolver_llaves_no_usadas()
        nueva = self.activar(dias=7)
        self.assertNotEqual(nueva.pk, ak.pk)
        self.assertEqual(EstudianteCurso.objects.filter(estudiante_id=self.alumno).count(), 1)


class BackfillMigracionTests(TestCase):
    def test_rellena_llaves_y_escuela_de_llaves_previas(self):
        from django.apps import apps
        mig = importlib.import_module("sales.migrations.0033_llaves_devolucion")
        escuela = crear_escuela()
        alumno = make_user("b@e.com", is_estudiante=True, escuela=escuela)
        curso = crear_curso()
        ak = AccessKey.objects.create(origen="key", valid_until=T0 + timedelta(days=14))
        AccessKey.objects.filter(pk=ak.pk).update(valid_from=T0)
        EstudianteCurso.objects.create(estudiante_id=alumno, curso_id=curso, access_key_id=ak)
        mig.rellenar(apps, None)
        ak.refresh_from_db()
        self.assertEqual((ak.llaves, ak.cobrada_a_id, ak.usada_en), (2, escuela.pk, None))
