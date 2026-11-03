"""Biblioteca: galería de señales (figuras del anexo) y subida de PDFs del admin."""
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Usuario
from schools.models import Categoria, Curso, Leccion, LeccionRecurso, Recurso, Unidad


def _pdf_bytes(paginas=2):
    import fitz
    doc = fitz.open()
    for _ in range(paginas):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


class _Storage:
    def __init__(self):
        self.saved = {}

    def save(self, filename, data):
        self.saved[filename] = data
        return f"https://media.example/course_docs/{filename}"


class SenalesTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = Usuario.objects.create_user(
            email="est@example.com", nombre="Ada", apellido="L", password="x12345678", is_estudiante=True,
        )
        curso = Curso.objects.create(nombre="Curso Clase B", codigo="BT", descripcion="")
        cat = Categoria.objects.create(nombre="Señales")
        anexo = Unidad.objects.create(curso=curso, nombre="Anexo: Señales de tránsito", orden=10)
        otra = Unidad.objects.create(curso=curso, nombre="Unidad 1", orden=1)
        self.l_anexo = Leccion.objects.create(curso=curso, unidad=anexo, categoria=cat, nombre="Señales de tránsito",
                                              posicion=1, tipo="texto")
        l_otra = Leccion.objects.create(curso=curso, unidad=otra, categoria=cat, nombre="Ley de tránsito",
                                        posicion=1, tipo="texto")

        def fig(leccion, clave, h, mapeo="anexo", orden=0):
            return LeccionRecurso.objects.create(
                leccion=leccion, tipo="imagen", rol="figura", clave=clave, orden=orden,
                url=f"https://media.example/course_images/B/{clave}.png",
                meta={"pie": f"Pie {clave}", "alt": f"Alt {clave}", "ancho": 200, "alto": 100,
                      "mapeo": mapeo, "hash": h},
            )
        fig(self.l_anexo, "F-1", "h1", orden=1)
        fig(self.l_anexo, "F-2", "h2", orden=2)
        fig(self.l_anexo, "F-dup", "h1", orden=3)          # misma imagen: se omite
        fig(self.l_anexo, "F-x", "hx", mapeo="pagina")     # figura normal, no del anexo
        fig(l_otra, "F-otra", "ho")                        # otra lección (no de señales)

    def test_requiere_auth(self):
        r = self.client.get("/api/v1/schools/library/senales/")
        self.assertIn(r.status_code, (401, 403))

    def test_lista_solo_anexo_sin_duplicados(self):
        self.client.force_authenticate(self.user)
        r = self.client.get("/api/v1/schools/library/senales/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([s["clave"] for s in r.data], ["F-1", "F-2"])
        self.assertEqual(r.data[0]["titulo"], "Pie F-1")
        self.assertEqual(r.data[0]["alt"], "Alt F-1")
        self.assertEqual(r.data[0]["ancho"], 200)


class UploadTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = Usuario.objects.create_user(
            email="admin@example.com", nombre="Ad", apellido="Min", password="x12345678",
        )
        self.admin.is_staff = True
        self.admin.save(update_fields=["is_staff"])
        self.est = Usuario.objects.create_user(
            email="est2@example.com", nombre="Es", apellido="T", password="x12345678", is_estudiante=True,
        )
        self.storage = _Storage()
        patcher = mock.patch("content_pipeline.media.storage.get_storage", return_value=self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, archivo, **campos):
        return self.client.post("/api/v1/schools/library/upload/", {"archivo": archivo, **campos}, format="multipart")

    def test_admin_sube_pdf(self):
        self.client.force_authenticate(self.admin)
        pdf = SimpleUploadedFile("Manual del conductor.pdf", _pdf_bytes(3), content_type="application/pdf")
        r = self._post(pdf, descripcion="Edición 2026")
        self.assertEqual(r.status_code, 201, r.data)
        rec = Recurso.objects.get(id=r.data["id"])
        self.assertEqual(rec.titulo, "Manual del conductor")
        self.assertEqual(rec.tipo, "pdf")
        self.assertEqual(rec.paginas, 3)
        self.assertGreater(rec.size_bytes, 0)
        (nombre,) = self.storage.saved
        self.assertTrue(nombre.startswith("biblioteca/") and nombre.endswith(".pdf"))
        self.assertEqual(rec.url, f"https://media.example/course_docs/{nombre}")

    def test_rechaza_no_pdf(self):
        self.client.force_authenticate(self.admin)
        r = self._post(SimpleUploadedFile("foto.png", b"\x89PNG....", content_type="image/png"))
        self.assertEqual(r.status_code, 400)
        r = self._post(SimpleUploadedFile("falso.pdf", b"no soy pdf", content_type="application/pdf"))
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Recurso.objects.exists())
        self.assertEqual(self.storage.saved, {})

    def test_estudiante_no_puede_subir(self):
        self.client.force_authenticate(self.est)
        r = self._post(SimpleUploadedFile("a.pdf", _pdf_bytes(), content_type="application/pdf"))
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Recurso.objects.exists())
