"""Separación práctica / examen final y analítica de pruebas.

- Solo el examen final del curso cuenta para el progreso (director).
- `me/final-exams/` lista el examen final de cada curso inscrito con su bloqueo.
- `analitica/pruebas|respuestas/` (JSON paginado o CSV) para director y admin.
"""
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Certificado, EstudianteLeccion, Prueba, PruebaEjercicio
from accounts.tests import make_user
from accounts.tests_gamification import make_curso, make_ejercicio
from sales.models import AccessKey, EstudianteCurso
from schools.models import Categoria, Escuela, Leccion


def _entregar(prueba, ejercicios, correctas):
    for i, e in enumerate(ejercicios):
        PruebaEjercicio.objects.create(prueba=prueba, ejercicio=e, correcta=i < correctas, respuesta_estudiante="a")
    prueba.completada_en = timezone.now()
    prueba.total_correctas = correctas
    prueba.score = round(100 * correctas / len(ejercicios), 2)
    prueba.save()
    return prueba


class _Base(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.esc_a = Escuela.objects.create(nombre="Escuela A")
        self.esc_b = Escuela.objects.create(nombre="Escuela B")
        self.admin = make_user("admin@x.cl", is_admin=True)
        self.dir_a = make_user("dir_a@x.cl", is_director=True, escuela=self.esc_a)
        self.est_a = make_user("est_a@x.cl", is_estudiante=True, escuela=self.esc_a)
        self.est_b = make_user("est_b@x.cl", is_estudiante=True, escuela=self.esc_b)
        self.curso = make_curso("Curso Clase B")
        self.cat = Categoria.objects.create(nombre="Señales")
        self.ejs = [make_ejercicio(curso=self.curso, categoria=self.cat) for _ in range(5)]
        EstudianteCurso.objects.create(estudiante_id=self.est_a, curso_id=self.curso, access_key_id=AccessKey.objects.create())

        # est_a: una práctica perfecta y un examen final reprobado.
        self.practica = _entregar(
            Prueba.objects.create(estudiante=self.est_a, curso=self.curso, tipo="rapida", modalidad="practica"),
            self.ejs, 5,
        )
        self.final = _entregar(
            Prueba.objects.create(estudiante=self.est_a, curso=self.curso, tipo="completa", modalidad="evaluacion"),
            self.ejs, 2,
        )
        # est_b (otra escuela): práctica.
        _entregar(Prueba.objects.create(estudiante=self.est_b, tipo="rapida"), self.ejs, 3)


class ClasificacionTest(_Base):
    def test_queryset_y_property(self):
        self.assertEqual(list(Prueba.objects.examenes_finales()), [self.final])
        self.assertNotIn(self.final, Prueba.objects.practica())
        self.assertTrue(self.final.es_examen_final)
        self.assertFalse(self.practica.es_examen_final)
        # Evaluación completa sin curso no es examen final.
        sin_curso = Prueba.objects.create(estudiante=self.est_a, tipo="completa", modalidad="evaluacion")
        self.assertFalse(sin_curso.es_examen_final)

    def test_se_registran_todas(self):
        self.assertEqual(Prueba.objects.filter(estudiante=self.est_a).count(), 2)


class ProgresoDirectorTest(_Base):
    def test_aprobacion_solo_cuenta_examen_final(self):
        self.client.force_authenticate(self.dir_a)
        r = self.client.get(f"/api/v1/accounts/progreso-estudiantes/{self.esc_a.id}/")
        self.assertEqual(r.status_code, 200)
        fila = next(x for x in r.data if x["id"] == self.est_a.id)
        # La práctica (100%) no cuenta; el final está reprobado → 0%.
        self.assertEqual(fila["aprobacionPruebas"], 0)
        self.assertEqual(fila["intentosExamenFinal"], 1)
        self.assertFalse(fila["examenFinalAprobado"])


class GenerarRegistraAnaliticaTest(_Base):
    def test_generate_guarda_categoria_y_creada_en(self):
        from schools.models import PREGUNTAS_SESION_TEMATICA
        for _ in range(PREGUNTAS_SESION_TEMATICA):
            make_ejercicio(categoria=self.cat)
        self.client.force_authenticate(self.est_a)
        r = self.client.post(
            "/api/v1/accounts/tests/generate/", {"tipo": "categoria", "categoria_id": self.cat.id}, format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)
        p = Prueba.objects.get(pk=r.data["prueba_id"])
        self.assertEqual(p.categoria_id, self.cat.id)
        self.assertIsNotNone(p.creada_en)


class MisExamenesFinalesTest(_Base):
    URL = "/api/v1/accounts/me/final-exams/"

    def setUp(self):
        super().setUp()
        self.l1 = Leccion.objects.create(curso=self.curso, nombre="L1", posicion=1)
        self.l2 = Leccion.objects.create(curso=self.curso, nombre="L2", posicion=2)
        self.client.force_authenticate(self.est_a)

    def _item(self):
        r = self.client.get(self.URL)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data), 1)
        return r.data[0]

    def test_bloqueado_hasta_ver_todas_las_lecciones(self):
        EstudianteLeccion.objects.create(estudiante=self.est_a, leccion=self.l1, curso=self.curso)
        item = self._item()
        self.assertEqual(item["curso_id"], self.curso.id)
        self.assertEqual(item["curso_nombre"], "Curso Clase B")
        self.assertFalse(item["puede"])
        self.assertEqual(item["razon"], "lecciones_pendientes")
        self.assertEqual((item["lecciones_completadas"], item["lecciones_total"]), (1, 2))

        EstudianteLeccion.objects.create(estudiante=self.est_a, leccion=self.l2, curso=self.curso)
        item = self._item()
        self.assertTrue(item["puede"])
        self.assertEqual(item["razon"], "ok")

    def test_curso_completado_con_certificado(self):
        Certificado.objects.create(estudiante=self.est_a, curso=self.curso)
        self.assertEqual(self._item()["razon"], "curso_completado")

    def test_sin_inscripciones_lista_vacia(self):
        self.client.force_authenticate(self.est_b)
        self.assertEqual(self.client.get(self.URL).data, [])


class AnaliticaTest(_Base):
    P = "/api/v1/accounts/analitica/pruebas/"
    R = "/api/v1/accounts/analitica/respuestas/"

    def test_estudiante_403_y_anonimo_401(self):
        self.assertEqual(self.client.get(self.P).status_code, 401)
        self.client.force_authenticate(self.est_a)
        self.assertEqual(self.client.get(self.P).status_code, 403)

    def test_director_solo_su_escuela(self):
        self.client.force_authenticate(self.dir_a)
        r = self.client.get(self.P, {"escuela_id": self.esc_b.id})  # se ignora
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["count"], 2)
        self.assertEqual({f["estudiante_id"] for f in r.data["results"]}, {self.est_a.id})

    def test_admin_todo_y_filtros(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(self.P).data["count"], 3)
        self.assertEqual(self.client.get(self.P, {"escuela_id": self.esc_b.id}).data["count"], 1)
        r = self.client.get(self.P, {"ambito": "final"})
        self.assertEqual(r.data["count"], 1)
        fila = r.data["results"][0]
        self.assertEqual(fila["prueba_id"], self.final.id)
        self.assertTrue(fila["es_examen_final"])
        self.assertTrue(fila["cuenta_progreso"])
        self.assertEqual(fila["total_preguntas"], 5)
        self.assertEqual(fila["escuela_nombre"], "Escuela A")
        self.assertEqual(self.client.get(self.P, {"ambito": "practica"}).data["count"], 2)
        manana = (timezone.localdate() + timezone.timedelta(days=1)).isoformat()
        self.assertEqual(self.client.get(self.P, {"desde": manana}).data["count"], 0)

    def test_parametros_invalidos_400(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(self.P, {"ambito": "x"}).status_code, 400)
        self.assertEqual(self.client.get(self.P, {"desde": "04-11-2026"}).status_code, 400)

    def test_respuestas_una_fila_por_respuesta(self):
        self.client.force_authenticate(self.admin)
        r = self.client.get(self.R, {"ambito": "final"})
        self.assertEqual(r.data["count"], 5)
        fila = r.data["results"][0]
        self.assertEqual(fila["categoria_nombre"], "Señales")
        self.assertTrue(fila["es_examen_final"])

    def test_csv(self):
        self.client.force_authenticate(self.dir_a)
        r = self.client.get(self.P, {"formato": "csv"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r["Content-Disposition"])
        texto = b"".join(r.streaming_content).decode("utf-8")
        lineas = texto.lstrip("﻿").strip().splitlines()
        self.assertTrue(lineas[0].startswith("prueba_id,estudiante_id"))
        self.assertEqual(len(lineas), 3)  # encabezado + 2 pruebas de su escuela
