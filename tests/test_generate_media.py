"""Tests del comando `generate_media`, del guion auditado, de TTS y del storage.

El proveedor TTS, el guion con IA y el bucket se mockean: se valida el cableado
(qué lecciones se locutan, fases, guardado incremental, escritura de
url_audio/transcripcion/audio_meta), no la llamada real a ElevenLabs/OpenAI/Claude/S3.
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

from content_pipeline.llm.client import LLMResponse
from content_pipeline.media.narration import (
    NarrationResult,
    audit_script,
    build_narration_script_meta,
    has_findings,
)
from content_pipeline.media.storage import S3Storage
from content_pipeline.media.tts import OpenAITTS, TTSError, _chunk_text

MOD = "schools.management.commands.generate_media."


class _StubTTS:
    name = "stub"
    voice_id = "v1"
    max_chars = 2000

    def __init__(self, fail_on: int | None = None):
        self.calls = 0
        self.fail_on = fail_on

    def synthesize(self, text: str) -> bytes:
        self.calls += 1
        if self.fail_on and self.calls == self.fail_on:
            raise TTSError("boom")
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
            {"nombre": "Lección Dos", "tipo": "texto", "posicion": 3,
             "contenido": "# Dos\n\nLímite de 60 km/h en zona urbana.", "transcripcion": ""},
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

    def _data(self):
        return json.loads(self.file.read_text(encoding="utf-8"))

    def _run(self, tts=None, guion="Guion de prueba locutado.", **extra):
        tts = tts or _StubTTS()
        with patch("content_pipeline.llm.client.LLMClient.is_available", lambda: False), \
             patch(MOD + "get_tts_provider", return_value=tts), \
             patch(MOD + "build_narration_script_meta",
                   return_value=NarrationResult(guion)) as narr:
            call_command(
                "generate_media",
                file=str(self.file),
                media_dir=str(self.media),
                base_url="http://test/media/course_audio/",
                stdout=StringIO(),
                stderr=StringIO(),
                **extra,
            )
        return tts, narr

    def test_locuta_solo_lecciones_texto(self):
        self._run()
        uno, quiz, dos = self._data()["lessons"]

        for texto in (uno, dos):
            self.assertTrue(texto["url_audio"].startswith("http://test/media/course_audio/"))
            self.assertTrue(texto["url_audio"].endswith(".mp3"))
            self.assertEqual(texto["transcripcion"], "Guion de prueba locutado.")
            self.assertEqual(texto["audio_meta"]["provider"], "stub")

        # El quiz no se locuta.
        self.assertNotIn("url_audio", quiz)
        self.assertEqual(quiz["transcripcion"], "")
        self.assertEqual(len(list(self.media.glob("*.mp3"))), 2)

    def test_provider_no_configurado_falla(self):
        with patch(MOD + "get_tts_provider", side_effect=TTSError("Falta ELEVENLABS_API_KEY.")):
            with self.assertRaises(CommandError):
                call_command(
                    "generate_media", file=str(self.file), media_dir=str(self.media),
                    stdout=StringIO(),
                )

    def test_no_regenera_si_existe_y_conserva_guion(self):
        self._run()
        primero = next(self.media.glob("*.mp3"))
        mtime = primero.stat().st_mtime_ns
        # Segunda corrida sin --overwrite: no reescribe el audio ni rehace el guion.
        tts, narr = self._run()
        self.assertEqual(primero.stat().st_mtime_ns, mtime)
        self.assertEqual(tts.calls, 0)
        narr.assert_not_called()
        self.assertEqual(self._data()["lessons"][0]["transcripcion"], "Guion de prueba locutado.")

    def test_solo_guion_no_llama_tts(self):
        with patch(MOD + "get_tts_provider") as prov:
            with patch("content_pipeline.llm.client.LLMClient.is_available", lambda: False), \
                 patch(MOD + "build_narration_script_meta",
                       return_value=NarrationResult("Guion de prueba locutado.")):
                call_command("generate_media", file=str(self.file), solo_guion=True,
                             stdout=StringIO(), stderr=StringIO())
            prov.assert_not_called()
        data = self._data()
        uno = data["lessons"][0]
        self.assertEqual(uno["transcripcion"], "Guion de prueba locutado.")
        self.assertIn("cifras_nuevas", uno["audio_meta"])
        self.assertNotIn("url_audio", uno)
        self.assertEqual(data["auditoria_audio"]["guiones"], 2)

    def test_solo_audio_no_redacta_y_omite_sin_guion(self):
        data = self._data()
        data["lessons"][0]["transcripcion"] = "Guion revisado a mano."
        self.file.write_text(json.dumps(data), encoding="utf-8")

        tts, narr = self._run(solo_audio=True)
        narr.assert_not_called()
        self.assertEqual(tts.calls, 1)
        uno, _, dos = self._data()["lessons"]
        self.assertTrue(uno["url_audio"].endswith(".mp3"))
        self.assertEqual(uno["transcripcion"], "Guion revisado a mano.")
        self.assertNotIn("url_audio", dos)

    def test_estricto_omite_guion_con_hallazgos(self):
        # "99" no está en el contenido → cifra nueva → --estricto no sintetiza.
        tts, _ = self._run(guion="Son 99 kilómetros por hora.", estricto=True)
        self.assertEqual(tts.calls, 0)
        self.assertEqual(self._data()["lessons"][0]["audio_meta"]["cifras_nuevas"], ["99"])

    def test_guardado_incremental_ante_fallo(self):
        with self.assertRaises(CommandError):
            self._run(tts=_StubTTS(fail_on=2))
        uno, _, dos = self._data()["lessons"]
        self.assertTrue(uno["url_audio"].endswith(".mp3"))
        # El guion de la lección que falló quedó guardado para retomar.
        self.assertEqual(dos["transcripcion"], "Guion de prueba locutado.")
        self.assertNotIn("url_audio", dos)


class AuditScriptTests(TestCase):
    def test_detecta_cifra_nueva_e_ignora_presentes(self):
        contenido = "Límite de **60 km/h**; Ley 18.290."
        audit = audit_script("El límite es 60 kilómetros por hora según la Ley 18290, multa 45.", contenido)
        self.assertEqual(audit["cifras_nuevas"], ["45"])
        self.assertTrue(has_findings(audit))

    def test_sin_hallazgos(self):
        contenido = "Frena con 2 segundos de distancia."
        audit = audit_script("Frena con 2 segundos de distancia, siempre.", contenido)
        self.assertEqual(audit["cifras_nuevas"], [])
        self.assertFalse(has_findings(audit))

    def test_guion_corto_se_marca(self):
        audit = audit_script("Breve.", "Texto largo " * 50)
        self.assertTrue(audit["corto"])

    def test_truncado_reintenta_con_mas_tokens(self):
        class _Client:
            def __init__(self):
                self.max_tokens = []

            def complete_meta(self, **kw):
                self.max_tokens.append(kw["max_tokens"])
                stop = "max_tokens" if len(self.max_tokens) == 1 else "end_turn"
                return LLMResponse(text="guion", stop_reason=stop)

        client = _Client()
        res = build_narration_script_meta("# Hola\n\nContenido.", nombre="X", client=client)
        self.assertEqual(client.max_tokens, [4000, 8000])
        self.assertFalse(res.truncado)


class OpenAITTSTests(TestCase):
    def test_trocea_y_envia_payload(self):
        calls = []

        class _Resp:
            status_code = 200
            content = b"MP3"

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append((url, json, headers))
            return _Resp()

        tts = OpenAITTS(api_key="k", voice_id="coral", model="gpt-4o-mini-tts")
        text = " ".join(f"Oración número {i}." for i in range(600))
        with patch("requests.post", fake_post):
            audio = tts.synthesize(text)
        self.assertGreater(len(calls), 1)
        self.assertTrue(all(len(c[1]["input"]) <= 4000 for c in calls))
        self.assertEqual(calls[0][1]["voice"], "coral")
        self.assertIn("instructions", calls[0][1])
        self.assertEqual(calls[0][2]["Authorization"], "Bearer k")
        self.assertEqual(audio, b"MP3" * len(calls))

    def test_sin_key_falla(self):
        with patch("content_pipeline.media.tts._conf", return_value=""):
            with self.assertRaises(TTSError):
                OpenAITTS()


class S3StorageTests(TestCase):
    def _storage(self, client):
        return S3Storage(client=client, bucket="b", public_base_url="https://cdn.test",
                         prefix="course_audio/")

    def test_save_sube_y_devuelve_url_publica(self):
        class _Client:
            def put_object(self, **kw):
                self.kw = kw

        client = _Client()
        url = self._storage(client).save("a4_01_x.mp3", b"ID3")
        self.assertEqual(url, "https://cdn.test/course_audio/a4_01_x.mp3")
        self.assertEqual(client.kw["Key"], "course_audio/a4_01_x.mp3")
        self.assertEqual(client.kw["ContentType"], "audio/mpeg")

    def test_exists_404_es_false(self):
        class _NotFound(Exception):
            response = {"Error": {"Code": "404"}}

        class _Client:
            def head_object(self, **kw):
                raise _NotFound()

        self.assertFalse(self._storage(_Client()).exists("x.mp3"))
