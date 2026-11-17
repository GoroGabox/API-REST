"""Cursos HTML interactivos → curso + banco (curso_html) y presentación inicial (presentacion_curso)."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from accounts.models import EstudianteLeccion
from accounts.tests import make_user
from content_pipeline.exporters.django_importer import import_a2_course, import_ejercicios, importar_presentacion
from content_pipeline.processors import curso_html as H
from content_pipeline.processors import presentacion as P
from content_pipeline.processors.content_blocks import parse_blocks
from content_pipeline.processors.validators import validate_generated_course
from schools.models import Categoria, Curso, Ejercicio, EjercicioUnidad, Leccion, Unidad
from schools.services import ejercicios_de_unidad

PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
       "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")

HTML = """<!doctype html><title>Curso</title><script>
const SIGNS={"137": "%s"};
const SVG = {
lights:`<svg viewBox="0 0 100 50"><rect x="${2*5}" y="5" width="40" height="40" fill="var(--accent)"/></svg>`
};
const fig=(k,cap)=>`<figure class="plate" style="margin:0"><div class="svgbox">${SVG[k]}</div><figcaption>${cap}</figcaption></figure>`;
const INTRO_C=()=>`<p>intro</p>`;
const MODULES=[
{id:'mp',group:'intro',title:'Requisitos',short:'x',html:`${INTRO_C()}`},
{id:'m1',group:'core',title:'Normas de circulación',short:'1',html:`
<p>Texto inicial con <b>negrita</b>.</p>
<h3>Los semáforos</h3>
${fig('lights','Significado de cada luz.')}
<ul><li><b>Verde:</b> paso.</li><li>Rojo: detención.</li></ul>
<h3>La velocidad</h3>
<div class="tbl"><table class="t"><tr><th>Lugar</th><th>Máximo</th></tr><tr><td>Urbana</td><td>50 km/h</td></tr></table></div>
<div class="key"><strong>Recuerda</strong><p>La velocidad debe ser razonable.</p></div>
<table class="t"><tr><td>1. Detener</td><td>2. Ayudar</td></tr></table>`},
{id:'x1',group:'extra',title:'Tractor',short:'t',html:`<h3>Partes</h3><p>Cabina y motor.</p>`}
];
const OWN=[
['m1','¿Cuál es la velocidad máxima urbana?',['40 km/h','50 km/h','60 km/h'],1,'Es 50 km/h.'],
['x1','¿Qué tiene un tractor?',['Cabina','Alas'],0,'Cabina.']
];
const OFFICIAL=[{"n": 137, "mod": "m1", "off": 1, "q": "¿Qué señal es?", "o": ["Pare", "Ceda"], "a": 0, "e": "Pare.", "img": 137}];
const Q=OWN.map((a,i)=>({id:'p'+i,mod:a[0],q:a[1],o:a[2],a:a[3],e:a[4]}));
OFFICIAL.forEach(q=>{q.id='o'+q.n;Q.push(q)});
/* ---------- Estado ---------- */
document.body.innerHTML='no se ejecuta';
</script>""" % PNG


@unittest.skipUnless(shutil.which("node"), "requiere node")
class CursoHtmlTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory()
        path = Path(cls.tmp.name) / "curso.html"
        path.write_text(HTML, encoding="utf-8")
        cls.datos = H.extraer(path)
        cls.curso, cls.ejercicios, cls.figuras, cls.mod_unidad = H.armar_curso(
            cls.datos, nombre="Curso C", codigo="C", precio=7000, profesional=False, fuente="Material C",
            libro="Libro moto", paginas_unidad={1: (6, 10)}, titulo_extras="Otras")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        super().tearDownClass()

    def test_extrae_datos_sin_ejecutar_el_dom(self):
        self.assertEqual([m["id"] for m in self.datos["modules"]], ["mp", "m1", "x1"])
        self.assertIn('x="10"', self.datos["svg"]["lights"])        # plantilla evaluada
        self.assertEqual(len(self.datos["q"]), 3)

    def test_estructura_unidades_y_lecciones(self):
        self.assertEqual(self.mod_unidad, {"m1": 1, "x1": 2})        # intro fuera; extra = unidad final
        nombres = [(l["unidad_orden"], l["tipo"], l["nombre"]) for l in self.curso["lessons"]]
        self.assertEqual(nombres[0], (1, "texto", "Normas de circulación"))
        self.assertEqual([n for n in nombres if n[1] == "quiz"],
                         [(1, "quiz", "Evaluación de la unidad 1"), (2, "quiz", "Evaluación de la unidad 2")])
        l = self.curso["lessons"][0]
        self.assertEqual((l["fuentes"][0]["pagina_inicio"], l["fuentes"][0]["pagina_fin"]), (6, 10))
        self.assertEqual(self.curso["lessons"][2]["fuentes"][0]["fuente_nombre"], "Material C")

    def test_html_a_markdown(self):
        c = self.curso["lessons"][0]["contenido"]
        self.assertIn("Texto inicial con **negrita**.", c)
        self.assertIn("### Los semáforos", c)
        self.assertIn("- **Verde:** paso.", c)
        self.assertIn("- 1. Detener\n- 2. Ayudar", c)                 # tabla de una fila → lista
        bloques = parse_blocks(c)
        self.assertEqual([b["header"] for b in bloques if b["t"] == "table"], [["Lugar", "Máximo"]])
        recuadro = [b for b in bloques if b["t"] == "callout"][0]
        self.assertEqual((recuadro["tipo"], recuadro["titulo"]), ("DEBES_SABER", "Recuerda"))

    def test_lamina_svg_rasterizada_como_figura(self):
        l = self.curso["lessons"][0]
        rec = l["recursos"][0]
        self.assertIn(f"{{{{figura:{rec['clave']}}}}}", l["contenido"])
        self.assertEqual(rec["meta"]["pie"], "Significado de cada luz.")
        png = next(f for f in self.figuras if f["archivo"] == rec["archivo_local"])
        self.assertTrue(png["data"].startswith(b"\x89PNG"))

    def test_banco_con_unidad_imagen_y_quiz_embebido(self):
        por_num = {e["numero"]: e for e in self.ejercicios}
        self.assertEqual(por_num["p0"]["respuestas"], ["b"])
        self.assertEqual(por_num["p1"]["unidad_orden"], 2)
        oficial = por_num["o137"]
        self.assertTrue(oficial["imagen_archivo"].endswith(".png"))
        self.assertIn("CONASET", oficial["fuente"])
        quiz = next(l for l in self.curso["lessons"] if l["tipo"] == "quiz")
        self.assertEqual([q["question"] for q in quiz["contenido"]["questions"]],
                         ["¿Cuál es la velocidad máxima urbana?"])     # sin la de imagen

    def test_valida_tras_completar_secciones(self):
        class FakeLLM:
            def complete(self, **kw):
                return "- Identificar la velocidad máxima urbana."

        curso = json.loads(json.dumps(self.curso))
        from content_pipeline.processors.guia_docx import completar_secciones
        completar_secciones(curso, FakeLLM(), log=lambda *_: None)
        val = validate_generated_course(curso["manifest"], curso["lessons"])
        self.assertEqual(val["errores"], [])


class PresentacionTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.carpeta = Path(self.tmp.name)
        for nombre in ("comun_declaracion.md", "comun_salud_faq.md"):
            shutil.copy(P.CARPETA / nombre, self.carpeta / nombre)
        (self.carpeta / "B.md").write_text(
            '---\nlicencia: B\nrenovacion: "Cada 6 años."\nfuentes:\n  - "Ley 18.290"\n---\n'
            "Intro.\n\n### Requisitos para postular\n\n| Requisito | Detalle |\n| --- | --- |\n| Edad | 18 |\n\n"
            "### El camino a la licencia\n\n1. Examen médico.\n", encoding="utf-8")
        self.urls = {"pdf_salud": "https://m.example/s.pdf", "pdf_drogas": "https://m.example/d.pdf"}
        self.curso = Curso.objects.create(nombre="Curso B", descripcion="d", codigo="B")
        self.u1 = Unidad.objects.create(curso=self.curso, nombre="U1", orden=1)
        self.l1 = Leccion.objects.create(curso=self.curso, unidad=self.u1, nombre="L1", posicion=1, tipo="texto")
        self.l2 = Leccion.objects.create(curso=self.curso, unidad=self.u1, nombre="L2", posicion=2, tipo="texto")

    def tearDown(self):
        self.tmp.cleanup()

    def test_componer_inserta_partes_comunes(self):
        spec = P.componer("B", self.urls, self.carpeta)
        c = spec["contenido"]
        self.assertLess(c.index("### Requisitos para postular"), c.index("### La declaración jurada"))
        self.assertLess(c.index("### La declaración jurada"), c.index("### El camino a la licencia"))
        self.assertIn("(https://m.example/s.pdf)", c)
        self.assertIn("**¿Cada cuánto se renueva?**\n\nCada 6 años.", c)
        self.assertTrue(c.rstrip().endswith("- Ley 18.290"))
        self.assertEqual(P.archivo_licencia("B1", self.carpeta).name, "B.md")

    def test_importar_idempotente_con_avance_y_prune(self):
        completo = make_user("c@a.com", is_estudiante=True)
        parcial = make_user("p@a.com", is_estudiante=True)
        for l in (self.l1, self.l2):
            EstudianteLeccion.objects.create(estudiante=completo, leccion=l, curso=self.curso)
        EstudianteLeccion.objects.create(estudiante=parcial, leccion=self.l1, curso=self.curso)
        spec = P.componer("B", self.urls, self.carpeta)
        r = importar_presentacion(self.curso, spec)
        self.assertEqual(r.counters["avance_completado"], 1)
        r = importar_presentacion(self.curso, spec)
        self.assertEqual((r.counters["leccion_update"], r.counters["avance_completado"]), (1, 0))
        pres = Leccion.objects.get(curso=self.curso, unidad__orden=0)
        self.assertEqual((pres.nombre, pres.posicion, pres.unidad.nombre),
                         ("Requisitos y exámenes", 1, "Antes de empezar"))
        self.assertTrue(EstudianteLeccion.objects.filter(estudiante=completo, leccion=pres).exists())
        self.assertFalse(EstudianteLeccion.objects.filter(estudiante=parcial, leccion=pres).exists())
        # Re-importar el curso con --prune no borra la unidad 0.
        manifest = {"curso": {"nombre": "Curso B", "codigo": "B", "descripcion": "d", "costo": 1},
                    "unidades": [{"orden": 1, "nombre": "U1", "categoria": "General"}]}
        lessons = [{"unidad_orden": 1, "posicion": 1, "nombre": "L1", "tipo": "texto", "contenido": "x",
                    "categoria": "General", "fuentes": []}]
        dry = import_a2_course(manifest, lessons, dry_run=True, prune=True)
        self.assertEqual((dry.counters["leccion_delete"], dry.counters["unidad_delete"]), (1, 0))
        import_a2_course(manifest, lessons, prune=True)
        self.assertTrue(Leccion.objects.filter(id=pres.id).exists())
        self.assertFalse(Leccion.objects.filter(id=self.l2.id).exists())


class ImagenBancoTests(TestCase):
    def test_imagen_se_guarda_y_no_entra_al_quiz_de_unidad(self):
        curso = Curso.objects.create(nombre="Curso C", descripcion="d", codigo="C")
        u = Unidad.objects.create(curso=curso, nombre="U1", orden=1)
        base = {"respuestas": ["a"], "multi": False, "categoria": "Señales de Tránsito", "unidad_orden": 1}
        items = [{**base, "pregunta": "¿Qué señal es?", "opciones": {"a": "Pare", "b": "Ceda"},
                  "imagen": "https://m.example/137.webp"},
                 {**base, "pregunta": "¿Velocidad urbana?", "opciones": {"a": "50", "b": "60"}}]
        import_ejercicios(items, curso=curso)
        con = Ejercicio.objects.get(pregunta="¿Qué señal es?")
        self.assertEqual(con.imagen, "https://m.example/137.webp")
        self.assertEqual(EjercicioUnidad.objects.filter(unidad=u).count(), 2)
        self.assertEqual(list(ejercicios_de_unidad(u).values_list("pregunta", flat=True)), ["¿Velocidad urbana?"])
        # Re-importar con imagen completa una fila que tenía el placeholder.
        sin = Ejercicio.objects.get(pregunta="¿Velocidad urbana?")
        import_ejercicios([{**items[1], "imagen": "https://m.example/x.png"}], curso=curso)
        sin.refresh_from_db()
        self.assertEqual(sin.imagen, "https://m.example/x.png")
        self.assertTrue(Categoria.objects.filter(nombre="Señales de Tránsito").exists())

    def test_comando_falla_sin_medios(self):
        Curso.objects.create(nombre="Curso X", descripcion="d", codigo="ZZ")
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("presentacion_curso", "--curso", "ZZ", "--dry-run")
