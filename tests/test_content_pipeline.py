from __future__ import annotations

from pathlib import Path

from django.test import SimpleTestCase, TestCase

from content_pipeline import taxonomy
from content_pipeline.llm.client import LLMResponse
from content_pipeline.processors.ejercicio_classifier import clasificar
from content_pipeline.processors.llm_lesson_writer import (
    _REQUIRED_SECTIONS,
    _write_body,
)
from content_pipeline.exporters.django_importer import import_a2_course, import_ejercicios
from content_pipeline.exporters.json_exporter import read_json
from content_pipeline.processors.clean_text import clean_extracted_text, hash_text_fragment
from content_pipeline.processors.lesson_generator import build_lesson_context, render_student_lesson
from content_pipeline.processors.map_topics import map_topics_to_segments
from content_pipeline.processors.segment_book import segment_pages
from content_pipeline.processors.validators import validate_lessons, validate_manifest
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
        # "Legislación" se canonicaliza a la etiqueta de la taxonomía compartida.
        self.assertEqual(
            Categoria.objects.filter(nombre="Legislación y Normativa de Tránsito").count(), 1
        )
        self.assertEqual(Unidad.objects.count(), 1)
        self.assertEqual(Leccion.objects.count(), 2)
        self.assertEqual(LeccionFuente.objects.count(), 2)


class PedagogicalLessonTests(TestCase):
    def _source(self):
        return [
            {
                "fuente_nombre": "Libro del Nuevo Conductor Clase A2",
                "pagina_inicio": 165,
                "pagina_fin": 177,
                "tema_regulatorio": "Los accidentes de tránsito",
                "fragmento_resumen": "Referencia tecnica de prueba.",
                "hash_fragmento": hash_text_fragment("accidentes"),
            }
        ]

    def test_render_student_lesson_hides_pipeline_language(self):
        segments = [
            {
                "segment_id": "seg_0043",
                "title": "Siniestros de tránsito",
                "page_start": 165,
                "page_end": 177,
                "text": (
                    "En más del 40% de los accidentes de tránsito está involucrado un vehículo que requiere "
                    "una licencia profesional para su conducción. La velocidad, la distancia y la atención "
                    "son factores relevantes para prevenir siniestros con pasajeros y peatones."
                ),
                "keywords": ["accidentes", "velocidad", "distancia"],
            }
        ]
        context = build_lesson_context(
            "Los accidentes de tránsito",
            "Los accidentes de tránsito",
            "Legislación de Tránsito",
            0,
            1,
            segments,
            self._source(),
        )
        markdown = render_student_lesson(context)

        self.assertIn("## Introducción", markdown)
        self.assertIn("## Desarrollo", markdown)
        self.assertIn("## Aplicación en la conducción profesional", markdown)
        self.assertIn("## Ejemplo aplicado", markdown)
        forbidden = ["mapeo", "segmento", "señales desde la fuente", "conducta observable", "conceptos de referencia"]
        self.assertFalse(any(term in markdown.casefold() for term in forbidden))

    def test_validate_lessons_rejects_internal_pipeline_language(self):
        manifest = {
            "curso": {"codigo": "A2", "nombre": "Curso Profesional Clase A2"},
            "unidades": [
                {
                    "orden": 1,
                    "nombre": "Legislación de Tránsito",
                    "horas_elearning": 1,
                    "categoria": "Legislación",
                    "temas": ["Los accidentes de tránsito"],
                },
                {"orden": 2, "nombre": "U2", "horas_elearning": 1, "categoria": "C", "temas": ["Tema"]},
                {"orden": 3, "nombre": "U3", "horas_elearning": 1, "categoria": "C", "temas": ["Tema"]},
                {"orden": 4, "nombre": "U4", "horas_elearning": 1, "categoria": "C", "temas": ["Tema"]},
                {"orden": 5, "nombre": "U5", "horas_elearning": 1, "categoria": "C", "temas": ["Tema"]},
                {"orden": 6, "nombre": "U6", "horas_elearning": 1, "categoria": "C", "temas": ["Tema"]},
                {"orden": 7, "nombre": "U7", "horas_elearning": 1, "categoria": "C", "temas": ["Tema"]},
            ],
        }
        content = """# Los accidentes de tránsito

## Objetivo
Texto.

## Introducción
Texto.

## Desarrollo
El mapeo tomó como base un segmento interno.

## Aplicación en la conducción profesional
Texto.

## Puntos clave
- Punto.

## Ejemplo aplicado
Un conductor circula por una avenida con pasajeros y reduce la velocidad antes de un cruce.

## Errores frecuentes
- Error.

## Actividad breve
Pregunta.

## Resumen
Cierre.

## Fuente
Libro del Nuevo Conductor Clase A2, páginas 1-2."""
        lessons = [
            {
                "unidad_orden": 1,
                "unidad_nombre": "Legislación de Tránsito",
                "categoria": "Legislación",
                "tema_regulatorio": "Los accidentes de tránsito",
                "nombre": "Los accidentes de tránsito",
                "posicion": 1,
                "tipo": "texto",
                "descripcion": "Prueba",
                "duracion_min": 10,
                "contenido": content,
                "fuentes": self._source(),
            },
            {
                "unidad_orden": 1,
                "unidad_nombre": "Legislación de Tránsito",
                "categoria": "Legislación",
                "tema_regulatorio": "Evaluación módulo 1",
                "nombre": "Evaluación del módulo 1",
                "posicion": 2,
                "tipo": "quiz",
                "descripcion": "Quiz",
                "duracion_min": 10,
                "contenido": {"questions": [{"question": "?", "options": ["A", "B"], "correct_index": 0}], "passing_score": 75},
                "fuentes": self._source(),
            },
        ]
        for orden in range(2, 8):
            lessons.append(
                {
                    "unidad_orden": orden,
                    "unidad_nombre": f"U{orden}",
                    "categoria": "C",
                    "tema_regulatorio": "Tema",
                    "nombre": "Tema",
                    "posicion": 1,
                    "tipo": "quiz",
                    "descripcion": "Quiz",
                    "duracion_min": 60,
                    "contenido": {"questions": [{"question": "?", "options": ["A", "B"], "correct_index": 0}], "passing_score": 75},
                    "fuentes": self._source(),
                }
            )

        result = validate_lessons(manifest, lessons)

        self.assertFalse(result.is_valid)
        self.assertIn("Lección no publicable", result.report)
        self.assertIn("mapeo", result.report.casefold())


def _full_body(marker: str = "ok") -> str:
    """Cuerpo de lección con todas las secciones obligatorias."""
    parts = [f"# Título {marker}"]
    for heading in _REQUIRED_SECTIONS:
        parts.append(f"{heading}\nContenido {marker} para {heading}.")
    return "\n\n".join(parts)


class _FakeLLM:
    """Cliente LLM falso: devuelve respuestas predefinidas y registra los
    presupuestos de tokens con que fue llamado."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.budgets: list[int] = []

    def complete_meta(self, *, max_tokens: int, **_kwargs) -> LLMResponse:
        self.budgets.append(max_tokens)
        return self._responses.pop(0)


class LessonBodyTruncationTests(SimpleTestCase):
    def _context(self):
        return build_lesson_context(
            "Los accidentes de tránsito",
            "Los accidentes de tránsito",
            "Legislación de Tránsito",
            0,
            1,
            [],
            [],
        )

    def _write(self, client):
        return _write_body(
            title="Los accidentes de tránsito",
            tema="Los accidentes de tránsito",
            unidad_nombre="Legislación de Tránsito",
            objetivos=["Reconocer factores de riesgo"],
            segments=[],
            sources=[],
            source_name="Libro del Nuevo Conductor",
            client=client,
            model="fake-model",
            context=self._context(),
        )

    def test_complete_body_gets_fuente_appended(self):
        client = _FakeLLM([LLMResponse(_full_body(), stop_reason="end_turn")])
        body = self._write(client)
        self.assertIn("## Fuente", body)
        self.assertEqual(client.budgets, [3_200])  # sin reintento

    def test_truncated_first_attempt_retries_with_more_budget(self):
        client = _FakeLLM(
            [
                LLMResponse(_full_body("cortado")[:120], stop_reason="max_tokens"),
                LLMResponse(_full_body("completo"), stop_reason="end_turn"),
            ]
        )
        body = self._write(client)
        self.assertIn("completo", body)
        self.assertIn("## Fuente", body)
        self.assertEqual(client.budgets, [3_200, 4_096])  # reintentó con más margen

    def test_persistent_truncation_falls_back_to_extractive(self):
        client = _FakeLLM(
            [
                LLMResponse("# T\n\n## Objetivo\ncortado", stop_reason="max_tokens"),
                LLMResponse("# T\n\n## Objetivo\ncortado de nuevo", stop_reason="max_tokens"),
            ]
        )
        body = self._write(client)
        # El extractivo neutral emite las 9 secciones completas + la fuente.
        for heading in _REQUIRED_SECTIONS:
            self.assertIn(heading, body)
        self.assertIn("## Fuente", body)
        self.assertEqual(len(client.budgets), 2)  # agotó ambos intentos

    def test_missing_section_is_treated_as_incomplete(self):
        incomplete = _full_body().replace("## Resumen", "## OtroTitulo")
        client = _FakeLLM(
            [
                LLMResponse(incomplete, stop_reason="end_turn"),
                LLMResponse(_full_body("segundo"), stop_reason="end_turn"),
            ]
        )
        body = self._write(client)
        self.assertIn("segundo", body)
        self.assertEqual(client.budgets, [3_200, 4_096])


class TaxonomyResolveTests(SimpleTestCase):
    def test_variants_collapse_to_canonical(self):
        cases = {
            "Primeros auxilios": "Primeros Auxilios",
            "Mecánica y mantención": "Mecánica y Mantención del Vehículo",
            "Mecánica y Mantención Preventiva": "Mecánica y Mantención del Vehículo",
            "Mecanica basica": "Mecánica y Mantención del Vehículo",
            "Transporte de carga": "Transporte Profesional (Carga y Pasajeros)",
            "Reglamentación aplicada al transporte de pasajeros": "Transporte Profesional (Carga y Pasajeros)",
            "Introducción a la seguridad vial y el vehículo": "Conducción Defensiva",
            "Señales reglamentarias": "Señales de Tránsito",
            "Reglas de prioridad": "Prioridad y Derecho de Paso",
            "Normativa, documentación y seguridad": "Legislación y Normativa de Tránsito",
        }
        for raw, canonical in cases.items():
            self.assertEqual(taxonomy.resolve(raw), canonical, raw)

    def test_unknown_and_empty_fall_back_to_general(self):
        self.assertEqual(taxonomy.resolve("algo que no existe"), taxonomy.FALLBACK)
        self.assertEqual(taxonomy.resolve(""), taxonomy.FALLBACK)
        self.assertEqual(taxonomy.resolve(None), taxonomy.FALLBACK)

    def test_every_canonical_resolves_to_itself(self):
        for name in taxonomy.CATEGORY_NAMES:
            self.assertEqual(taxonomy.resolve(name), name)


class CategoriaDedupImportTests(TestCase):
    def _course(self, codigo, categorias):
        """Manifest + lecciones de una unidad por cada categoría dada."""
        unidades = []
        lessons = []
        for i, cat in enumerate(categorias, start=1):
            unidades.append(
                {
                    "orden": i,
                    "nombre": f"Módulo {i}",
                    "horas_elearning": 1,
                    "categoria": cat,
                    "temas": [f"Tema {i}"],
                }
            )
            lessons.append(
                {
                    "unidad_orden": i,
                    "unidad_nombre": f"Módulo {i}",
                    "categoria": cat,
                    "tema_regulatorio": f"Tema {i}",
                    "nombre": f"Lección {i}",
                    "posicion": 1,
                    "tipo": "texto",
                    "descripcion": "d",
                    "duracion_min": 20,
                    "contenido": "# x",
                    "transcripcion": "",
                    "fuentes": [],
                }
            )
        manifest = {
            "curso": {"nombre": f"Curso {codigo}", "codigo": codigo, "descripcion": "d"},
            "unidades": unidades,
        }
        return manifest, lessons

    def test_variant_categories_across_courses_dedupe(self):
        # Dos cursos con la misma familia de conceptos escrita distinto.
        m1, l1 = self._course("B", ["Primeros Auxilios", "Mecánica y Mantención Preventiva"])
        m2, l2 = self._course("A2", ["primeros auxilios", "Mecánica y mantención"])
        import_a2_course(m1, l1, dry_run=False)
        import_a2_course(m2, l2, dry_run=False)

        self.assertEqual(Categoria.objects.filter(nombre="Primeros Auxilios").count(), 1)
        self.assertEqual(
            Categoria.objects.filter(nombre="Mecánica y Mantención del Vehículo").count(), 1
        )
        # Sin filas fuera de la taxonomía (ni "primeros auxilios" ni variantes).
        canon = set(taxonomy.CATEGORY_NAMES) | {taxonomy.FALLBACK}
        for name in Categoria.objects.values_list("nombre", flat=True):
            self.assertIn(name, canon, name)

    def test_every_lesson_has_a_category(self):
        m, l = self._course("C", ["Señales reglamentarias", "algo inclasificable"])
        import_a2_course(m, l, dry_run=False)
        self.assertFalse(Leccion.objects.filter(categoria__isnull=True).exists())
        # La categoría desconocida cae a "General", no a null.
        self.assertTrue(Categoria.objects.filter(nombre=taxonomy.FALLBACK).exists())

    def test_canonical_category_gets_official_color(self):
        m, l = self._course("B", ["Señales de tránsito"])
        import_a2_course(m, l, dry_run=False)
        cat = Categoria.objects.get(nombre="Señales de Tránsito")
        self.assertEqual(cat.color_hex, taxonomy.color_for("Señales de Tránsito"))

    def test_ejercicios_share_taxonomy_with_lessons(self):
        # Lección en "Señales de Tránsito" + ejercicio clasificado como variante:
        # ambos deben apuntar a la MISMA fila canónica.
        m, l = self._course("B", ["Señales de tránsito"])
        import_a2_course(m, l, dry_run=False)
        import_ejercicios(
            [
                {
                    "pregunta": "¿Qué indica un disco PARE?",
                    "opciones": {"a": "Detenerse", "b": "Seguir"},
                    "respuestas": ["a"],
                    "categoria": "Señales reglamentarias",
                }
            ],
            dry_run=False,
        )
        self.assertEqual(Categoria.objects.filter(nombre="Señales de Tránsito").count(), 1)


class EjercicioClassifierTaxonomyTests(SimpleTestCase):
    def test_classifier_shares_canonical_taxonomy(self):
        from content_pipeline.processors import ejercicio_classifier as ec

        self.assertEqual(ec.TAXONOMIA, taxonomy.CATEGORY_NAMES)
        self.assertEqual(ec.CATEGORIA_FALLBACK, taxonomy.FALLBACK)

    def test_classifier_falls_back_when_llm_unavailable(self):
        from unittest.mock import patch

        from content_pipeline.llm.client import LLMClient

        with patch.object(LLMClient, "is_available", return_value=False):
            got = clasificar([{"numero": 1, "pregunta": "x", "opciones": {"a": "1"}}])
        self.assertEqual(got, {1: taxonomy.FALLBACK})

