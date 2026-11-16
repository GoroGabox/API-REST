"""Quiz de unidad desde el banco de ejercicios, vínculos multi-curso,
importación de cuestionarios, clasificación y consolidación de categorías."""
import io
import json
import tempfile
import zipfile
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import EstudianteLeccion, Prueba
from accounts.tests import make_user
from content_pipeline.exporters.django_importer import clave_de_fila, import_ejercicios
from content_pipeline.processors import cuestionario_docx_parser as P
from offline.tests import dar_acceso
from schools.models import (
    PREGUNTAS_QUIZ_UNIDAD, Categoria, Curso, Ejercicio, EjercicioUnidad, Leccion, Unidad,
)

URL_QUIZ = "/api/v1/accounts/lessons/{}/quiz/"


def crear_ejercicio(cat, n, unidad=None, grupo="", correcta="a"):
    e = Ejercicio.objects.create(
        categoria=cat, pregunta=f"Pregunta {n}?", opcion_a=f"A{n}", opcion_b=f"B{n}", opcion_c=f"C{n}",
        respuesta=f"{correcta.upper()}{n}", respuestas_correctas=[correcta], grupo=grupo,
    )
    if unidad is not None:
        EjercicioUnidad.objects.create(ejercicio=e, unidad=unidad, origen="manual")
    return e


class QuizUnidadBase(TestCase):
    def setUp(self):
        self.cat = Categoria.objects.create(nombre="Conducción Defensiva")
        self.curso = Curso.objects.create(nombre="Curso B", descripcion="d", codigo="B")
        self.u1 = Unidad.objects.create(curso=self.curso, nombre="U1", orden=1)
        self.u2 = Unidad.objects.create(curso=self.curso, nombre="U2", orden=2)
        self.quiz1 = Leccion.objects.create(
            curso=self.curso, unidad=self.u1, nombre="Evaluación 1", posicion=2, tipo="quiz",
            contenido='{"questions": [{"question": "q", "options": ["a","b"], "correct_index": 0}]}')
        self.quiz2 = Leccion.objects.create(
            curso=self.curso, unidad=self.u2, nombre="Evaluación 2", posicion=1, tipo="quiz",
            contenido='{"questions": []}')
        self.al = make_user("al@a.com", is_estudiante=True)
        dar_acceso(self.al, self.curso)
        self.c = APIClient()
        self.c.force_authenticate(self.al)


class QuizUnidadTests(QuizUnidadBase):
    def _llenar_u1(self, n=PREGUNTAS_QUIZ_UNIDAD + 2):
        return [crear_ejercicio(self.cat, i, self.u1) for i in range(n)]

    def test_embebido_si_la_unidad_no_tiene_banco_suficiente(self):
        [crear_ejercicio(self.cat, i, self.u1) for i in range(PREGUNTAS_QUIZ_UNIDAD - 1)]
        r = self.c.post(URL_QUIZ.format(self.quiz1.id))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, {"fuente": "embebido"})
        self.assertFalse(Prueba.objects.filter(tipo="quiz").exists())

    def test_banco_sortea_n_sin_exponer_la_clave(self):
        self._llenar_u1()
        crear_ejercicio(self.cat, 99, self.u2)  # de otra unidad: nunca aparece
        r = self.c.post(URL_QUIZ.format(self.quiz1.id))
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["fuente"], "banco")
        self.assertEqual(r.data["total"], PREGUNTAS_QUIZ_UNIDAD)
        texto = json.dumps(r.data["preguntas"])
        self.assertNotIn("respuesta", texto)
        self.assertNotIn("Pregunta 99", texto)
        prueba = Prueba.objects.get(pk=r.data["prueba_id"])
        self.assertEqual((prueba.tipo, prueba.leccion_id, prueba.curso_id), ("quiz", self.quiz1.id, self.curso.id))

    def test_un_ejercicio_por_grupo_de_variantes(self):
        # 10 grupos con 3 variantes cada uno: el quiz trae uno de cada grupo.
        for g in range(PREGUNTAS_QUIZ_UNIDAD):
            for v in range(3):
                crear_ejercicio(self.cat, g * 10 + v, self.u1, grupo=f"g{g}")
        r = self.c.post(URL_QUIZ.format(self.quiz1.id))
        ids = [p["id"] for p in r.data["preguntas"]]
        grupos = set(Ejercicio.objects.filter(id__in=ids).values_list("grupo", flat=True))
        self.assertEqual(len(grupos), PREGUNTAS_QUIZ_UNIDAD)

    def test_seleccion_multiple_no_entra_al_quiz_de_unidad(self):
        for i in range(PREGUNTAS_QUIZ_UNIDAD):
            e = crear_ejercicio(self.cat, i, self.u1)
            e.multiple = i == 0
            e.save()
        self.assertEqual(self.c.post(URL_QUIZ.format(self.quiz1.id)).data["fuente"], "embebido")

    def test_variantes_cuentan_como_un_grupo_para_el_minimo(self):
        for v in range(PREGUNTAS_QUIZ_UNIDAD + 5):
            crear_ejercicio(self.cat, v, self.u1, grupo="mismo")
        self.assertEqual(self.c.post(URL_QUIZ.format(self.quiz1.id)).data["fuente"], "embebido")

    def test_aprobar_marca_la_leccion_y_reprobar_no(self):
        self._llenar_u1()
        r = self.c.post(URL_QUIZ.format(self.quiz1.id))
        malas = {p["id"]: "b" for p in r.data["preguntas"]}
        res = self.c.post(f"/api/v1/accounts/tests/{r.data['prueba_id']}/submit/", {"respuestas": malas}, format="json")
        self.assertFalse(res.data["aprobado"])
        self.assertFalse(EstudianteLeccion.objects.filter(estudiante=self.al, leccion=self.quiz1).exists())
        r = self.c.post(URL_QUIZ.format(self.quiz1.id))
        buenas = {p["id"]: "a" for p in r.data["preguntas"]}
        res = self.c.post(f"/api/v1/accounts/tests/{r.data['prueba_id']}/submit/", {"respuestas": buenas}, format="json")
        self.assertTrue(res.data["aprobado"])
        self.assertTrue(EstudianteLeccion.objects.filter(estudiante=self.al, leccion=self.quiz1).exists())

    def test_quiz_no_aparece_en_mis_pruebas(self):
        self._llenar_u1()
        self.c.post(URL_QUIZ.format(self.quiz1.id))
        r = self.c.get("/api/v1/accounts/me/tests/")
        filas = r.data["results"] if isinstance(r.data, dict) else r.data
        self.assertEqual(len(filas), 0)

    def test_sin_acceso_403_y_leccion_no_quiz_400(self):
        otro = make_user("otro@a.com", is_estudiante=True)
        c = APIClient()
        c.force_authenticate(otro)
        self.assertEqual(c.post(URL_QUIZ.format(self.quiz1.id)).status_code, 403)
        texto = Leccion.objects.create(curso=self.curso, unidad=self.u1, nombre="L", posicion=1, tipo="texto")
        self.assertEqual(self.c.post(URL_QUIZ.format(texto.id)).status_code, 400)

    def test_filtro_por_categoria(self):
        otra = Categoria.objects.create(nombre="Señales de Tránsito")
        self._llenar_u1()
        r = self.c.post(URL_QUIZ.format(self.quiz1.id) + f"?categoria={otra.id}")
        self.assertEqual(r.data["fuente"], "embebido")

    def test_detalle_de_leccion_expone_quiz_banco(self):
        self._llenar_u1()
        r = self.c.get(f"/api/v1/schools/lessons/{self.quiz1.id}/")
        self.assertTrue(r.data["quiz_banco"])
        r = self.c.get(f"/api/v1/schools/lessons/{self.quiz2.id}/")
        self.assertFalse(r.data["quiz_banco"])

    def test_examen_final_usa_ejercicios_de_las_unidades(self):
        from accounts.services import total_preguntas_examen_final
        self._llenar_u1(3)
        self.assertEqual(total_preguntas_examen_final(self.curso), 3)


class MultiCursoTests(QuizUnidadBase):
    def test_un_ejercicio_sirve_a_dos_cursos(self):
        curso_c = Curso.objects.create(nombre="Curso C", descripcion="d", codigo="C")
        uc = Unidad.objects.create(curso=curso_c, nombre="UC1", orden=1)
        e = crear_ejercicio(self.cat, 1, self.u1)
        EjercicioUnidad.objects.create(ejercicio=e, unidad=uc, origen="clasificacion")
        self.assertEqual(set(e.unidades.values_list("curso__codigo", flat=True)), {"B", "C"})

    def test_filtros_admin(self):
        admin = make_user("ad@a.com", is_admin=True)
        c = APIClient()
        c.force_authenticate(admin)
        crear_ejercicio(self.cat, 1, self.u1)
        crear_ejercicio(self.cat, 2, self.u2)
        crear_ejercicio(self.cat, 3)
        r = c.get(f"/api/v1/schools/exercices/?unidades={self.u1.id}")
        filas = r.data["results"] if isinstance(r.data, dict) else r.data
        self.assertEqual([f["pregunta"] for f in filas], ["Pregunta 1?"])
        r = c.get(f"/api/v1/schools/exercices/?unidades__curso={self.curso.id}")
        filas = r.data["results"] if isinstance(r.data, dict) else r.data
        self.assertEqual(len(filas), 2)
        self.assertEqual(filas[0]["unidades_detalle"][0]["orden"], 1)

    def test_admin_asigna_unidades(self):
        admin = make_user("ad@a.com", is_admin=True)
        c = APIClient()
        c.force_authenticate(admin)
        e = crear_ejercicio(self.cat, 1, self.u1)
        r = c.patch(f"/api/v1/schools/exercices/{e.id}/", {"unidades": [self.u2.id]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(list(e.unidades.values_list("orden", flat=True)), [2])


class ImportEjerciciosTests(QuizUnidadBase):
    def _item(self, opciones, grupo="g1", unidad=1):
        return {"pregunta": "¿Qué hacer?", "opciones": opciones, "respuestas": ["a"],
                "categoria": "Conducción Defensiva", "unidad_orden": unidad, "grupo": grupo,
                "fuente": "Cuestionario X"}

    def test_variantes_con_mismo_enunciado_se_cargan_todas_y_es_idempotente(self):
        items = [self._item({"a": "Uno", "b": "Dos"}), self._item({"a": "Tres", "b": "Cuatro"})]
        s = import_ejercicios(items, curso=self.curso)
        self.assertEqual(s.counters["ejercicio_create"], 2)
        self.assertEqual(s.counters["vinculo_unidad_create"], 2)
        s = import_ejercicios(items, curso=self.curso)
        self.assertEqual(s.counters["duplicado"], 2)
        self.assertEqual(Ejercicio.objects.count(), 2)
        self.assertEqual(EjercicioUnidad.objects.filter(unidad=self.u1).count(), 2)
        self.assertEqual(set(Ejercicio.objects.values_list("grupo", flat=True)), {"g1"})

    def test_ejercicio_existente_de_otro_curso_solo_suma_vinculo(self):
        curso_c = Curso.objects.create(nombre="Curso C", descripcion="d", codigo="C")
        Unidad.objects.create(curso=curso_c, nombre="UC3", orden=3)
        item = self._item({"a": "Uno", "b": "Dos"})
        import_ejercicios([item], curso=self.curso)
        import_ejercicios([{**item, "unidad_orden": 3}], curso=curso_c)
        self.assertEqual(Ejercicio.objects.count(), 1)
        self.assertEqual(set(Ejercicio.objects.get().unidades.values_list("curso__codigo", flat=True)), {"B", "C"})

    def test_repara_alternativas_truncadas_sin_duplicar(self):
        largo = "Frenando suavemente, luego un poco más fuerte cuando comienza a detenerse y después aflojando"
        Ejercicio.objects.create(categoria=self.cat, pregunta="¿Qué hacer?", opcion_a="Uno corto",
                                 opcion_b=largo[:40], respuesta=largo[:40])
        item = self._item({"a": "Uno corto", "b": largo})
        item["respuestas"] = ["b"]
        s = import_ejercicios([item], curso=self.curso)
        self.assertEqual(s.counters["reparado_truncado"], 1)
        e = Ejercicio.objects.get()
        self.assertEqual((e.opcion_b, e.respuesta, e.respuestas_correctas), (largo, largo, ["b"]))
        self.assertEqual(list(e.unidades.values_list("orden", flat=True)), [1])

    def test_dry_run_no_escribe(self):
        s = import_ejercicios([self._item({"a": "Uno", "b": "Dos"})], dry_run=True, curso=self.curso)
        self.assertEqual(s.counters["ejercicio_create"], 1)
        self.assertEqual(Ejercicio.objects.count(), 0)


class ConsolidarCategoriasTests(TestCase):
    def test_mueve_y_borra_heredadas(self):
        vieja = Categoria.objects.create(nombre="Mecanica basica")
        canonica = Categoria.objects.create(nombre="Mecánica y Mantención del Vehículo")
        curso = Curso.objects.create(nombre="B", descripcion="d", codigo="B")
        e = Ejercicio.objects.create(categoria=vieja, pregunta="p", opcion_a="x")
        l = Leccion.objects.create(curso=curso, nombre="L", posicion=1, tipo="texto", categoria=vieja)
        call_command("consolidar_categorias", "--dry-run", stdout=io.StringIO())
        self.assertTrue(Categoria.objects.filter(id=vieja.id).exists())
        call_command("consolidar_categorias", stdout=io.StringIO())
        e.refresh_from_db()
        l.refresh_from_db()
        self.assertEqual((e.categoria_id, l.categoria_id), (canonica.id, canonica.id))
        self.assertFalse(Categoria.objects.filter(id=vieja.id).exists())
        call_command("consolidar_categorias", stdout=io.StringIO())  # idempotente


class AplicarClasificacionTests(QuizUnidadBase):
    def test_aplica_por_clave_y_respeta_confianza(self):
        a = crear_ejercicio(self.cat, 1)
        b = crear_ejercicio(self.cat, 2)
        c = crear_ejercicio(self.cat, 3)
        data = {"curso": "B", "items": [
            {"clave": clave_de_fila(a), "unidad_orden": 2, "categoria": "Señales de Tránsito", "confianza": 0.9},
            {"clave": clave_de_fila(b), "unidad_orden": 1, "categoria": "Inventada", "confianza": 0.2},
            {"clave": clave_de_fila(c), "unidad_orden": None, "categoria": "Primeros Auxilios", "confianza": 0.9},
            {"clave": "no || existe", "unidad_orden": 1, "categoria": "General", "confianza": 1},
        ]}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps(data), encoding="utf-8")
            out = io.StringIO()
            call_command("clasificar_ejercicios", "--aplicar", str(p), stdout=out)
        self.assertEqual(list(a.unidades.values_list("orden", flat=True)), [2])
        a.refresh_from_db()
        self.assertEqual(a.categoria.nombre, "Señales de Tránsito")
        self.assertFalse(b.unidades.exists())                       # baja confianza
        self.assertEqual(Ejercicio.objects.get(pk=b.pk).categoria_id, self.cat.id)  # categoría fuera de lista: no se toca
        self.assertFalse(c.unidades.exists())                       # sin unidad = pool común
        self.assertIn("no_encontrado: 1", out.getvalue())


def _docx(parrafos):
    """.docx mínimo con un párrafo por línea ("• " = ítem de lista de Word)."""
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    ps = []
    for t in parrafos:
        num = ""
        if t.startswith("• "):
            t, num = t[2:], "<w:pPr><w:numPr><w:numId w:val=\"1\"/></w:numPr></w:pPr>"
        ps.append(f"<w:p>{num}<w:r><w:t xml:space=\"preserve\">{t}</w:t></w:r></w:p>")
    xml = f'<w:document xmlns:w="{W}"><w:body>{"".join(ps)}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    buf.seek(0)
    return buf


class FakeLLM:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []

    def complete(self, **kw):
        self.llamadas.append(kw)
        return json.dumps(self.respuestas.pop(0))


class ParserDocxTests(TestCase):
    LINEAS = [
        "CUESTIONARIO", "CAPITULO :1",
        "1) ¿Un siniestro se puede prevenir? (A)", "A) SI   B) NO   C) Depende",
        "19) ¿Por qué se desalinean las ruedas? A",
        "A) Llevar demasiado peso.  B) Altas velocidades.  C) Frenadas bruscas",
        "A) Golpear la cuneta. B) Mucho aire. C) Poco aire",
        "CAPITULO 2", "• ¿Qué indica una luz roja? B", "• Avería leve", "Problema grave", "Nada",
        "CAPITULO 4",
    ]

    def setUp(self):
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f:
            f.write(_docx(self.LINEAS).read())
            self.path = f.name

    def test_lee_y_divide_capitulos(self):
        caps = P.dividir_capitulos(P.leer_lineas(self.path))
        self.assertEqual(sorted(caps), [1, 2, 4])
        self.assertEqual(caps[4], [])
        self.assertTrue(caps[2][0].startswith("• "))

    def test_validacion_exige_texto_del_documento(self):
        fuente = P.norm(" ".join(self.LINEAS))
        ok = {"numero": "1", "pregunta": "¿Un siniestro se puede prevenir?", "respuesta": "a",
              "variantes": [{"a": "SI", "b": "NO", "c": "Depende"}]}
        self.assertEqual(P.validar_item(ok, fuente), [])
        inventada = {**ok, "variantes": [{"a": "SI", "b": "Tal vez", "c": "Depende"}]}
        self.assertTrue(any("no aparece" in e for e in P.validar_item(inventada, fuente)))
        sin_resp = {**ok, "respuesta": "d"}
        self.assertTrue(any("respuesta" in e for e in P.validar_item(sin_resp, fuente)))

    def test_estructura_reintenta_y_genera_un_ejercicio_por_variante(self):
        caps = P.dividir_capitulos(P.leer_lineas(self.path))
        malo = {"preguntas": [{"numero": "19", "pregunta": "¿Por qué se desalinean las ruedas?", "respuesta": "a",
                               "variantes": [{"a": "Inventado", "b": "Altas velocidades.", "c": "Frenadas bruscas"}]}]}
        bueno = {"preguntas": [
            {"numero": "1", "pregunta": "¿Un siniestro se puede prevenir?", "respuesta": "a",
             "variantes": [{"a": "SI", "b": "NO", "c": "Depende"}]},
            {"numero": "19", "pregunta": "¿Por qué se desalinean las ruedas?", "respuesta": "a",
             "variantes": [{"a": "Llevar demasiado peso.", "b": "Altas velocidades.", "c": "Frenadas bruscas"},
                           {"a": "Golpear la cuneta.", "b": "Mucho aire.", "c": "Poco aire"}]}]}
        llm = FakeLLM([malo, bueno])
        res = P.estructurar_capitulo(1, caps[1], client=llm)
        self.assertEqual(len(llm.llamadas), 2)          # reintento con el error
        self.assertIn("no aparece", llm.llamadas[1]["user"])
        ejercicios = P.a_ejercicios(res, prefijo_grupo="adolfo-b", fuente="Cuestionario Adolfo B")
        self.assertEqual(len(ejercicios), 3)
        grupos = [e["grupo"] for e in ejercicios]
        self.assertEqual(grupos, ["adolfo-b-c1-1", "adolfo-b-c1-19", "adolfo-b-c1-19"])
        self.assertTrue(all(e["unidad_orden"] == 1 and e["respuestas"] == ["a"] for e in ejercicios))
        self.assertEqual(P.numeros_faltantes(res), list(range(2, 19)))

    def test_lo_que_no_valida_tras_reintento_queda_diferido(self):
        caps = P.dividir_capitulos(P.leer_lineas(self.path))
        malo = {"preguntas": [{"numero": "1", "pregunta": "¿Inventada?", "respuesta": "a",
                               "variantes": [{"a": "SI", "b": "NO"}]}]}
        res = P.estructurar_capitulo(1, caps[1], client=FakeLLM([malo, malo]))
        self.assertEqual(len(res.diferidas), 1)
        e = P.a_ejercicios(res, prefijo_grupo="x", fuente="f")[0]
        self.assertTrue(e["diferir"])
        self.assertIn("enunciado", e["motivo"])
