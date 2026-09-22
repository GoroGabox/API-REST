"""Tests de la inferencia de estructura desde el contenido (curso sin temario)."""
from __future__ import annotations

import json

from django.test import TestCase

from content_pipeline.llm.client import LLMError
from content_pipeline.processors.manifest_from_content import (
    build_manifest_from_content,
    build_manifest_from_content_llm,
)


def _segments(n=6):
    return [
        {
            "segment_id": f"seg_{i:04d}",
            "title": f"Tema: Concepto {i}",
            "keywords": [f"kw{i}a", f"kw{i}b"],
            "text": f"Texto del segmento {i}. " * 10,
            "page_start": i,
            "page_end": i,
        }
        for i in range(1, n + 1)
    ]


class _StubClient:
    """Cliente LLM falso: devuelve un JSON fijo sin tocar la red."""

    def __init__(self, payload: dict):
        self._raw = json.dumps(payload, ensure_ascii=False)

    def complete(self, **kwargs) -> str:
        return self._raw


class HeuristicContentManifestTests(TestCase):
    def test_reparte_en_n_unidades(self):
        manifest = build_manifest_from_content(
            _segments(6), nombre="Curso X", codigo="CX",
            is_profesional=False, max_lecciones=20, n_unidades=2,
        )
        self.assertEqual(manifest["curso"]["nombre"], "Curso X")
        self.assertEqual(manifest["curso"]["codigo"], "CX")
        self.assertEqual(len(manifest["unidades"]), 2)
        self.assertEqual([u["orden"] for u in manifest["unidades"]], [1, 2])
        # El prefijo "Tema:" del segmentador no debe filtrarse a los temas.
        for u in manifest["unidades"]:
            self.assertTrue(u["temas"])
            for t in u["temas"]:
                self.assertNotIn("Tema:", t)

    def test_respeta_tope_de_lecciones(self):
        # 12 segmentos, pero max 6 lecciones (temas + quizzes por unidad).
        manifest = build_manifest_from_content(
            _segments(12), nombre="Curso X", codigo="CX",
            is_profesional=False, max_lecciones=6, n_unidades=3,
        )
        total = sum(len(u["temas"]) for u in manifest["unidades"]) + len(manifest["unidades"])
        self.assertLessEqual(total, 6)

    def test_sin_segmentos_falla(self):
        with self.assertRaises(ValueError):
            build_manifest_from_content(
                [], nombre="X", codigo="X", is_profesional=False, max_lecciones=10,
            )


class LLMContentManifestTests(TestCase):
    def test_normaliza_salida_del_llm(self):
        payload = {
            "curso": {"descripcion": "Curso inferido de prueba"},
            "unidades": [
                {"orden": 1, "nombre": "U1", "categoria": "U1", "horas_elearning": 2,
                 "objetivos": ["obj"], "temas": ["Tema 1", "Tema 2"]},
                {"orden": 2, "nombre": "U2", "temas": ["Tema 3"]},
            ],
        }
        manifest = build_manifest_from_content_llm(
            _segments(5), nombre="Curso Y", codigo="CY",
            is_profesional=True, max_lecciones=20, client=_StubClient(payload),
        )
        self.assertEqual(manifest["curso"]["codigo"], "CY")
        self.assertTrue(manifest["curso"]["is_profesional"])
        self.assertEqual(manifest["curso"]["descripcion"], "Curso inferido de prueba")
        self.assertEqual(len(manifest["unidades"]), 2)
        self.assertEqual(manifest["unidades"][0]["temas"], ["Tema 1", "Tema 2"])

    def test_categoria_se_clasifica_en_taxonomia(self):
        # El path de contenido ahora clasifica cada unidad en la lista cerrada:
        # una variante conocida resuelve a su canónica; algo libre cae a "General".
        payload = {
            "curso": {"descripcion": "d"},
            "unidades": [
                {"orden": 1, "nombre": "Mecánica del auto",
                 "categoria": "Mecánica y mantención preventiva", "temas": ["T1"]},
                {"orden": 2, "nombre": "Cosas varias",
                 "categoria": "Título inventado del libro", "temas": ["T2"]},
            ],
        }
        manifest = build_manifest_from_content_llm(
            _segments(5), nombre="Y", codigo="CY", is_profesional=True,
            max_lecciones=20, client=_StubClient(payload),
        )
        cats = [u["categoria"] for u in manifest["unidades"]]
        self.assertEqual(cats[0], "Mecánica y Mantención del Vehículo")
        self.assertEqual(cats[1], "General")

    def test_llm_sin_unidades_falla(self):
        # Tras agotar los reintentos, se propaga como LLMError.
        with self.assertRaises(LLMError):
            build_manifest_from_content_llm(
                _segments(3), nombre="Y", codigo="Y", is_profesional=False,
                max_lecciones=10, client=_StubClient({"curso": {}, "unidades": []}),
            )
