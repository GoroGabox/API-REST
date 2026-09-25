"""Tests de flujos críticos de gamificación y evaluación del estudiante.

Cubre:
  - Sin vidas: nada las otorga, consume ni bloquea.
  - Examen final del curso: preguntas SOLO del curso, umbral 80%, reintento
    sin espera, curso completado tras certificado.
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


def make_student(email="est@example.com"):
    u = Usuario.objects.create_user(
        email=email, nombre="Est", apellido="Test", password="x", is_estudiante=True
    )
    u.is_active = True
    u.save()
    return u


def make_curso(nombre="Curso Demo"):
    # El precio del curso vive en PlanCurso (schools); `Curso.costo` fue eliminado.
    return Curso.objects.create(nombre=nombre, descripcion="d", codigo="C1")


def make_ejercicio(curso=None, leccion=None, categoria=None, correcta="a"):
    return Ejercicio.objects.create(
        curso=curso, leccion=leccion, categoria=categoria,
        pregunta="¿?", opcion_a="A", opcion_b="B", opcion_c="C", opcion_d="D",
        respuesta=correcta, explicacion="e",
    )


class SinVidasTest(TestCase):
    """Las vidas fueron eliminadas: nada las otorga, consume ni bloquea."""

    def setUp(self):
        self.cat = Categoria.objects.create(nombre="General")

    def test_modelo_sin_vidas(self):
        u = make_student()
        self.assertFalse(hasattr(u, "hearts"))
        self.assertFalse(hasattr(u, "next_heart_regen_at"))

    def test_completar_leccion_idempotente(self):
        u = make_student()
        curso = make_curso()
        leccion = Leccion.objects.create(curso=curso, nombre="L1", posicion=1, tipo="texto")
        client = APIClient()
        client.force_authenticate(u)

        r1 = client.post("/api/v1/accounts/estudiante-leccion/",
                         {"curso": curso.id, "leccion": leccion.id}, format="json")
        self.assertEqual(r1.status_code, 201)
        # Repost de la misma lección: idempotente (200).
        r2 = client.post("/api/v1/accounts/estudiante-leccion/",
                         {"curso": curso.id, "leccion": leccion.id}, format="json")
        self.assertEqual(r2.status_code, 200)

    def test_submit_no_expone_corazones(self):
        u = make_student()
        ejs = [make_ejercicio(categoria=self.cat) for _ in range(3)]
        prueba = services.crear_prueba_con_ejercicios(
            u, ejs, tipo="completa", modalidad="practica"
        )
        res = services.submit_prueba(prueba, {e.id: "b" for e in ejs})  # todas mal
        self.assertNotIn("corazones_restantes", res)

    def test_rapida_generable(self):
        u = make_student()
        for _ in range(10):
            make_ejercicio(categoria=self.cat)
        client = APIClient()
        client.force_authenticate(u)
        r = client.post("/api/v1/accounts/tests/generate/", {"tipo": "rapida"}, format="json")
        self.assertEqual(r.status_code, 201)

    def test_examen_final_generable(self):
        u = make_student()
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
        self.assertEqual(r.status_code, 201)


class ExamenFinalTest(TestCase):
    def setUp(self):
        self.cat = Categoria.objects.create(nombre="General")
        self.curso = make_curso()
        self.otro_curso = make_curso("Otro Curso")
        # 10 preguntas del curso, 5 de otro curso (no deben entrar al examen).
        self.curso_ejs = [make_ejercicio(curso=self.curso, categoria=self.cat) for _ in range(10)]
        self.otros_ejs = [make_ejercicio(curso=self.otro_curso, categoria=self.cat) for _ in range(5)]

    def test_lecciones_pendientes_bloquean_y_se_liberan_al_completar(self):
        from accounts.models import EstudianteLeccion
        u = make_student()
        l1 = Leccion.objects.create(curso=self.curso, nombre="L1", posicion=1, tipo="texto")
        l2 = Leccion.objects.create(curso=self.curso, nombre="L2", posicion=2, tipo="texto")
        EstudianteLeccion.objects.create(estudiante=u, leccion=l1, curso=self.curso)

        elig = services.elegibilidad_examen_final(u, self.curso)
        self.assertFalse(elig["puede"])
        self.assertEqual(elig["razon"], "lecciones_pendientes")
        self.assertEqual((elig["lecciones_completadas"], elig["lecciones_total"]), (1, 2))

        # El endpoint de generación aplica el mismo gate.
        client = APIClient()
        client.force_authenticate(u)
        r = client.post(
            "/api/v1/accounts/tests/generate/",
            {"tipo": "completa", "modalidad": "evaluacion", "curso_id": self.curso.id},
            format="json",
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json().get("razon"), "lecciones_pendientes")

        EstudianteLeccion.objects.create(estudiante=u, leccion=l2, curso=self.curso)
        elig2 = services.elegibilidad_examen_final(u, self.curso)
        self.assertTrue(elig2["puede"])
        self.assertEqual(elig2["razon"], "ok")

    def test_total_preguntas_es_el_real_del_curso(self):
        u = make_student()
        elig = services.elegibilidad_examen_final(u, self.curso)
        self.assertEqual(elig["total_preguntas"], 10)  # el curso tiene 10 (< 35)

    def test_sesion_tematica_tiene_15_preguntas(self):
        for _ in range(16):
            make_ejercicio(categoria=self.cat)
        ejs, err = services.seleccionar_ejercicios("categoria", self.cat.id)
        self.assertIsNone(err)
        self.assertEqual(len(ejs), 15)

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

    def test_reintento_inmediato_y_curso_completado_tras_cert(self):
        u = make_student()
        # Entrega reciente (reprobada) → puede reintentar de inmediato.
        p = Prueba.objects.create(
            estudiante=u, curso=self.curso, tipo="completa", modalidad="evaluacion", aprobado=False
        )
        p.completada_en = timezone.now() - timedelta(minutes=1)
        p.save(update_fields=["completada_en"])
        elig = services.elegibilidad_examen_final(u, self.curso)
        self.assertTrue(elig["puede"])
        self.assertEqual(elig["razon"], "ok")
        self.assertEqual(elig["ultimo_intento"], p.completada_en)
        self.assertNotIn("retry_after_seconds", elig)

        # Con certificado → curso completado (sin más intentos).
        Certificado.objects.create(estudiante=u, curso=self.curso, prueba=p)
        elig2 = services.elegibilidad_examen_final(u, self.curso)
        self.assertFalse(elig2["puede"])
        self.assertEqual(elig2["razon"], "curso_completado")


class MultiRespuestaTest(TestCase):
    """Corrección de preguntas de selección múltiple (varias opciones correctas).

    La respuesta es correcta solo si el estudiante marca EXACTAMENTE el conjunto
    correcto (sin faltar ni sobrar). Las preguntas de respuesta única siguen
    funcionando como antes.
    """

    def setUp(self):
        self.cat = Categoria.objects.create(nombre="General")
        self.u = make_student()

    def _multi(self, correctas):
        e = make_ejercicio(categoria=self.cat)
        e.multiple = True
        e.respuestas_correctas = correctas
        e.save(update_fields=["multiple", "respuestas_correctas"])
        return e

    def _corregir(self, ejercicio, seleccion):
        prueba = services.crear_prueba_con_ejercicios(self.u, [ejercicio], tipo="rapida")
        res = services.submit_prueba(prueba, {ejercicio.id: seleccion})
        return res["detalles"][0]["correcta"]

    def test_multi_set_exacto_aprueba(self):
        e = self._multi(["a", "c"])
        self.assertTrue(self._corregir(e, ["a", "c"]))
        self.assertTrue(self._corregir(e, ["c", "a"]))   # orden no importa
        self.assertTrue(self._corregir(e, "a,c"))         # string separado

    def test_multi_incompleta_o_con_extra_reprueba(self):
        e = self._multi(["a", "c"])
        self.assertFalse(self._corregir(e, ["a"]))        # falta una
        self.assertFalse(self._corregir(e, ["a", "c", "b"]))  # sobra una
        self.assertFalse(self._corregir(e, []))           # vacío

    def test_single_sigue_funcionando(self):
        e = make_ejercicio(categoria=self.cat, correcta="a")  # respuesta='a'
        self.assertTrue(self._corregir(e, "a"))
        self.assertFalse(self._corregir(e, "b"))


class CalificarPruebaGratisTest(TestCase):
    """Corrección de práctica pública (sin login), incl. multi-respuesta."""

    def setUp(self):
        from rest_framework.test import APIClient
        self.client = APIClient()
        self.cat = Categoria.objects.create(nombre="General")
        self.single = make_ejercicio(categoria=self.cat, correcta="a")  # respuesta='a'
        self.multi = make_ejercicio(categoria=self.cat)
        self.multi.multiple = True
        self.multi.respuestas_correctas = ["a", "c"]
        self.multi.save(update_fields=["multiple", "respuestas_correctas"])

    def _grade(self, respuestas):
        from django.urls import reverse
        return self.client.post(reverse("grade_free_test"), {"respuestas": respuestas}, format="json")

    def test_corrige_single_y_multi_sin_login(self):
        r = self._grade({
            str(self.single.id): "a",           # correcta
            str(self.multi.id): ["a", "c"],      # correcta (set exacto)
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["total"], 2)
        self.assertEqual(r.data["total_correctas"], 2)
        self.assertEqual(r.data["score"], 100)

    def test_multi_incompleta_reprueba(self):
        r = self._grade({
            str(self.single.id): "b",            # incorrecta
            str(self.multi.id): ["a"],           # incompleta → incorrecta
        })
        self.assertEqual(r.data["total_correctas"], 0)
        # No expone la clave en las preguntas, pero sí en el detalle de corrección.
        det = {d["pregunta_id"]: d for d in r.data["detalles"]}
        self.assertEqual(det[self.multi.id]["opciones_correctas"], ["a", "c"])

    def test_body_invalido_400(self):
        from django.urls import reverse
        r = self.client.post(reverse("grade_free_test"), {"respuestas": "x"}, format="json")
        self.assertEqual(r.status_code, 400)


class NivelesPorTemaTest(TestCase):
    """Nivel por tema ('reforzar'/'progreso'/'dominado'/None) + recomendación.
    Solo temas disponibles (≥ PREGUNTAS_SESION_TEMATICA ejercicios)."""

    def setUp(self):
        from schools.models import PREGUNTAS_SESION_TEMATICA as MIN
        self.u = make_student()
        self.cats = {}
        for nombre in ("A", "B", "C", "D", "E"):
            cat = Categoria.objects.create(nombre=nombre)
            self.cats[nombre] = (cat, [make_ejercicio(categoria=cat) for _ in range(MIN)])
        # Tema sin preguntas suficientes: nunca se lista.
        self.corta = Categoria.objects.create(nombre="Corta")
        self.corta_ejs = [make_ejercicio(categoria=self.corta) for _ in range(3)]

    def _responder(self, ejercicios, correctas, hace_min=0):
        from accounts.models import PruebaEjercicio
        p = Prueba.objects.create(estudiante=self.u, tipo="categoria", modalidad="practica")
        p.completada_en = timezone.now() - timedelta(minutes=hace_min)
        p.save(update_fields=["completada_en"])
        for i, e in enumerate(ejercicios):
            PruebaEjercicio.objects.create(prueba=p, ejercicio=e, correcta=i < correctas)

    def _niveles(self):
        res = services.niveles_por_tema(self.u)
        return {t["categoria_id"]: t["nivel"] for t in res["temas"]}, res["recomendada"]

    def test_umbrales_y_recomendacion_mas_dificil(self):
        A, B, C, D, _ = (self.cats[k] for k in "ABCDE")
        self._responder(A[1][:5], 2)   # 40% → reforzar
        self._responder(B[1][:5], 3)   # 60% → progreso
        self._responder(C[1][:5], 4)   # 80% → dominado
        self._responder(D[1][:4], 0)   # 4 respuestas → sin nivel
        self._responder(self.corta_ejs, 0)
        niveles, reco = self._niveles()
        self.assertEqual(niveles[A[0].id], "reforzar")
        self.assertEqual(niveles[B[0].id], "progreso")
        self.assertEqual(niveles[C[0].id], "dominado")
        self.assertIsNone(niveles[D[0].id])
        self.assertNotIn(self.corta.id, niveles)
        self.assertEqual(reco, {"categoria_id": A[0].id, "motivo": "mas_dificil"})

    def test_ventana_de_respuestas_recientes(self):
        cat, ejs = self.cats["A"]
        extra = [make_ejercicio(categoria=cat) for _ in range(10)]
        self._responder(ejs[:5], 0, hace_min=60)            # 5 antiguas, todas mal
        self._responder(ejs[5:] + extra[:10], 20, hace_min=1)  # 20 recientes, todas bien
        niveles, _ = self._niveles()
        self.assertEqual(niveles[cat.id], "dominado")

    def test_recomienda_sin_practicar_o_ninguna(self):
        for k in "ABCD":
            self._responder(self.cats[k][1][:5], 5)
        _, reco = self._niveles()
        self.assertEqual(reco, {"categoria_id": self.cats["E"][0].id, "motivo": "sin_practicar"})
        self._responder(self.cats["E"][1][:5], 5)
        _, reco2 = self._niveles()
        self.assertIsNone(reco2)

    def test_endpoint_requiere_sesion_y_no_expone_porcentajes(self):
        self._responder(self.cats["A"][1][:5], 1)
        anon = APIClient()
        self.assertEqual(anon.get("/api/v1/accounts/me/temas/").status_code, 401)
        client = APIClient()
        client.force_authenticate(self.u)
        r = client.get("/api/v1/accounts/me/temas/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(set(body), {"temas", "recomendada"})
        self.assertEqual(set(body["temas"][0]), {"categoria_id", "nivel"})
