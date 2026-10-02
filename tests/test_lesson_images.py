"""Figuras del libro: extracción (PDF/Word), mapeo a lecciones, IA de visión, storage e import."""
from __future__ import annotations

import io
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

import fitz
from django.test import SimpleTestCase, TestCase

from content_pipeline.media.images import (
    Figura, describe_figures_llm, extract_docx_figures, extract_pdf_figures, map_figures_to_plan,
)

BODY_A = "Antes de partir revisa siempre el nivel de combustible y la presión de los neumáticos del vehículo."
BODY_B = "El tablero informa el estado del motor mediante testigos de color rojo, ámbar y verde."
BODY_C = "Las señales reglamentarias indican prohibiciones y obligaciones que debes cumplir siempre en la vía."


def _png(color: tuple[int, int, int], size: int = 60) -> bytes:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, size, size), 0)
    pix.set_rect(pix.irect, color)
    return pix.tobytes("png")


def _pdf(tmp: Path) -> Path:
    """6 páginas: logo repetido en 5, figura entre párrafos, dos imágenes contiguas,
    divisoria a página completa sin texto, imagen diminuta."""
    doc = fitz.open()
    logo = _png((0, 0, 255), 30)
    for i in range(6):
        page = doc.new_page(width=600, height=800)
        if i < 5:
            page.insert_image(fitz.Rect(20, 20, 80, 80), stream=logo)           # logo repetido
        if i == 0:
            page.insert_textbox(fitz.Rect(50, 120, 550, 200), BODY_A, fontsize=11)
            page.insert_image(fitz.Rect(100, 220, 400, 420), stream=_png((255, 0, 0)))
            page.insert_textbox(fitz.Rect(100, 425, 550, 445), "Indicador de combustible", fontsize=9)
            page.insert_textbox(fitz.Rect(50, 480, 550, 560), BODY_B, fontsize=11)
        if i == 1:
            page.insert_textbox(fitz.Rect(50, 100, 550, 180), BODY_C, fontsize=11)
            page.insert_image(fitz.Rect(100, 200, 250, 350), stream=_png((0, 200, 0)))
            page.insert_image(fitz.Rect(254, 200, 400, 350), stream=_png((200, 200, 0)))  # contigua
            page.insert_image(fitz.Rect(450, 600, 455, 605), stream=_png((9, 9, 9)))      # diminuta
        if i == 2:
            page.insert_image(page.rect, stream=_png((120, 120, 120)))                  # divisoria
        if i >= 3:
            page.insert_textbox(fitz.Rect(50, 100, 550, 300), BODY_A + " " + BODY_B, fontsize=11)
    path = tmp / "libro.pdf"
    doc.save(str(path))
    return path


def _docx(tmp: Path) -> Path:
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
    A = "http://schemas.openxmlformats.org/drawingml/2006/main"
    R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

    def p(text):
        return f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>'

    def img(rid, cx=1905000, cy=1905000):
        return (f'<w:p><w:r><w:drawing><wp:inline><wp:extent cx="{cx}" cy="{cy}"/><a:graphic><a:graphicData>'
                f'<a:blip r:embed="{rid}"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>')
    body = p(BODY_A) + img("rId1") + p("Figura 1: Tablero del vehículo") + p(BODY_B) + img("rId2") + p(BODY_C)
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}" xmlns:wp="{WP}" '
           f'xmlns:a="{A}" xmlns:r="{R}"><w:body>{body}</w:body></w:document>')
    rels = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="media/image1.png"/><Relationship Id="rId2" Target="media/image2.emf"/>'
            '</Relationships>')
    path = tmp / "capitulo.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)
        z.writestr("word/_rels/document.xml.rels", rels)
        z.writestr("word/media/image1.png", _png((10, 20, 30)))
        z.writestr("word/media/image2.emf", b"emf")
    return path


def _plan(paginas=((1, 1), (2, 2))):
    return {"unidades": [{"orden": 1, "nombre": "U1", "lecciones": [
        {"id": "L-a", "nombre": "Revisión previa", "paginas": list(paginas[0]), "texto": BODY_A},
        {"id": "L-b", "nombre": "Tablero y señales", "paginas": list(paginas[1]), "texto": BODY_B + "\n\n" + BODY_C},
    ]}]}


class _TmpMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class PdfExtractionTests(_TmpMixin, SimpleTestCase):
    def test_filters_merges_and_anchors(self):
        figs, descartes = extract_pdf_figures(_pdf(self.tmp))
        self.assertEqual([f.pagina for f in figs], [1, 2])                # logo/divisoria/diminuta fuera
        self.assertGreaterEqual(descartes["repetida"], 5)
        self.assertEqual(descartes["divisoria"], 1)
        self.assertGreaterEqual(descartes["pequena"], 1)
        f1, f2 = figs
        self.assertIn("combustible", f1.ancla_antes)
        self.assertIn("tablero", f1.ancla_despues.lower())
        self.assertEqual(f1.pie_libro, "Indicador de combustible")
        self.assertGreater(f2.ancho, f2.alto)                             # 2 imágenes contiguas = 1 figura
        self.assertTrue(f1.data.startswith(b"\x89PNG"))

    def test_maps_by_anchor_text(self):
        figs, _ = extract_pdf_figures(_pdf(self.tmp))
        asignadas, sin = map_figures_to_plan(figs, _plan())
        self.assertEqual(sin, [])
        self.assertEqual([m for _f, m in asignadas["L-a"]], ["texto"])
        self.assertEqual(len(asignadas["L-b"]), 1)


class MappingFallbackTests(SimpleTestCase):
    def _fig(self, **kw):
        return Figura(data=b"x" + kw.pop("salt", b""), ext="png", ancho=10, alto=10, orden=1, origen="pdf", **kw)

    def test_page_fallback_and_unmapped(self):
        en_pag = self._fig(pagina=2, ancla_antes="texto que no está en ninguna lección del curso")
        sin_pag = self._fig(pagina=9, salt=b"2", ancla_antes="otro texto ajeno al libro y a sus lecciones")
        asignadas, sin = map_figures_to_plan([en_pag, sin_pag], _plan())
        self.assertEqual(asignadas["L-b"][0][1], "pagina")
        self.assertEqual(sin, [sin_pag])

    def test_uninformative_pages_do_not_map(self):
        fig = self._fig(pagina=1, ancla_antes="texto que no está en ninguna lección del curso")
        _asig, sin = map_figures_to_plan([fig], _plan(paginas=((1, 1), (1, 1))))
        self.assertEqual(sin, [fig])                                     # plan desde Word: [1,1] en todo

    def test_folder_fallback_to_unit(self):
        fig = Figura(data=b"y", ext="png", ancho=10, alto=10, orden=1, origen="docx", archivo=0,
                     ancla_antes="sin coincidencias en el texto de la unidad")
        asignadas, _ = map_figures_to_plan([fig], _plan(), por_archivo=True)
        self.assertEqual(asignadas["L-a"][0][1], "unidad")


class DocxExtractionTests(_TmpMixin, SimpleTestCase):
    def test_images_between_paragraphs(self):
        figs, descartes = extract_docx_figures(_docx(self.tmp))
        self.assertEqual(len(figs), 1)
        self.assertEqual(descartes["formato"], 1)                         # emf no es web
        f = figs[0]
        self.assertEqual((f.origen, f.ancho, f.alto), ("docx", 200, 200))
        self.assertEqual(f.ancla_antes, BODY_A)
        self.assertEqual(f.pie_libro, "Figura 1: Tablero del vehículo")
        asignadas, _ = map_figures_to_plan(figs, _plan(paginas=((1, 1), (1, 1))))
        self.assertIn("L-a", asignadas)


class _VisionLLM:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def complete(self, **kw):
        self.calls.append(kw)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return json.dumps(a)


class DescribeTests(SimpleTestCase):
    def test_vision_blocks_and_best_effort(self):
        f1 = Figura(data=b"a", ext="png", ancho=1, alto=1, orden=1, origen="pdf", ancla_antes="contexto")
        f2 = Figura(data=b"b", ext="jpg", ancho=1, alto=1, orden=2, origen="pdf")
        f3 = Figura(data=b"c", ext="png", ancho=1, alto=1, orden=3, origen="pdf")
        llm = _VisionLLM({"relevante": True, "pie": "Tablero del auto", "alt": "Tablero"},
                         {"relevante": False, "motivo": "decorativa"}, RuntimeError("caída"))
        out = describe_figures_llm([(f1, "Tablero"), (f2, "X"), (f3, "Y")], client=llm, model="m")
        self.assertEqual(out[f1.hash]["pie"], "Tablero del auto")
        self.assertFalse(out[f2.hash]["relevante"])
        self.assertNotIn(f3.hash, out)                                   # falla → se conserva sin IA
        bloques = llm.calls[1]["user"]
        self.assertEqual(bloques[0]["source"]["media_type"], "image/jpeg")
        self.assertEqual(bloques[1]["type"], "text")


class StorageTests(_TmpMixin, SimpleTestCase):
    def test_local_images_storage_with_subfolder(self):
        from content_pipeline.media.storage import get_storage
        st = get_storage("local", kind="images", media_dir=self.tmp, base_url="http://x/media/course_images/")
        self.assertFalse(st.exists("B/abc.png"))
        self.assertEqual(st.save("B/abc.png", b"png"), "http://x/media/course_images/B/abc.png")
        self.assertTrue(st.exists("B/abc.png"))

    def test_s3_prefix_and_content_type_by_kind(self):
        from content_pipeline.media.storage import S3Storage

        class _Client:
            def put_object(self, **kw):
                self.kw = kw
        c = _Client()
        st = S3Storage(client=c, bucket="b", public_base_url="https://cdn/", kind="images")
        self.assertEqual(st.save("B/x.png", b"1"), "https://cdn/course_images/B/x.png")
        self.assertEqual(c.kw["ContentType"], "image/png")


class CommandAndImportTests(_TmpMixin, TestCase):
    def _course(self):
        lessons = [
            {"unidad_orden": 1, "nombre": "Revisión previa", "posicion": 1, "tipo": "texto", "categoria": "General",
             "contenido": "c", "plan_id": "L-a", "fuentes": []},
            {"unidad_orden": 1, "nombre": "Tablero y señales", "posicion": 2, "tipo": "texto",
             "categoria": "General", "contenido": "c", "plan_id": "L-b", "fuentes": []},
        ]
        manifest = {"curso": {"codigo": "IMG", "nombre": "Curso Img", "descripcion": "d"},
                    "unidades": [{"orden": 1, "nombre": "U1", "categoria": "General"}]}
        return {"manifest": manifest, "lessons": lessons}

    def test_command_uploads_maps_and_import_creates_images(self):
        from django.core.management import call_command
        from schools.models import Leccion, LeccionImagen
        from schools.serializers import LeccionDetalleSerializer
        from content_pipeline.exporters.django_importer import import_generated_course

        plan_p, course_p, out_p = self.tmp / "plan.json", self.tmp / "c.json", self.tmp / "c_img.json"
        plan_p.write_text(json.dumps(_plan()), encoding="utf-8")
        course_p.write_text(json.dumps(self._course()), encoding="utf-8")
        call_command("extract_images", plan=str(plan_p), course=str(course_p), contenido=str(_pdf(self.tmp)),
                     out=str(out_p), storage="local", media_dir=str(self.tmp / "media"),
                     base_url="http://x/img/", stdout=io.StringIO(), stderr=io.StringIO())
        data = json.loads(out_p.read_text(encoding="utf-8"))
        img = data["lessons"][0]["imagenes"][0]
        self.assertTrue(img["url"].startswith("http://x/img/IMG/"))
        self.assertEqual((img["pie"], img["mapeo"], img["pagina"]), ("Indicador de combustible", "texto", 1))
        self.assertEqual(data["imagenes_meta"]["asignadas"], 2)

        import_generated_course(data["manifest"], data["lessons"])
        lec = Leccion.objects.get(nombre="Revisión previa")
        self.assertEqual(lec.imagenes.count(), 1)
        self.assertEqual(LeccionDetalleSerializer(lec).data["imagenes"][0]["pie"], "Indicador de combustible")
        # Re-import reemplaza (no duplica); una lección sin la clave conserva sus figuras.
        import_generated_course(data["manifest"], data["lessons"])
        self.assertEqual(LeccionImagen.objects.filter(leccion=lec).count(), 1)
        data["lessons"][0].pop("imagenes")
        import_generated_course(data["manifest"], data["lessons"])
        self.assertEqual(LeccionImagen.objects.filter(leccion=lec).count(), 1)

    def test_sin_subir_keeps_local_files_and_import_skips_them(self):
        from django.core.management import call_command
        plan_p, course_p, out_p = self.tmp / "plan.json", self.tmp / "c.json", self.tmp / "out" / "c_img.json"
        plan_p.write_text(json.dumps(_plan()), encoding="utf-8")
        course_p.write_text(json.dumps(self._course()), encoding="utf-8")
        call_command("extract_images", plan=str(plan_p), course=str(course_p), contenido=str(_pdf(self.tmp)),
                     out=str(out_p), sin_subir=True, stdout=io.StringIO(), stderr=io.StringIO())
        img = json.loads(out_p.read_text(encoding="utf-8"))["lessons"][0]["imagenes"][0]
        self.assertEqual(img["url"], "")
        self.assertTrue(Path(img["archivo_local"]).exists())
        self.assertIn("img_IMG", img["archivo_local"])
