"""Text-to-Speech agnóstico al proveedor para los audio-cursos.

Igual que el email (SMTP/Resend por env), la síntesis de voz se elige por
configuración: ElevenLabs (mejor voz) u OpenAI (barato); mañana Azure/Polly sin tocar el resto del
pipeline. El generador de media solo depende de la interfaz `TTSProvider`.

Config (settings o entorno):
    TTS_PROVIDER          -> 'elevenlabs' (default) | 'openai'
    ELEVENLABS_API_KEY    -> API key
    ELEVENLABS_VOICE_ID   -> id de la voz
    ELEVENLABS_MODEL      -> 'eleven_multilingual_v2' (default)
    OPENAI_API_KEY        -> API key (proveedor barato, para iterar)
    OPENAI_TTS_MODEL      -> 'gpt-4o-mini-tts' (default)
    OPENAI_TTS_VOICE      -> 'coral' (default)
"""
from __future__ import annotations

import os
from typing import Protocol


class TTSError(RuntimeError):
    pass


def _conf(name: str, default=""):
    """Lee de Django settings y cae al entorno (como content_pipeline.llm.client)."""
    try:
        from django.conf import settings
        if hasattr(settings, name):
            return getattr(settings, name)
    except Exception:  # pragma: no cover - Django siempre presente en runtime
        pass
    return os.environ.get(name, default)


def _chunk_text(text: str, max_chars: int = 2000) -> list[str]:
    """Parte el texto en trozos <= max_chars respetando límites de oración.

    ElevenLabs (y la mayoría de TTS) tienen un tope por request; una lección
    completa se sintetiza por partes y luego se concatena.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    import re
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(current) + len(sentence) + 1 > max_chars and current:
            chunks.append(current.strip())
            current = ""
        # Oración sola más larga que el tope: trocear duro.
        while len(sentence) > max_chars:
            chunks.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        current = f"{current} {sentence}".strip()
    if current.strip():
        chunks.append(current.strip())
    return chunks


class TTSProvider(Protocol):
    name: str
    voice_id: str
    max_chars: int

    def synthesize(self, text: str) -> bytes: ...
    def extension(self) -> str: ...


# Reintentos ante rate limit / errores transitorios del proveedor.
_RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}
_RETRIES = 3


def _post_with_retry(url: str, *, headers: dict, payload: dict, label: str) -> bytes:
    import time

    import requests

    last = ""
    for attempt in range(_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=180)
        except requests.RequestException as exc:
            last = f"{label} red: {exc}"
        else:
            if resp.status_code == 200:
                return resp.content
            last = f"{label} {resp.status_code}: {resp.text[:300]}"
            if resp.status_code not in _RETRY_STATUS:
                break
        if attempt < _RETRIES:
            time.sleep(min(2 ** (attempt + 1), 20))
    raise TTSError(last)


class ElevenLabsTTS:
    """Proveedor ElevenLabs (HTTP API). Devuelve MP3. Mejor voz; más caro."""

    name = "elevenlabs"
    API_ROOT = "https://api.elevenlabs.io/v1/text-to-speech"
    max_chars = 2000

    def __init__(self, *, api_key: str | None = None, voice_id: str | None = None, model: str | None = None):
        self.api_key = api_key or _conf("ELEVENLABS_API_KEY")
        self.voice_id = voice_id or _conf("ELEVENLABS_VOICE_ID")
        self.model = model or _conf("ELEVENLABS_MODEL", "eleven_multilingual_v2")
        if not self.api_key:
            raise TTSError("Falta ELEVENLABS_API_KEY.")
        if not self.voice_id:
            raise TTSError("Falta ELEVENLABS_VOICE_ID (elige una voz en el panel de ElevenLabs).")

    def extension(self) -> str:
        return "mp3"

    def _synthesize_chunk(self, text: str, previous_text: str = "", next_text: str = "") -> bytes:
        payload = {
            "text": text,
            "model_id": self.model,
            "voice_settings": {"stability": 0.4, "similarity_boost": 0.75},
        }
        # Contexto vecino: mantiene la prosodia continua entre trozos.
        if previous_text:
            payload["previous_text"] = previous_text[-500:]
        if next_text:
            payload["next_text"] = next_text[:500]
        headers = {
            "xi-api-key": self.api_key,
            "accept": "audio/mpeg",
            "content-type": "application/json",
        }
        return _post_with_retry(f"{self.API_ROOT}/{self.voice_id}", headers=headers,
                                payload=payload, label="ElevenLabs")

    def synthesize(self, text: str) -> bytes:
        chunks = _chunk_text(text, self.max_chars)
        if not chunks:
            raise TTSError("Texto vacío para sintetizar.")
        parts = []
        for i, chunk in enumerate(chunks):
            prev = chunks[i - 1] if i > 0 else ""
            nxt = chunks[i + 1] if i + 1 < len(chunks) else ""
            parts.append(self._synthesize_chunk(chunk, prev, nxt))
        return b"".join(parts)


class OpenAITTS:
    """Proveedor OpenAI (`/v1/audio/speech`). Devuelve MP3. Barato; bueno para iterar.

    Config: OPENAI_API_KEY, OPENAI_TTS_MODEL (def. gpt-4o-mini-tts),
    OPENAI_TTS_VOICE (def. coral), OPENAI_TTS_INSTRUCTIONS (tono/acento).
    """

    name = "openai"
    API_URL = "https://api.openai.com/v1/audio/speech"
    max_chars = 4000  # tope de la API: 4096
    DEFAULT_INSTRUCTIONS = (
        "Habla en español de Chile, con tono claro, cercano y pausado de instructor "
        "de conducción. Pronuncia las cifras y unidades de forma natural."
    )

    def __init__(self, *, api_key: str | None = None, voice_id: str | None = None, model: str | None = None):
        self.api_key = api_key or _conf("OPENAI_API_KEY")
        self.voice_id = voice_id or _conf("OPENAI_TTS_VOICE", "coral") or "coral"
        self.model = model or _conf("OPENAI_TTS_MODEL", "gpt-4o-mini-tts") or "gpt-4o-mini-tts"
        self.instructions = _conf("OPENAI_TTS_INSTRUCTIONS", "") or self.DEFAULT_INSTRUCTIONS
        if not self.api_key:
            raise TTSError("Falta OPENAI_API_KEY.")

    def extension(self) -> str:
        return "mp3"

    def _synthesize_chunk(self, text: str) -> bytes:
        payload = {
            "model": self.model,
            "input": text,
            "voice": self.voice_id,
            "response_format": "mp3",
        }
        # `instructions` solo lo aceptan los modelos gpt-4o-*-tts (no tts-1).
        if self.model.startswith("gpt-"):
            payload["instructions"] = self.instructions
        headers = {"Authorization": f"Bearer {self.api_key}", "content-type": "application/json"}
        return _post_with_retry(self.API_URL, headers=headers, payload=payload, label="OpenAI TTS")

    def synthesize(self, text: str) -> bytes:
        chunks = _chunk_text(text, self.max_chars)
        if not chunks:
            raise TTSError("Texto vacío para sintetizar.")
        return b"".join(self._synthesize_chunk(c) for c in chunks)


_PROVIDERS = {
    "elevenlabs": ElevenLabsTTS,
    "openai": OpenAITTS,
}


def get_tts_provider(name: str | None = None) -> TTSProvider:
    name = (name or _conf("TTS_PROVIDER", "elevenlabs") or "elevenlabs").lower()
    factory = _PROVIDERS.get(name)
    if factory is None:
        raise TTSError(f"Proveedor TTS desconocido: {name}. Disponibles: {sorted(_PROVIDERS)}.")
    return factory()


def tts_available(name: str | None = None) -> bool:
    """True si el proveedor configurado tiene sus credenciales listas."""
    try:
        get_tts_provider(name)
        return True
    except TTSError:
        return False
