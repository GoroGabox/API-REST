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
from content_pipeline.processors.map_topics import coverage_alert, map_topics_to_segments
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


class LicenseOrientationTests(SimpleTestCase):
    def test_orientation_per_code_and_override(self):
        from content_pipeline.licenses import orientation_for
        self.assertIn("carga", orientation_for("A4"))
        self.assertIn("pasajeros", orientation_for("A2"))
        self.assertEqual(orientation_for("a4", "texto propio"), "texto propio")  # override gana
        self.assertIsNone(orientation_for("ZZZ"))  # desconocida => None
        self.assertIsNone(orientation_for(""))


class ParagraphAnchorTests(SimpleTestCase):
    def test_segment_pages_tags_blocks_with_pages(self):
        pages = [
            {"page": 40, "text": "Bloque en página cuarenta sobre velocidad. " * 8, "has_text": True},
            {"page": 41, "text": "Otro bloque distinto en página cuarenta y uno. " * 8, "has_text": True},
        ]
        segs = segment_pages(pages, target_min_words=10, target_max_words=90)
        all_blocks = [b for s in segs for b in s["blocks"]]
        self.assertTrue(all_blocks)
        self.assertTrue(all(isinstance(b["page"], int) and b["text"] for b in all_blocks))
        self.assertEqual({b["page"] for b in all_blocks}, {40, 41})

    def test_sources_anchor_to_paragraph_page(self):
        from content_pipeline.processors.lesson_generator import _sources_for_segments
        segments = [{
            "segment_id": "s1", "page_start": 40, "page_end": 45,
            "text": "irrelevante", "keywords": [],
            "blocks": [
                {"page": 40, "text": "Este párrafo habla de mantención general del vehículo y rutinas."},
                {"page": 43, "text": "La distancia de frenado depende de la velocidad y el estado de los frenos y el pavimento del camino."},
            ],
        }]
        fuentes = _sources_for_segments(segments, "distancia de frenado y velocidad")
        # Ancla al párrafo relevante (pág. 43), no al rango 40-45.
        self.assertTrue(fuentes)
        top = fuentes[0]
        self.assertEqual(top["pagina_inicio"], 43)
        self.assertEqual(top["pagina_fin"], 43)
        self.assertIn("distancia de frenado", top["fragmento_resumen"].lower())

    def test_sources_fallback_without_blocks(self):
        from content_pipeline.processors.lesson_generator import _sources_for_segments
        segments = [{"segment_id": "s1", "page_start": 10, "page_end": 20, "text": "algo de contenido", "keywords": []}]
        fuentes = _sources_for_segments(segments, "tema")
        self.assertEqual(fuentes[0]["pagina_inicio"], 10)  # cae al rango del segmento
        self.assertEqual(fuentes[0]["pagina_fin"], 20)


class FaithfulnessTests(SimpleTestCase):
    def test_unsupported_figures_flags_invented_numbers(self):
        from content_pipeline.processors.faithfulness import unsupported_figures
        source = "El límite es 50 km/h en zona urbana. El 80% de los siniestros. A los 18 años."
        content = (
            "Circular a 50 km/h es lo permitido. El 80% de los casos. "
            "Una multa de $90.000 y esperar 30 días."  # 90000 y 30 NO están en la fuente
        )
        figs = unsupported_figures(content, source)
        joined = " ".join(figs).lower()
        self.assertIn("90.000", joined)
        self.assertIn("30", joined)
        self.assertNotIn("50 km", joined)   # 50 sí está
        self.assertNotIn("80 %", joined.replace("%", " %"))  # 80 sí está

    def test_number_separators_are_normalized(self):
        from content_pipeline.processors.faithfulness import unsupported_figures
        source = "Se registran 82.000 siniestros al año."
        content = "Hay 82000 siniestros por año."  # mismo número, sin separador
        self.assertEqual(unsupported_figures(content, source), [])

    def test_anchoring_score_high_and_low(self):
        from content_pipeline.processors.faithfulness import anchoring_score
        source = "La distancia de frenado depende de la velocidad y el estado del pavimento y los frenos."
        alto = anchoring_score("La velocidad y la distancia de frenado y los frenos.", source)
        bajo = anchoring_score("Recetas de cocina italiana con tomate albahaca y queso parmesano.", source)
        self.assertGreater(alto, bajo)
        self.assertLess(bajo, 0.28)

    def test_audit_lessons_reports_without_blocking(self):
        from content_pipeline.processors.faithfulness import audit_lessons
        segments = [{"segment_id": "s1", "text": "El límite es 50 km/h. El 80% de los casos.", "page_start": 10}]
        mappings = [{"unidad_orden": 1, "tema": "Velocidad", "matched_segments": [{"segment_id": "s1"}]}]
        lessons = [{
            "tipo": "texto", "unidad_orden": 1, "tema_regulatorio": "Velocidad", "nombre": "Velocidad",
            "contenido": "A 50 km/h. Pero una multa de $120.000.",  # 120000 inventado
        }]
        res = audit_lessons(lessons, segments, mappings)
        self.assertEqual(res["auditadas"], 1)
        self.assertEqual(len(res["figuras"]), 1)
        self.assertIn("120.000", " ".join(res["figuras"][0]["cifras"]))

    def test_audit_skips_lessons_without_source(self):
        from content_pipeline.processors.faithfulness import audit_lessons
        lessons = [{"tipo": "texto", "unidad_orden": 9, "tema_regulatorio": "X", "nombre": "X", "contenido": "algo 999 km/h"}]
        res = audit_lessons(lessons, segments=[], mappings=[])
        self.assertEqual(res["auditadas"], 0)  # sin fuente => no se audita
        self.assertEqual(res["figuras"], [])

    def test_judge_lessons_llm_reports_low_faithfulness(self):
        import json as _json
        from content_pipeline.processors.faithfulness import judge_lessons_llm

        class _Judge:
            def __init__(self, payload): self._raw = _json.dumps(payload, ensure_ascii=False)
            def complete(self, **kw): return self._raw

        pairs = [
            ({"tipo": "texto", "unidad_orden": 1, "nombre": "Buena", "contenido": "c"}, "fuente"),
            ({"tipo": "texto", "unidad_orden": 1, "nombre": "Mala", "contenido": "c"}, "fuente"),
        ]
        # Cliente que siempre reporta baja fidelidad con un reparo => críticas.
        client = _Judge({"faithfulness": 0.4, "unsupported_claims": ["cifra inventada"]})
        res = judge_lessons_llm(pairs, client=client, model="fake")
        self.assertEqual(res["evaluadas"], 2)
        self.assertEqual(len(res["criticas"]), 2)
        self.assertEqual(len(res["con_reparos"]), 0)
        self.assertIn("cifra inventada", res["criticas"][0]["claims"])
        self.assertEqual(res["promedio"], 0.4)

    def test_judge_separates_critical_from_minor(self):
        import json as _json
        from content_pipeline.processors.faithfulness import judge_lessons_llm

        class _Judge:
            def __init__(self, payload): self._raw = _json.dumps(payload, ensure_ascii=False)
            def complete(self, **kw): return self._raw

        # score 0.9 con un reparo => reparo menor, NO crítica (umbral 0.7).
        client = _Judge({"faithfulness": 0.9, "unsupported_claims": ["detalle menor"]})
        pairs = [({"tipo": "texto", "unidad_orden": 1, "nombre": "L", "contenido": "c"}, "fuente")]
        res = judge_lessons_llm(pairs, client=client, model="fake")
        self.assertEqual(len(res["criticas"]), 0)
        self.assertEqual(len(res["con_reparos"]), 1)
        # Subir el umbral a 0.95 convierte ese reparo en crítica.
        res2 = judge_lessons_llm(pairs, client=client, model="fake", judge_min=0.95)
        self.assertEqual(len(res2["criticas"]), 1)
        self.assertEqual(len(res2["con_reparos"]), 0)

    def test_build_lesson_sources_pairs_texto_with_source(self):
        from content_pipeline.processors.faithfulness import build_lesson_sources
        segments = [{"segment_id": "s1", "text": "texto fuente", "page_start": 1}]
        mappings = [{"unidad_orden": 1, "tema": "T", "matched_segments": [{"segment_id": "s1"}]}]
        lessons = [
            {"tipo": "texto", "unidad_orden": 1, "tema_regulatorio": "T", "nombre": "L", "contenido": "c"},
            {"tipo": "quiz", "unidad_orden": 1, "tema_regulatorio": "Eval", "nombre": "Q", "contenido": {}},
        ]
        pairs = build_lesson_sources(lessons, segments, mappings)
        self.assertEqual(len(pairs), 1)  # solo la de texto con fuente
        self.assertEqual(pairs[0][1], "texto fuente")


class CoursePlanningTests(SimpleTestCase):
    def test_resolve_max_lecciones_auto_scales_to_book(self):
        from content_pipeline.services.course_planning import (
            AUTO_FLOOR, HARD_CEILING, resolve_max_lecciones,
        )
        # Sin valor del operador => se dimensiona al nº de segmentos.
        self.assertEqual(resolve_max_lecciones(45, None), (45, "auto"))
        # Libro chico => piso.
        self.assertEqual(resolve_max_lecciones(3, None), (AUTO_FLOOR, "auto"))
        # Libro enorme => techo duro.
        self.assertEqual(resolve_max_lecciones(500, None), (HARD_CEILING, "auto"))
        # Operador manda => techo (acotado).
        self.assertEqual(resolve_max_lecciones(500, 30), (30, "operador"))
        self.assertEqual(resolve_max_lecciones(500, 999), (HARD_CEILING, "operador"))

    def test_truncation_notes_fire_when_book_exceeds_cap(self):
        from content_pipeline.services.course_planning import truncation_notes
        # Libro (150 seg) > cap (100) => avisa recorte.
        notes = truncation_notes(150, 100, "auto")
        self.assertTrue(any("no quedar cubierta" in n for n in notes))
        # Libro que cabe => sin alertas.
        self.assertEqual(truncation_notes(40, 60, "auto"), [])

    def test_validate_topics_present_flags_missing_temario_topics(self):
        from content_pipeline.services.course_planning import validate_topics_present
        manifest = {
            "unidades": [
                {"temas": ["Distancia de frenado y velocidad segura", "Señales reglamentarias"]},
                {"temas": ["Uso del cinturón de seguridad"]},
            ]
        }
        expected = [
            "Distancia de frenado",              # presente (match con tema generado)
            "Señales reglamentarias de tránsito",  # presente
            "Transporte internacional de carga peligrosa",  # ausente del libro
        ]
        res = validate_topics_present(expected, manifest)
        self.assertEqual(res["expected"], 3)
        self.assertIn("Transporte internacional de carga peligrosa", res["missing"])
        self.assertIn("Distancia de frenado", res["present"])

    def test_validate_topics_present_all_missing_when_no_generated(self):
        from content_pipeline.services.course_planning import validate_topics_present
        res = validate_topics_present(["A", "B"], {"unidades": []})
        self.assertEqual(res["missing"], ["A", "B"])
        self.assertEqual(res["present"], [])


class GenerateCourseStreamTests(SimpleTestCase):
    def _run(self, *, n_segments, temario_topics, generated_temas, max_lecciones):
        from unittest.mock import patch

        import content_pipeline.services.course_generator as cg

        pages = [{"page": 1, "text": "x" * 80, "char_count": 80, "has_text": True}]
        segments = [
            {"segment_id": f"seg_{i}", "title": f"T {i}", "keywords": [f"k{i}"],
             "text": "y" * 60, "page_start": i, "page_end": i}
            for i in range(1, n_segments + 1)
        ]
        manifest = {
            "curso": {"nombre": "C", "codigo": "X", "descripcion": "d", "costo": 0},
            "unidades": [{"orden": 1, "nombre": "U1", "categoria": "General",
                          "horas_elearning": 1, "temas": list(generated_temas)}],
        }
        lessons = [{"nombre": "L1", "tipo": "texto", "posicion": 1,
                    "unidad_orden": 1, "categoria": "General", "contenido": "c", "fuentes": []}]
        curso = type("Curso", (), {"id": 7, "nombre": "C", "codigo": "X"})()

        with patch.object(cg.LLMClient, "is_available", return_value=False), \
             patch.object(cg, "extract_pdf_pages", return_value=pages), \
             patch.object(cg, "segment_pages", return_value=segments), \
             patch.object(cg, "build_manifest_from_content", return_value=manifest), \
             patch.object(cg, "map_topics_to_segments", return_value=[]), \
             patch.object(cg, "extract_temario_topics", return_value=temario_topics), \
             patch.object(cg, "generate_lessons_generic", return_value=lessons), \
             patch.object(cg, "import_generated_course", return_value=(None, curso)):
            return list(cg.generate_course_stream(
                temario_path="t.pdf", contenido_path="c.pdf",
                nombre="C", codigo="X", costo=5000, max_lecciones=max_lecciones,
            ))

    def test_structure_from_content_and_temario_validation(self):
        events = self._run(
            n_segments=6,
            temario_topics=["Concepto A", "Tema Ausente XYZ"],
            generated_temas=["Concepto A", "Concepto B"],
            max_lecciones=None,
        )
        steps = {e.get("step") for e in events if e["event"] == "step"}
        self.assertIn("estructura_ok", steps)   # estructura desde el libro
        self.assertIn("dimension", steps)        # dimensionado al libro
        self.assertIn("temario_ok", steps)       # temario validado (no estructura)

        # El tema del temario ausente del curso dispara un warn.
        faltante = [e for e in events if e["event"] == "warn" and e.get("step") == "temario_faltante"]
        self.assertEqual(len(faltante), 1)
        self.assertIn("Tema Ausente XYZ", faltante[0]["temas_faltantes"])

        # El curso se persiste y termina en 'done'.
        self.assertTrue(any(e["event"] == "done" for e in events))

    def test_truncation_warning_when_book_exceeds_ceiling(self):
        events = self._run(
            n_segments=150,  # > HARD_CEILING (100)
            temario_topics=[],
            generated_temas=["A"],
            max_lecciones=None,
        )
        trunc = [e for e in events if e["event"] == "warn" and e.get("step") == "truncamiento"]
        self.assertTrue(trunc)
        self.assertIn("no quedar cubierta", trunc[0]["message"])


class CoverageAlertTests(SimpleTestCase):
    def _mapping(self, orden, tema, matched):
        return {"unidad_orden": orden, "unidad_nombre": f"U{orden}", "tema": tema, "matched_segments": matched}

    def test_classifies_solid_weak_and_uncovered(self):
        mappings = [
            self._mapping(1, "Sólido", [{"segment_id": "s1", "score": 0.5}]),
            self._mapping(1, "Débil", [{"segment_id": "s2", "score": 0.1, "below_min_score": True}]),
            self._mapping(2, "Sin fuente", []),
            # Mezcla: un match sólido + uno bajo umbral => cuenta como sólido.
            self._mapping(2, "Mixto", [
                {"segment_id": "s3", "score": 0.4},
                {"segment_id": "s4", "score": 0.1, "below_min_score": True},
            ]),
        ]
        alert = coverage_alert(mappings)
        self.assertEqual(alert["total"], 4)
        self.assertEqual(alert["solid"], ["U1 · Sólido", "U2 · Mixto"])
        self.assertEqual(alert["weak"], ["U1 · Débil"])
        self.assertEqual(alert["uncovered"], ["U2 · Sin fuente"])

    def test_all_solid_leaves_nothing_flagged(self):
        mappings = [self._mapping(1, "A", [{"segment_id": "s1", "score": 0.6}])]
        alert = coverage_alert(mappings)
        self.assertEqual(alert["weak"], [])
        self.assertEqual(alert["uncovered"], [])


class ProvenanceMappingTests(SimpleTestCase):
    def _segments(self):
        return [
            {"segment_id": f"seg_000{i}", "title": f"T{i}", "keywords": [f"k{i}"],
             "text": f"texto {i}", "page_start": i * 10, "page_end": i * 10 + 2}
            for i in range(1, 4)
        ]

    def test_provenance_used_over_lexical(self):
        manifest = {"unidades": [{"orden": 1, "nombre": "U1", "temas": ["Tema X"]}]}
        provenance = [{"unidad_orden": 1, "tema": "Tema X", "segment_ids": ["seg_0002"]}]
        mappings = map_topics_to_segments(manifest, self._segments(), provenance=provenance)
        ms = mappings[0]["matched_segments"]
        self.assertEqual(len(ms), 1)
        self.assertEqual(ms[0]["segment_id"], "seg_0002")
        self.assertEqual(ms[0]["score"], 1.0)
        self.assertTrue(ms[0]["reason"].startswith("Procedencia"))
        self.assertEqual(ms[0]["page_start"], 20)

    def test_invalid_provenance_ids_fall_back_to_lexical(self):
        manifest = {"unidades": [{"orden": 1, "nombre": "U1", "temas": ["texto 1"]}]}
        # IDs inexistentes => se ignoran => cae al mapeo lexical (que igual encuentra seg_0001).
        provenance = [{"unidad_orden": 1, "tema": "texto 1", "segment_ids": ["seg_9999"]}]
        mappings = map_topics_to_segments(manifest, self._segments(), provenance=provenance)
        ms = mappings[0]["matched_segments"]
        self.assertTrue(ms)  # hubo fallback lexical
        self.assertFalse(str(ms[0].get("reason", "")).startswith("Procedencia"))

    def test_no_provenance_is_pure_lexical(self):
        manifest = {"unidades": [{"orden": 1, "nombre": "U1", "temas": ["texto 2"]}]}
        mappings = map_topics_to_segments(manifest, self._segments())
        self.assertTrue(mappings[0]["matched_segments"])


class LLMMapperTests(SimpleTestCase):
    def test_map_topics_llm_builds_provenance_and_drops_invalid(self):
        import json as _json
        from content_pipeline.processors.llm_mapper import map_topics_llm

        class _Stub:
            def complete(self, **kw):
                # tema1 -> id válido; tema2 -> id inexistente; tema3 -> vacío
                return _json.dumps({"1": ["seg_0002"], "2": ["seg_9999"], "3": []})

        manifest = {"unidades": [{"orden": 1, "nombre": "U1",
                                  "temas": ["Frenos", "Motor", "Suelto"]}]}
        segments = [{"segment_id": f"seg_000{i}", "title": f"T{i}", "keywords": [],
                     "text": "x", "page_start": i, "page_end": i} for i in range(1, 4)]
        prov = map_topics_llm(manifest, segments, client=_Stub(), model="fake")
        self.assertEqual(len(prov), 1)
        self.assertEqual(prov[0]["tema"], "Frenos")
        self.assertEqual(prov[0]["segment_ids"], ["seg_0002"])


class TemarioContentCoverageTests(SimpleTestCase):
    def test_umbrella_topic_present_via_content_corpus(self):
        from content_pipeline.services.course_planning import validate_topics_present
        # El curso no tiene un tema llamado "Primeros auxilios", pero su contenido sí.
        manifest = {"unidades": [{"temas": ["Reanimación cardiopulmonar", "Manejo de hemorragias"]}]}
        expected = ["Primeros auxilios", "Transporte de explosivos"]
        corpus = "primeros auxilios reanimación hemorragias shock víctima accidente atención herido"
        res = validate_topics_present(expected, manifest, corpus_text=corpus)
        self.assertIn("Primeros auxilios", res["present"])       # cubierto por el corpus
        self.assertIn("Transporte de explosivos", res["missing"])  # no está


class WeakMappingDegradeTests(SimpleTestCase):
    def test_mapping_is_weak(self):
        from content_pipeline.processors.llm_lesson_writer import _mapping_is_weak
        self.assertTrue(_mapping_is_weak(None))
        self.assertTrue(_mapping_is_weak({"matched_segments": []}))
        self.assertTrue(_mapping_is_weak({"matched_segments": [{"segment_id": "s1", "below_min_score": True}]}))
        self.assertFalse(_mapping_is_weak({"matched_segments": [{"segment_id": "s1"}]}))
        self.assertFalse(_mapping_is_weak({"matched_segments": [
            {"segment_id": "s1", "below_min_score": True}, {"segment_id": "s2"}]}))

    def test_weak_mapping_degrades_without_calling_llm(self):
        from content_pipeline.processors.llm_lesson_writer import generate_lessons_llm

        class _BoomClient:
            """Si el writer llama al LLM en una fuente débil, el test falla."""
            meter = None
            def complete_meta(self, **kw): raise AssertionError("no debe llamar al LLM con fuente débil")
            def complete(self, **kw): raise AssertionError("no debe llamar al LLM con fuente débil")

        manifest = {
            "curso": {"nombre": "C", "codigo": "X"},
            "unidades": [{"orden": 1, "nombre": "U1", "categoria": "General",
                          "horas_elearning": 1, "temas": ["Tema sin fuente"]}],
        }
        segments = [{"segment_id": "s1", "title": "otro", "keywords": ["x"],
                     "text": "texto irrelevante", "page_start": 5, "page_end": 5,
                     "blocks": [{"page": 5, "text": "texto irrelevante"}]}]
        # Match forzado bajo umbral => fuente débil.
        mappings = [{"unidad_orden": 1, "tema": "Tema sin fuente",
                     "matched_segments": [{"segment_id": "s1", "score": 0.05, "below_min_score": True}]}]

        lessons = list(generate_lessons_llm(manifest, segments, mappings, client=_BoomClient(), model="fake"))
        texto = [l for l in lessons if l["tipo"] == "texto"]
        self.assertEqual(len(texto), 1)
        self.assertTrue(texto[0]["fuente_debil"])           # marcada para revisión
        self.assertIn("## Objetivo", texto[0]["contenido"])  # extractivo neutral, con estructura


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

