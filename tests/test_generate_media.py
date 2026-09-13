"""Tests del comando `generate_media` y del troceo de TTS.

El proveedor TTS y el guion con IA se mockean: se valida el cableado (qué
lecciones se locutan, escritura de archivos y de url_audio/transcripcion), no la
llamada real a ElevenLabs ni a Claude.
"""
from __future__ import annotations

import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from content_pipeline.media.tts import TTSError, _chunk_text


class _StubTTS:
    def synthesize(self, text: str) -> bytes:
        return b"ID3" + text[:8].encode("utf-8", "ignore")

    def extension(self) -> str:
        return "mp3"


def _course_json():
    return {
        "manifest": {"curso": {"nombre": "Curso Demo", "codigo": "DEMO"}, "unidades": []},
        "lessons": [
            {"nombre": "Lección Uno", "tipo": "texto", "posicion": 1,
             "contenido": "# Uno\n\nContenido de la lección uno.", "transcripcion": ""},
            {"nombre": "Quiz Uno", "tipo": "quiz", "posicion": 2,
             "contenido": {"questions": []}, "transcripcion": ""},
        ],
    }


class ChunkTextTests(TestCase):
    def test_texto_corto_un_chunk(self):
        self.assertEqual(_chunk_text("Hola mundo."), ["Hola mundo."])

    def test_trocea_por_oraciones_respetando_tope(self):
        text = " ".join(f"Oración número {i}." for i in range(200))
        chunks = _chunk_text(text, max_chars=100)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 100 for c in chunks))


class GenerateMediaCommandTests(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.file = self.tmp / "curso.json"
        self.file.write_text(json.dumps(_course_json()), encoding="utf-8")
        self.media = self.tmp / "audio"

    def _run(self, **extra):
        mod = "schools.management.commands.generate_media."
        with patch("content_pipeline.llm.client.LLMClient.is_available", lambda: False), \
             patch(mod + "get_tts_provider", return_value=_StubTTS()), \
             patch(mod + "build_narration_script", return_value="Guion de prueba locutado."):
            call_command(
                "generate_media",
                file=str(self.file),
                media_dir=str(self.media),
                base_url="http://test/media/course_audio/",
                stdout=StringIO(),
                **extra,
            )

    def test_locuta_solo_lecciones_texto(self):
        self._run()
        data = json.loads(self.file.read_text(encoding="utf-8"))
        texto, quiz = data["lessons"]

        self.assertTrue(texto["url_audio"].startswith("http://test/media/course_audio/"))
        self.assertTrue(texto["url_audio"].endswith(".mp3"))
        self.assertEqual(texto["transcripcion"], "Guion de prueba locutado.")

        # El quiz no se locuta.
        self.assertNotIn("url_audio", quiz)
        self.assertEqual(quiz["transcripcion"], "")

        # Se escribió exactamente 1 archivo de audio.
        self.assertEqual(len(list(self.media.glob("*.mp3"))), 1)

    def test_provider_no_configurado_falla(self):
        mod = "schools.management.commands.generate_media."
        with patch(mod + "get_tts_provider", side_effect=TTSError("Falta ELEVENLABS_API_KEY.")):
            with self.assertRaises(CommandError):
                call_command(
                    "generate_media", file=str(self.file), media_dir=str(self.media),
                    stdout=StringIO(),
                )

    def test_no_regenera_si_existe(self):
        self._run()
        primero = next(self.media.glob("*.mp3"))
        mtime = primero.stat().st_mtime_ns
        # Segunda corrida sin --overwrite: no reescribe el archivo.
        self._run()
        self.assertEqual(primero.stat().st_mtime_ns, mtime)
