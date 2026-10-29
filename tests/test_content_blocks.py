"""Componentes de contenido (recuadros y tablas): contrato, validación, texto plano y Brújula."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from django.test import SimpleTestCase

from content_pipeline.processors.content_blocks import (
    parse_blocks, parse_inline, strip_components, validate_components,
)

REVIEW = Path(__file__).resolve().parent.parent / "content_pipeline" / "review"
FIXTURES = REVIEW / "fixtures_contenido.json"
JS = REVIEW / "contenido_md.js"
BRUJULA = REVIEW / "brujula.html"


def _leccion(desarrollo: str, extra: str = "") -> str:
    return (f"# T\n\n## Objetivo\nx\n\n## Desarrollo\n{desarrollo}\n\n## Puntos clave\n- y\n{extra}\n"
            "## Resumen\nz\n\n## Fuente\nLibro.")


class ContratoTests(SimpleTestCase):
    """La implementación de referencia reproduce los casos compartidos con los clientes."""

    def setUp(self):
        self.fx = json.loads(FIXTURES.read_text(encoding="utf-8"))

    def test_fixtures_python(self):
        for caso in self.fx["bloques"]:
            with self.subTest(caso["nombre"]):
                self.assertEqual(parse_blocks(caso["md"]), caso["esperado"])
        for caso in self.fx["linea"]:
            with self.subTest(caso["nombre"]):
                self.assertEqual(parse_inline(caso["texto"]), caso["esperado"])
        for caso in self.fx["texto_plano"]:
            with self.subTest(caso["nombre"]):
                self.assertEqual(strip_components(caso["md"]), caso["esperado"])

    def test_fixtures_js(self):
        if not shutil.which("node"):
            self.skipTest("node no está instalado")
        script = (
            "const M=require(process.argv[1]),d=require(process.argv[2]),bad=[];"
            "for(const c of d.bloques)if(JSON.stringify(M.parseBlocks(c.md))!==JSON.stringify(c.esperado))bad.push(c.nombre);"
            "for(const c of d.linea)if(JSON.stringify(M.parseInline(c.texto))!==JSON.stringify(c.esperado))bad.push(c.nombre);"
            "console.log(JSON.stringify(bad));"
        )
        out = subprocess.run(["node", "-e", script, str(JS), str(FIXTURES)], capture_output=True, text=True,
                             encoding="utf-8", check=True)
        self.assertEqual(json.loads(out.stdout), [])

    def test_brujula_embebe_el_parser_identico(self):
        html = BRUJULA.read_text(encoding="utf-8")
        m = re.search(r"/\* contenido_md:inicio[^\n]*\*/\n(.*?)\n/\* contenido_md:fin \*/", html, flags=re.S)
        self.assertIsNotNone(m, "falta el bloque contenido_md en brujula.html")
        self.assertEqual(m.group(1), JS.read_text(encoding="utf-8").rstrip("\n"))


class ParseTests(SimpleTestCase):
    def test_recuadro_con_titulo_y_cuerpo(self):
        b = parse_blocks("> [!norma] Ley Emilia\n> Cárcel efectiva.\n> - Reincidencia.")[0]
        self.assertEqual((b["t"], b["tipo"], b["titulo"]), ("callout", "NORMA", "Ley Emilia"))
        self.assertEqual([x["t"] for x in b["blocks"]], ["paragraph", "list"])

    def test_cita_sin_etiqueta_y_tabla_con_titulo(self):
        bl = parse_blocks("> cita\n\nTabla: Datos\n| A | B |\n|:--|--:|\n| 1 | 2 |")
        self.assertEqual(bl[0]["t"], "quote")
        self.assertEqual(bl[1], {"t": "table", "titulo": "Datos", "align": ["left", "right"],
                                 "header": ["A", "B"], "rows": [["1", "2"]]})

    def test_tabla_rompe_parrafo_previo(self):
        bl = parse_blocks("Texto previo\n| A | B |\n|---|---|\n| 1 | 2 |")
        self.assertEqual([b["t"] for b in bl], ["paragraph", "table"])


class ValidacionTests(SimpleTestCase):
    def test_lecciones_sin_componentes_no_reportan(self):
        self.assertEqual(validate_components(_leccion("Texto **normal**.\n\n> una cita")), ([], []))

    def test_errores(self):
        md = _leccion("> [!RARO]\n> x\n\n> [!DATO]\n\n"
                      "| A | B |\n|---|---|\n| 1 |\n\n"
                      "| 1 | 2 | 3 | 4 | 5 | 6 | 7 |\n|---|---|---|---|---|---|---|\n| a | b | c | d | e | f | g |")
        errores, _ = validate_components(md)
        texto = " ".join(errores)
        self.assertIn("tipo desconocido", texto)
        self.assertIn("sin contenido", texto)
        self.assertIn("número de columnas distinto", texto)
        self.assertIn("7 columnas", texto)

    def test_anidado_y_tabla_dentro_de_recuadro(self):
        errores, _ = validate_components(_leccion("> [!NORMA]\n> [!DATO]\n> x"))
        self.assertTrue(any("anidar" in e for e in errores))
        errores, _ = validate_components(_leccion("> [!NORMA]\n> | A |\n> |---|\n> | 1 |"))
        self.assertTrue(any("tabla no puede ir dentro" in e for e in errores))

    def test_advertencias(self):
        md = _leccion("\n\n".join(f"> [!DATO]\n> d{k}" for k in range(5)),
                      extra="\n> [!CONSEJO]\n> fuera de lugar\n")
        errores, adv = validate_components(md)
        self.assertEqual(errores, [])
        self.assertTrue(any("6 recuadros" in a for a in adv))
        self.assertTrue(any("fuera de las secciones de lectura" in a and "puntos clave" in a for a in adv))

    def test_integrado_en_validate_generated_course(self):
        from content_pipeline.processors.validators import validate_generated_course
        lesson = {"unidad_orden": 1, "posicion": 1, "nombre": "S", "tipo": "texto",
                  "contenido": _leccion("> [!RARO]\n> x"),
                  "fuentes": [{"fuente_nombre": "L", "pagina_inicio": 1, "pagina_fin": 1}]}
        v = validate_generated_course({"unidades": [{"orden": 1, "nombre": "U"}]}, [lesson])
        self.assertTrue(any("tipo desconocido" in e and "S" in e for e in v["errores"]))


class TextoPlanoTests(SimpleTestCase):
    MD = ("## Desarrollo\n\n> [!NORMA] Ley Emilia\n> Cárcel efectiva de **al menos un año**.\n\n"
          "Tabla: Suspensión\n| Infracción | Suspensión |\n|---|---|\n| Alcohol | 3 meses |\n\n{{figura:F-a}}")

    def test_strip_markers_quita_componentes(self):
        from content_pipeline.processors.figure_markers import strip_markers
        out = strip_markers(self.MD)
        self.assertNotIn("[!", out)
        self.assertNotIn("|", out)
        self.assertNotIn("{{", out)
        self.assertIn("Ley Emilia\nCárcel efectiva", out)
        self.assertIn("Infracción: Alcohol; Suspensión: 3 meses.", out)

    def test_guion_de_audio_sin_sintaxis(self):
        from content_pipeline.media.narration import strip_markdown
        out = strip_markdown(self.MD)
        self.assertNotIn("[!NORMA]", out)
        self.assertNotIn("|", out)
        self.assertIn("al menos un año", out)

    def test_cifras_de_tablas_se_siguen_auditando(self):
        from content_pipeline.processors.faithfulness import unsupported_figures
        from content_pipeline.processors.figure_markers import strip_markers
        flagged = unsupported_figures(strip_markers(self.MD), "La suspensión por alcohol es de 2 meses.")
        self.assertTrue(flagged)


class PromptTests(SimpleTestCase):
    def test_prompt_del_redactor_documenta_los_componentes(self):
        from content_pipeline.processors.content_blocks import TIPOS_RECUADRO
        from content_pipeline.processors.llm_lesson_writer import FAITHFUL_LESSON_SYSTEM
        for tipo in TIPOS_RECUADRO:
            self.assertIn(tipo, FAITHFUL_LESSON_SYSTEM)
        self.assertIn("> [!DEBES_SABER]", FAITHFUL_LESSON_SYSTEM)
        self.assertIn("Tabla:", FAITHFUL_LESSON_SYSTEM)
