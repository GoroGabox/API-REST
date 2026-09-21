"""Tests de los comandos `generate_course` (local) e `import_course` (prod).

`generate_course` requiere PDFs + pipeline, así que aquí solo se cubren sus
validaciones de entrada. `import_course` se cubre de punta a punta con un JSON
combinado, verificando el upsert idempotente contra la BD.
"""
from __future__ import annotations

import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from content_pipeline.processors.clean_text import hash_text_fragment
from schools.models import Curso, Leccion, LeccionFuente, Unidad


def _combined_payload():
    return {
        "manifest": {
            "curso": {
                "nombre": "Curso Demo CLI",
                "codigo": "CLI",
                "descripcion": "Curso de prueba del comando",
                "is_profesional": True,
                "costo": 12345,
            },
            "unidades": [
                {
                    "orden": 1,
                    "nombre": "Unidad 1",
                    "horas_elearning": 1,
                    "categoria": "General",
                    "temas": ["Tema 1"],
                }
            ],
        },
        "lessons": [
            {
                "unidad_orden": 1,
                "unidad_nombre": "Unidad 1",
                "categoria": "General",
                "tema_regulatorio": "Tema 1",
                "nombre": "Lección 1",
                "posicion": 1,
                "tipo": "texto",
                "descripcion": "Descripción.",
                "duracion_min": 15,
                "contenido": "# Lección 1",
                "transcripcion": "",
                "fuentes": [
                    {
                        "fuente_nombre": "Fuente demo",
                        "pagina_inicio": 1,
                        "pagina_fin": 2,
                        "tema_regulatorio": "Tema 1",
                        "fragmento_resumen": "Resumen.",
                        "hash_fragmento": hash_text_fragment("demo"),
                    }
                ],
            }
        ],
    }


class ImportCourseCommandTests(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.file = self.tmp / "curso.json"
        self.file.write_text(json.dumps(_combined_payload()), encoding="utf-8")

    def test_dry_run_no_escribe(self):
        out = StringIO()
        call_command("import_course", file=str(self.file), dry_run=True, stdout=out)
        self.assertEqual(Curso.objects.count(), 0)
        self.assertIn("DRY-RUN", out.getvalue())

    def test_import_crea_y_es_idempotente(self):
        call_command("import_course", file=str(self.file), stdout=StringIO())
        call_command("import_course", file=str(self.file), stdout=StringIO())

        self.assertEqual(Curso.objects.filter(codigo="CLI").count(), 1)
        self.assertEqual(Unidad.objects.count(), 1)
        self.assertEqual(Leccion.objects.count(), 1)
        self.assertEqual(LeccionFuente.objects.count(), 1)

        # El precio ya no vive en el Curso (`costo` fue eliminado): el comando de
        # importación ignora ese campo del payload; el precio se define aparte en
        # PlanCurso. Basta con verificar que el curso se creó (idempotente).
        self.assertTrue(Curso.objects.filter(codigo="CLI").exists())

    def test_file_invalido_falla(self):
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
        with self.assertRaises(CommandError):
            call_command("import_course", file=str(bad), stdout=StringIO())

    def test_requiere_algun_input(self):
        with self.assertRaises(CommandError):
            call_command("import_course", stdout=StringIO())


class GenerateCourseCommandTests(TestCase):
    def test_pdf_inexistente_falla(self):
        with self.assertRaises(CommandError):
            call_command(
                "generate_course",
                temario="no_existe_temario.pdf",
                contenido="no_existe_contenido.pdf",
                nombre="X",
                codigo="X",
                costo=1000,
                out="out.json",
                stdout=StringIO(),
            )

    def test_genera_sin_temario(self):
        """Sin --temario, la estructura se infiere del contenido (pipeline mockeado)."""
        from unittest.mock import patch

        tmp = Path(tempfile.mkdtemp())
        contenido = tmp / "libro.pdf"
        contenido.write_bytes(b"%PDF-fake")
        out = tmp / "curso_sin_temario.json"

        fake_pages = [{"page": 1, "text": "x" * 80, "char_count": 80, "has_text": True}]
        fake_segments = [
            {"segment_id": f"seg_{i}", "title": f"Tema: Concepto {i}", "keywords": [f"k{i}"],
             "text": "y" * 60, "page_start": i, "page_end": i}
            for i in range(1, 7)
        ]
        fake_lessons = [{"nombre": "L1", "tipo": "texto", "posicion": 1, "contenido": "c", "fuentes": []}]

        mod = "schools.management.commands.generate_course."
        with patch("content_pipeline.llm.client.LLMClient.is_available", lambda: False), \
             patch(mod + "extract_pdf_pages", return_value=fake_pages), \
             patch(mod + "segment_pages", return_value=fake_segments), \
             patch(mod + "map_topics_to_segments", return_value=[]), \
             patch(mod + "generate_lessons_generic", return_value=fake_lessons):
            call_command(
                "generate_course",
                contenido=str(contenido),
                nombre="Curso Sin Temario", codigo="NOTEM", costo=5000,
                unidades=2, out=str(out), stdout=StringIO(),
            )

        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertIn("manifest", data)
        self.assertIn("lessons", data)
        self.assertGreaterEqual(len(data["manifest"]["unidades"]), 1)
        self.assertEqual(data["manifest"]["curso"]["codigo"], "NOTEM")
        self.assertEqual(data["manifest"]["curso"]["costo"], 5000)
        self.assertEqual(len(data["lessons"]), 1)
