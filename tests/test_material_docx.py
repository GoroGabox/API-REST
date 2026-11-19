"""«Material de estudio» de cursos profesionales → curso + banco (content_pipeline.processors.material_docx)."""
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from content_pipeline.processors import material_docx as M
from content_pipeline.processors.content_blocks import parse_blocks, validate_components
from tests.test_guia_docx import IMG, _p, _tabla, guia


def _fila_senal(texto):
    return f"<w:tr><w:tc>{IMG}</w:tc><w:tc>{_p(texto)}</w:tc></w:tr>"


MATERIAL = "".join([
    _p("Contenido", "Heading1"), _p("Índice que se omite."),
    _p("Introducción al curso", "Heading1"),
    _p("Bienvenido al curso de conductor profesional Clase A4."),
    _p("Requisitos para obtener la licencia", "Heading2"),
    _p("Tener como mínimo 20 años de edad.", "ListParagraph"),
    _p("Vigencia de la licencia", "Heading3"),
    _p("Sin embargo, el titular de una licencia profesional debe acreditar cada 4 años su idoneidad."),
    _p("Cómo está organizado el curso", "Heading2"), _p("El curso suma 118 horas de test."),
    _p("MÓDULO 1"), _p("Reglamentación aplicada al transporte de carga", "Heading1"),
    _tabla([["Horas de este módulo\n8 horas de lectura y estudio."]]),
    _p("En este primer módulo vamos a estudiar las reglas legales."),
    _p("Objetivos del módulo", "Heading3"), _p("Distinguir las clases de licencia.", "ListParagraph"),
    _p("Unidad 1 · Las licencias de conductor", "Heading2"),
    _p("Tiempo estimado de estudio: 1 h 15 min"),
    _p("Licencias de carga", "Heading3"),
    _p("La A4 es para camiones simples de más de 3.500 kg.", "ListParagraph"),
    _p("1.\tNo detenerse ante la luz roja."),
    _tabla([["Recuerde\nLa A4 es para camiones simples.\nLos articulados requieren A5."]]),
    _tabla([["Clase", "Vehículo"], ["A4", "Camión simple"]]),
    "<w:tbl>" + _fila_senal("PARE. Ordena detenerse completamente.") + "</w:tbl>",
    _tabla([["Preguntas para repasar\n¿Qué diferencia hay entre A4 y A5?"]]),
    _p("Ideas clave del módulo 1", "Heading2"), _p("Tiempo estimado de estudio: 30 min"),
    _tabla([["Recuerde\nLa A4 permite camiones simples."]]),
])


def _q(n, enunciado, opciones, correcta, conaset=False):
    xs = [_p(f"{n}. {enunciado}" + ("  · Cuestionario CONASET" if conaset else ""))]
    for i, o in enumerate(opciones):
        letra = "abcd"[i]
        xs.append(_p(("ⓐⓑⓒⓓ"[i] + " " + o) if i == correcta else f"{letra}) {o}"))
    return "".join(xs)


PRACTICA = "".join([
    _p("Módulo 1 · Reglamentación aplicada al transporte de carga", "Title"),
    _p("50 test de 20 preguntas · Clase A4 · Banco de 3 preguntas: 2 de los módulos y 1 oficial", "Heading2"),
    _p("Módulo 1 · Test 1 de 50", "Heading1"),
    _q(1, "¿Peso bruto total máximo?", ["45 toneladas", "30 toneladas"], 0),
    _q(2, "¿Quién cubre el SOAP?", ["Solo al conductor", "A todos los afectados"], 1, conaset=True),
    _p("Módulo 1 · Test 2 de 50", "Heading1"),
    # La misma pregunta con las alternativas en otro orden y una nueva.
    _q(1, "¿Peso bruto total máximo?", ["30 toneladas", "45 toneladas"], 1),
    _q(2, "¿Qué es el tacógrafo?", ["Un registrador de velocidad", "Un freno"], 0),
    _p("Pautas de respuestas", "Heading1"),
    _p("1. a"),
])


class MaterialDocxTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory()
        mat = Path(cls.tmp.name) / "Curso_A4_Material_de_estudio_7_modulos.docx"
        mat.write_bytes(guia(MATERIAL))
        prac = Path(cls.tmp.name) / "Curso_A4_Practica_CON_RESPUESTAS.docx"
        prac.write_bytes(guia(PRACTICA))
        cls.mat = M.parsear_material(mat, "A4")
        cls.ejercicios, cls.declarados, _ = M.parsear_banco(prac, codigo="A4", fuente_guia="Práctica A4")
        cls.curso, cls.figuras = M.armar_curso(
            cls.mat, nombre="Curso A4", codigo="A4", precio=6990, profesional=True, guia="Material A4",
            ejercicios=cls.ejercicios, paginas={(1, 1): (205, 208, 0.86)})

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        super().tearDownClass()

    def test_presentacion_desde_la_introduccion(self):
        md = M.presentacion_md(self.mat, codigo="A4", guia="Material A4")
        self.assertIn("### Requisitos para obtener la licencia", md)
        self.assertNotIn("118 horas", md)                      # «Cómo está organizado» se omite
        self.assertNotIn("Índice", md)
        self.assertLess(md.index("{{declaracion}}"), md.index("{{salud_faq}}"))
        self.assertIn("renovacion: 'La licencia tiene duración indefinida, pero el titular", md)
        self.assertIn("tratamiento: usted\n", md)

    def test_quiz_remite_a_sus_lecciones(self):
        quiz = next(l for l in self.curso["lessons"] if l["tipo"] == "quiz")
        self.assertEqual([f["fuente_nombre"] for f in quiz["fuentes"]], ["Lección 1 · Las licencias de conductor"])
        self.assertNotIn("Libro", " ".join(f["fuente_nombre"] for f in quiz["fuentes"]))
        self.assertIn("Repasa: Las licencias de conductor.", quiz["descripcion"])

    def test_modulo_y_lecciones(self):
        mod = self.mat.modulos[0]
        self.assertEqual((mod.numero, mod.titulo), (1, "Reglamentación aplicada al transporte de carga"))
        self.assertEqual(mod.horas, "8 horas de lectura y estudio.")
        nombres = [(l["posicion"], l["tipo"], l["nombre"], l["anexo"]) for l in self.curso["lessons"]]
        self.assertEqual(nombres, [(1, "texto", "Las licencias de conductor", False),
                                   (2, "texto", "Ideas clave del módulo 1", True),
                                   (3, "quiz", "Evaluación del módulo 1", False)])
        l = self.curso["lessons"][0]
        self.assertEqual(l["duracion_min"], 75)
        c = l["contenido"]
        self.assertLess(c.index("## Introducción"), c.index("## Desarrollo"))
        intro = c[c.index("## Introducción"):c.index("## Desarrollo")]
        self.assertIn("**Objetivos del módulo**\n\n- Distinguir las clases de licencia.", intro)
        self.assertNotIn("Horas de este módulo", c)
        self.assertIn("### Licencias de carga", c)
        self.assertIn("1. No detenerse ante la luz roja.", c)
        self.assertIn("## Actividad breve\n\n- ¿Qué diferencia hay entre A4 y A5?", c)
        self.assertIn("páginas 205-208", c)
        self.assertEqual((l["fuentes"][0]["pagina_inicio"], l["fuentes"][0]["pagina_fin"]), (205, 208))

    def test_componentes_y_figura_de_senal(self):
        c = self.curso["lessons"][0]["contenido"]
        bloques = parse_blocks(c)
        rec = [b for b in bloques if b["t"] == "callout"][0]
        self.assertEqual((rec["tipo"], rec["titulo"]), ("DEBES_SABER", "Recuerde"))
        self.assertEqual(rec["blocks"][0]["t"], "list")       # varios párrafos → viñetas
        self.assertEqual([b["header"] for b in bloques if b["t"] == "table"], [["Clase", "Vehículo"]])
        rec_fig = self.curso["lessons"][0]["recursos"][0]
        self.assertEqual(rec_fig["meta"]["pie"], "Pare")
        self.assertIn(f"{{{{figura:{rec_fig['clave']}}}}}\n\n**PARE.** Ordena detenerse completamente.", c)
        self.assertEqual(validate_components(c)[0], [])

    def test_banco_unico_correcta_y_conaset(self):
        self.assertEqual(self.declarados, {1: 3})
        self.assertEqual(len(self.ejercicios), 3)              # la repetida con otro orden cuenta una vez
        peso = next(e for e in self.ejercicios if e["pregunta"].startswith("¿Peso"))
        self.assertEqual(peso["opciones"][peso["respuestas"][0]], "45 toneladas")
        self.assertEqual(peso["veces_en_tests"], 2)
        soap = next(e for e in self.ejercicios if "SOAP" in e["pregunta"])
        self.assertEqual((soap["pregunta"], soap["fuente"]), ("¿Quién cubre el SOAP?", "Cuestionario oficial CONASET"))
        self.assertTrue(all(e["unidad_orden"] == 1 and not e["diferir"] for e in self.ejercicios))

    def test_correcta_distinta_entre_tests_se_difiere(self):
        practica = "".join([
            _p("Módulo 1 · Test 1 de 50", "Heading1"), _q(1, "¿X?", ["Uno", "Dos"], 0),
            _p("Módulo 1 · Test 2 de 50", "Heading1"), _q(1, "¿X?", ["Dos", "Uno"], 0),
        ])
        p = Path(self.tmp.name) / "conflicto.docx"
        p.write_bytes(guia(practica))
        ej, _, _ = M.parsear_banco(p, codigo="A4", fuente_guia="x")
        self.assertTrue(ej[0]["diferir"])
        self.assertIn("cambia entre tests", ej[0]["motivo"])

    def test_utilidades(self):
        self.assertEqual(M.minutos("1 h 15 min"), 75)
        self.assertEqual(M.nombre_senal("CEDA EL PASO. Los conductores…"), ("Ceda el paso", "Los conductores…"))
        self.assertEqual(M.nombre_senal("Texto normal."), ("", "Texto normal."))
