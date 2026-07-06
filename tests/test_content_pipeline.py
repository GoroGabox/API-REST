from __future__ import annotations

from pathlib import Path

from django.test import TestCase

from content_pipeline.exporters.django_importer import import_a2_course
from content_pipeline.exporters.json_exporter import read_json
from content_pipeline.processors.clean_text import clean_extracted_text, hash_text_fragment
from content_pipeline.processors.map_topics import map_topics_to_segments
from content_pipeline.processors.segment_book import segment_pages
from content_pipeline.processors.validators import validate_manifest
from schools.models import Categoria, Curso, Leccion, LeccionFuente, Unidad


class ContentPipelineFunctionTests(TestCase):
    def test_clean_text_fixes_hyphenation_and_spaces(self):
        raw = "La dis-\n tancia   de frenado\n\nes importante."
        cleaned = clean_extracted_text(raw)
        self.assertIn("distancia de frenado", cleaned)
        self.assertNotIn("  ", cleaned)

    def test_segment_pages_basic(self):
        pages = [
            {
                "page": 1,
                "text": "Velocidad\n\nLa velocidad debe ajustarse al tránsito y a la vía. " * 12,
                "char_count": 600,
                "has_text": True,
            },
            {
                "page": 2,
                "text": "Distancia de seguridad\n\nMantener distancia permite reaccionar y frenar a tiempo. " * 12,
                "char_count": 700,
                "has_text": True,
            },
        ]
        segments = segment_pages(pages, target_min_words=30, target_max_words=120)
        self.assertGreaterEqual(len(segments), 1)
        self.assertEqual(segments[0]["page_start"], 1)
        self.assertIn("segment_id", segments[0])
        self.assertTrue(segments[0]["keywords"])

    def test_topic_segment_similarity(self):
        manifest = {
            "curso": {"codigo": "A2", "nombre": "Curso Profesional Clase A2"},
            "unidades": [
                {
                    "orden": 1,
                    "nombre": "Normativa vial",
                    "categoria": "Infraestructura vial",
                    "temas": ["Distancia entre vehículos"],
                }
            ],
        }
        segments = [
            {
                "segment_id": "seg_0001",
                "title": "Distancia de seguridad",
                "page_start": 10,
                "page_end": 12,
                "text": "La distancia entre vehículos permite reaccionar, frenar y evitar accidentes.",
                "keywords": ["distancia", "vehiculos", "frenar", "accidentes"],
            }
        ]
        mappings = map_topics_to_segments(manifest, segments, min_score=0.01, top_k=3)
        self.assertEqual(mappings[0]["matched_segments"][0]["segment_id"], "seg_0001")

    def test_hash_fragment_is_stable(self):
        first = hash_text_fragment("texto con espacios")
        second = hash_text_fragment("texto   con\n espacios")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_manifest_is_valid(self):
        manifest_path = Path(__file__).resolve().parents[1] / "content_pipeline" / "manifests" / "a2_course_manifest.json"
        errors = validate_manifest(read_json(manifest_path))
        self.assertEqual(errors, [])


class DjangoImporterTests(TestCase):
    def _manifest(self):
        return {
            "curso": {
                "nombre": "Curso Profesional Clase A2",
                "codigo": "A2",
                "descripcion": "Curso A2 de prueba",
                "is_profesional": True,
            },
            "unidades": [
                {
                    "orden": 1,
                    "nombre": "Legislación de Tránsito",
                    "horas_elearning": 1,
                    "categoria": "Legislación",
                    "temas": ["Distancia de frenado"],
                }
            ],
        }

    def _lessons(self):
        return [
            {
                "unidad_orden": 1,
                "unidad_nombre": "Legislación de Tránsito",
                "categoria": "Legislación",
                "tema_regulatorio": "Distancia de frenado",
                "nombre": "Distancia de frenado",
                "posicion": 1,
                "tipo": "texto",
                "descripcion": "Explica la distancia de frenado.",
                "duracion_min": 20,
                "contenido": "# Distancia de frenado",
                "transcripcion": "Texto breve",
                "fuentes": [
                    {
                        "fuente_nombre": "Libro del Nuevo Conductor Clase A2",
                        "pagina_inicio": 10,
                        "pagina_fin": 12,
                        "tema_regulatorio": "Distancia de frenado",
                        "fragmento_resumen": "Referencia de prueba.",
                        "hash_fragmento": hash_text_fragment("distancia"),
                    }
                ],
            },
            {
                "unidad_orden": 1,
                "unidad_nombre": "Legislación de Tránsito",
                "categoria": "Legislación",
                "tema_regulatorio": "Evaluación módulo 1",
                "nombre": "Evaluación del módulo 1",
                "posicion": 2,
                "tipo": "quiz",
                "descripcion": "Quiz de prueba.",
                "duracion_min": 10,
                "contenido": {
                    "questions": [
                        {
                            "question": "¿Qué se prioriza?",
                            "options": ["Rapidez", "Seguridad"],
                            "correct_index": 1,
                            "explanation": "La seguridad es prioritaria.",
                        }
                    ],
                    "passing_score": 75,
                },
                "transcripcion": "",
                "fuentes": [
                    {
                        "fuente_nombre": "Libro del Nuevo Conductor Clase A2",
                        "pagina_inicio": 10,
                        "pagina_fin": 12,
                        "tema_regulatorio": "Evaluación módulo 1",
                        "fragmento_resumen": "Quiz basado en la unidad.",
                        "hash_fragmento": hash_text_fragment("quiz"),
                    }
                ],
            },
        ]

    def test_import_is_idempotent(self):
        import_a2_course(self._manifest(), self._lessons(), dry_run=False)
        import_a2_course(self._manifest(), self._lessons(), dry_run=False)

        self.assertEqual(Curso.objects.filter(codigo="A2").count(), 1)
        self.assertEqual(Categoria.objects.filter(nombre="Legislación").count(), 1)
        self.assertEqual(Unidad.objects.count(), 1)
        self.assertEqual(Leccion.objects.count(), 2)
        self.assertEqual(LeccionFuente.objects.count(), 2)
