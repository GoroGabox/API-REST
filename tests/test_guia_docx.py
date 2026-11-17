"""Conversor de guías del alumno (.docx) → curso + ejercicios (content_pipeline.processors.guia_docx)."""
import io
import json
import tempfile
import zipfile
from pathlib import Path

from django.test import SimpleTestCase

from content_pipeline.processors import guia_docx as G
from content_pipeline.processors.content_blocks import parse_blocks
from content_pipeline.processors.validators import validate_generated_course

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


def _p(texto, estilo="", negrita_inicio=""):
    ppr = f'<w:pPr><w:pStyle w:val="{estilo}"/></w:pPr>' if estilo else ""
    runs = ""
    if negrita_inicio:
        runs += f'<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">{negrita_inicio}</w:t></w:r>'
    runs += f'<w:r><w:t xml:space="preserve">{texto}</w:t></w:r>'
    return f"<w:p>{ppr}{runs}</w:p>"


def _tabla(filas):
    trs = "".join("<w:tr>" + "".join(f"<w:tc>{''.join(_p(x) for x in celda.split(chr(10)))}</w:tc>"
                                      for celda in fila) + "</w:tr>" for fila in filas)
    return f"<w:tbl>{trs}</w:tbl>"


IMG = ('<w:p><w:r><w:drawing><a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
       '<a:blip xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:embed="rId9"/>'
       '</a:graphic></w:drawing></w:r></w:p>')


def guia(cuerpo_xml: str) -> bytes:
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId9" Target="media/image1.png"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body>{cuerpo_xml}</w:body></w:document>')
        z.writestr("word/_rels/document.xml.rels", rels)
        z.writestr("word/media/image1.png", PNG)
    return buf.getvalue()


CUERPO = "".join([
    _p("Módulo 1Reglamentación de carga", "Title"),
    _p("Presentación", "Heading2"), _p("Este módulo reúne las normas del conductor de carga."),
    _p("Organización del curso online", "Heading1"), _p("Texto de organización que se omite."),
    _p("Unidad 1 · Licencias y documentos", "Heading1"),
    _p("Capítulos: 1"), _p("Libro CONASET: páginas 40 y 205 a 210"), _p("Tiempo estimado de estudio: 15 minutos"),
    _p("Objetivos de la unidad", "Heading2"), _p("Explicar qué habilita la licencia A4.", "ListBullet"),
    _p("1 Licencias", "Heading1"), _p("1.1 Qué habilita", "Heading2"),
    _p("La licencia A4 habilita vehículos simples de más de 3.500 kg."),
    _tabla([["Clase", "Vehículos"], ["A4", "Simples de carga"]]),
    _tabla([["Criterio central\nNo se sale sin documentos al día."]]),
    IMG, _p("Esquema didáctico Al Volante · Camión simple de carga. Detalle adicional."),
    _p("Ideas clave de la unidad 1", "Heading2"), _p("A4: más de 3.500 kg.", "ListBullet"),
    _p("Cuestionario de la unidad 1", "Heading2"), _p("Marque la alternativa correcta."),
    _p("1. ¿Desde qué peso se requiere A4?"), _p("a) Más de 3.500 kg"), _p("b) Más de 1.000 kg"),
    _p("2. ¿Qué licencia exige un tractocamión?"), _p("a) A4"), _p("b) A5"),
    _p("Actividad de aplicación", "Heading1"), _p("Caso: prepare un viaje con documentos al día."),
    _p("Respuestas de los cuestionarios", "Heading1"),
    _p("Unidad 1 · Licencias y documentos", "Heading2"),
    _p("1. Alternativa a) Más de 3.500 kg"), _p("Libro, página 205."),
    _p("2. Alternativa a) A5"), _p("Un tractocamión es articulado."),
    _p("Fuentes", "Heading1"), _p("CONASET, libro profesional.", "ListBullet"),
])


class GuiaDocxTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "A4_1_Reglamentacion_ALUMNO.docx"
        self.path.write_bytes(guia(CUERPO))
        self.mod = G.parsear_modulo(self.path, 1, "A4")
        self.curso, self.ejercicios, self.figuras = G.armar_curso(
            [self.mod], nombre="Curso A4", codigo="A4", precio=6990, profesional=True, guia="Guía A4")
        self.leccion = self.curso["lessons"][0]

    def tearDown(self):
        self.tmp.cleanup()

    def test_modulo_y_leccion_por_unidad(self):
        self.assertEqual(self.mod.titulo, "Reglamentación de carga")
        self.assertEqual([l.nombre for l in self.mod.lecciones], ["Licencias y documentos"])
        self.assertEqual(self.leccion["duracion_min"], 15)
        c = self.leccion["contenido"]
        self.assertIn("## Objetivo\n\n- Explicar qué habilita la licencia A4.", c)
        self.assertIn("### 1 Licencias", c)
        self.assertIn("**1.1 Qué habilita**", c)
        self.assertIn("## Puntos clave\n\n- A4: más de 3.500 kg.", c)
        self.assertIn("## Aplicación práctica", c)          # actividad → última lección
        self.assertNotIn("organización", c.lower())
        self.assertNotIn("Cuestionario", c)                  # el cuestionario va al banco

    def test_paginas_por_tramo(self):
        self.assertEqual([(f["pagina_inicio"], f["pagina_fin"]) for f in self.leccion["fuentes"]],
                         [(40, 40), (205, 210)])
        self.assertEqual(G.tramos_paginas("38, 43 a 45 y 99–101 (visión general)"),
                         [(38, 38), (43, 45), (99, 101)])

    def test_recuadro_tabla_y_figura(self):
        bloques = parse_blocks(self.leccion["contenido"])
        recuadros = [b for b in bloques if b["t"] == "callout"]
        self.assertEqual((recuadros[0]["tipo"], recuadros[0]["titulo"]), ("DEBES_SABER", "Criterio central"))
        tablas = [b for b in bloques if b["t"] == "table"]
        self.assertEqual(tablas[0]["header"], ["Clase", "Vehículos"])
        self.assertEqual(len(self.figuras), 1)
        rec = self.leccion["recursos"][0]
        self.assertIn(f"{{{{figura:{rec['clave']}}}}}", self.leccion["contenido"])
        self.assertEqual(rec["meta"]["pie"], "Camión simple de carga.")

    def test_cuestionario_cruzado_con_respuestas(self):
        e1, e2 = self.ejercicios
        self.assertEqual((e1["respuestas"], e1["explicacion"], e1["diferir"]), (["a"], "Libro, página 205.", False))
        # La respuesta dice «a) A5» pero la alternativa a) es «A4»: no calza → diferir.
        self.assertTrue(e2["diferir"])
        self.assertIn("no coincide", e2["motivo"])
        self.assertEqual(e1["grupo"], "a4-m1-u1-1")

    def test_anexo_y_quiz_del_modulo(self):
        anexo = next(l for l in self.curso["lessons"] if l.get("anexo"))
        self.assertIn("### Fuentes", anexo["contenido"])
        quiz = next(l for l in self.curso["lessons"] if l["tipo"] == "quiz")
        self.assertEqual(len(quiz["contenido"]["questions"]), 1)    # solo la válida
        self.assertEqual(quiz["contenido"]["questions"][0]["correct_index"], 0)

    def test_completar_secciones_solo_las_faltantes_y_audita_cifras(self):
        class FakeLLM:
            def __init__(self):
                self.n = 0

            def complete(self, **kw):
                self.n += 1
                # 1ª respuesta inventa una cifra; la 2ª es fiel.
                return "Se exigen 9.999 kg." if self.n == 1 else "La licencia A4 habilita vehículos de más de 3.500 kg."

        llm = FakeLLM()
        reg = G.completar_secciones(self.curso, llm, log=lambda *_: None)
        self.assertEqual([r["seccion"] for r in reg], ["Resumen"])   # Objetivo y Puntos clave ya venían
        self.assertEqual((reg[0]["modo"], llm.n), ("ia", 2))
        c = self.leccion["contenido"]
        self.assertLess(c.index("## Puntos clave"), c.index("## Resumen"))
        self.assertLess(c.index("## Resumen"), c.index("## Fuente"))
        val = validate_generated_course(self.curso["manifest"], self.curso["lessons"])
        self.assertEqual(val["errores"], [])
        json.dumps(self.curso)  # serializable
