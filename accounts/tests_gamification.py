"""Tests de flujos críticos de gamificación y evaluación del estudiante.

Cubre:
  - Economía de vidas: ganar por lección, gastar por error en tests externos,
    rápida sin costo, gate a 0 vidas, regeneración pasiva, examen final exento.
  - Examen final del curso: preguntas SOLO del curso, umbral 80%, cooldown 24h,
    curso completado tras certificado.
  - Emisión de certificado al aprobar el examen final.

Correr:  manage.py test accounts.tests_gamification --settings=autotestAPI.settings.test
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Usuario, Certificado, Prueba
from accounts import gamification, services
from schools.models import Curso, Categoria, Leccion, Ejercicio


def make_student(email="est@example.com", hearts=5):
    u = Usuario.objects.create_user(
        email=email, nombre="Est", apellido="Test", password="x", is_estudiante=True
    )
    u.is_active = True
    u.hearts = hearts
    u.next_heart_regen_at = None
    u.save()
    return u


def make_curso(nombre="Curso Demo"):
    return Curso.objects.create(nombre=nombre, descripcion="d", costo=0, codigo="C1")


def make_ejercicio(curso=None, leccion=None, categoria=None, correcta="a"):
    return Ejercicio.objects.create(
        curso=curso, leccion=leccion, categoria=categoria,
        pregunta="¿?", opcion_a="A", opcion_b="B", opcion_c="C", opcion_d="D",
        respuesta=correcta, explicacion="e",
    )


class VidasEconomyTest(TestCase):
    def setUp(self):
        self.cat = Categoria.objects.create(nombre="General")

    def test_ganar_corazones_topa_en_maximo(self):
        u = make_student(hearts=4)
        self.assertEqual(gamification.ganar_corazones(u, 1), 5)
        # No pasa del máximo.
        self.assertEqual(gamification.ganar_corazones(u, 1), 5)

    def test_completar_leccion_otorga_una_vida_idempotente(self):
        u = make_student(hearts=3)
        curso = make_curso()
        leccion = Leccion.objects.create(curso=curso, nombre="L1", posicion=1, tipo="texto")
        client = APIClient()
        client.force_authenticate(u)

        r1 = client.post("/api/v1/accounts/estudiante-leccion/",
                         {"curso": curso.id, "leccion": leccion.id}, format="json")
        self.assertEqual(r1.status_code, 201)
        u.refresh_from_db()
        self.assertEqual(u.hearts, 4)  # +1

        # Repost de la misma lección: idempotente (200) y NO otorga otra vida.
        r2 = client.post("/api/v1/accounts/estudiante-leccion/",
                         {"curso": curso.id, "leccion": leccion.id}, format="json")
        self.assertEqual(r2.status_code, 200)
        u.refresh_from_db()
        self.assertEqual(u.hearts, 4)

    def test_test_externo_gasta_una_vida_por_error(self):
        u = make_student(hearts=5)
        ejs = [make_ejercicio(categoria=self.cat) for _ in range(3)]
        prueba = services.crear_prueba_con_ejercicios(
            u, ejs, tipo="completa", modalidad="practica"
        )
        # 1 correcta ('a'), 2 incorrectas ('b') → -2 vidas.
        respuestas = {ejs[0].id: "a", ejs[1].id: "b", ejs[2].id: "b"}
        services.submit_prueba(prueba, respuestas)
        u.refresh_from_db()
        self.assertEqual(u.hearts, 3)

    def test_rapida_no_gasta_vidas(self):
        u = make_student(hearts=5)
        ejs = [make_ejercicio(categoria=self.cat) for _ in range(3)]
        prueba = services.crear_prueba_con_ejercicios(u, ejs, tipo="rapida", modalidad="practica")
        respuestas = {e.id: "z" for e in ejs}  # todas mal
        services.submit_prueba(prueba, respuestas)
        u.refresh_from_db()
        self.assertEqual(u.hearts, 5)  # sin cambios

    def test_regeneracion_pasiva(self):
        u = make_student(hearts=0)
        u.next_heart_regen_at = timezone.now() - timedelta(hours=5)  # 2h/vida → +3
        u.save(update_fields=["next_heart_regen_at"])
        gamification.regenerar_recursos(u)
        self.assertEqual(u.hearts, 3)

    def test_gate_bloquea_gimnasio_sin_vidas(self):
        u = make_student(hearts=0)
        client = APIClient()
        client.force_authenticate(u)
        # Simulacro (completa/practica) con 0 vidas → 403 sin_vidas (antes de
        # seleccionar preguntas, así que no requiere banco).
        r = client.post("/api/v1/accounts/tests/generate/", {"tipo": "completa"}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json().get("razon"), "sin_vidas")

    def test_rapida_no_esta_bloqueada_por_vidas(self):
        u = make_student(hearts=0)
        # Banco suficiente para rápida (10).
        for _ in range(10):
            make_ejercicio(categoria=self.cat)
        client = APIClient()
        client.force_authenticate(u)
        r = client.post("/api/v1/accounts/tests/generate/", {"tipo": "rapida"}, format="json")
        self.assertEqual(r.status_code, 201)

    def test_examen_final_exento_del_gate_de_vidas(self):
        u = make_student(hearts=0)
        curso = make_curso()
        for _ in range(5):
            make_ejercicio(curso=curso, categoria=self.cat)
        client = APIClient()
        client.force_authenticate(u)
        r = client.post(
            "/api/v1/accounts/tests/generate/",
            {"tipo": "completa", "modalidad": "evaluacion", "curso_id": curso.id},
            format="json",
        )
        # No bloqueado por vidas (el examen final tiene su propio gate).
        self.assertEqual(r.status_code, 201)
        self.assertNotEqual(r.json().get("razon"), "sin_vidas")


class ExamenFinalTest(TestCase):
    def setUp(self):
        self.cat = Categoria.objects.create(nombre="General")
        self.curso = make_curso()
        self.otro_curso = make_curso("Otro Curso")
        # 10 preguntas del curso, 5 de otro curso (no deben entrar al examen).
        self.curso_ejs = [make_ejercicio(curso=self.curso, categoria=self.cat) for _ in range(10)]
        self.otros_ejs = [make_ejercicio(curso=self.otro_curso, categoria=self.cat) for _ in range(5)]

    def test_examen_final_solo_preguntas_del_curso(self):
        ejs, err = services.seleccionar_ejercicios_de_curso(self.curso)
        self.assertIsNone(err)
        curso_ids = {e.id for e in self.curso_ejs}
        self.assertTrue(all(e.id in curso_ids for e in ejs))
        self.assertEqual(len(ejs), 10)  # se ajusta al total disponible del curso

    def test_umbral_80_reprueba_con_70(self):
        u = make_student()
        ejs, _ = services.seleccionar_ejercicios_de_curso(self.curso)
        prueba = services.crear_prueba_con_ejercicios(
            u, ejs, tipo="completa", modalidad="evaluacion", curso=self.curso
        )
        # 7/10 = 70% → por debajo del 80% del examen final → reprueba, sin cert.
        respuestas = {e.id: ("a" if i < 7 else "b") for i, e in enumerate(ejs)}
        res = services.submit_prueba(prueba, respuestas)
        self.assertFalse(res["aprobado"])
        self.assertFalse(Certificado.objects.filter(estudiante=u, curso=self.curso).exists())

    def test_umbral_80_aprueba_con_80_y_emite_certificado(self):
        u = make_student()
        ejs, _ = services.seleccionar_ejercicios_de_curso(self.curso)
        prueba = services.crear_prueba_con_ejercicios(
            u, ejs, tipo="completa", modalidad="evaluacion", curso=self.curso
        )
        # 8/10 = 80% → aprueba → certificado emitido (vía signal).
        respuestas = {e.id: ("a" if i < 8 else "b") for i, e in enumerate(ejs)}
        res = services.submit_prueba(prueba, respuestas)
        self.assertTrue(res["aprobado"])
        self.assertTrue(Certificado.objects.filter(estudiante=u, curso=self.curso).exists())

    def test_cooldown_tras_entrega_y_curso_completado_tras_cert(self):
        u = make_student()
        # Entrega reciente (reprobada) → cooldown.
        p = Prueba.objects.create(
            estudiante=u, curso=self.curso, tipo="completa", modalidad="evaluacion", aprobado=False
        )
        p.completada_en = timezone.now() - timedelta(hours=2)
        p.save(update_fields=["completada_en"])
        elig = services.elegibilidad_examen_final(u, self.curso)
        self.assertFalse(elig["puede"])
        self.assertEqual(elig["razon"], "cooldown")
        self.assertGreater(elig["retry_after_seconds"], 0)

        # Con certificado → curso completado (sin más intentos).
        Certificado.objects.create(estudiante=u, curso=self.curso, prueba=p)
        elig2 = services.elegibilidad_examen_final(u, self.curso)
        self.assertFalse(elig2["puede"])
        self.assertEqual(elig2["razon"], "curso_completado")
