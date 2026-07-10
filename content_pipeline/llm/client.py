"""Cliente LLM (Anthropic) para el generador de cursos.

Envuelve el SDK `anthropic` con:
  - configuración desde Django settings / entorno (.env),
  - medidor de costo por tokens (input/output + prompt caching),
  - reintentos con backoff,
  - detección de disponibilidad (sin API key o sin SDK => no disponible, el
    generador cae al pipeline extractivo).

El SDK se importa de forma perezosa: si `anthropic` no está instalado, el
módulo igual carga y `is_available()` devuelve False.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

# Precios de lista Anthropic en USD por millón de tokens. Ajustar si cambian.
# cache_read = 0.10x del input; cache_write = 1.25x del input.
PRICING = {
    "opus": {"in": 15.0, "out": 75.0},
    "sonnet": {"in": 3.0, "out": 15.0},
    "haiku": {"in": 1.0, "out": 5.0},
}
_DEFAULT_FAMILY = "sonnet"


def _family(model: str) -> str:
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "haiku" in m:
        return "haiku"
    if "sonnet" in m:
        return "sonnet"
    return _DEFAULT_FAMILY


def _settings():
    try:
        from django.conf import settings
        return settings
    except Exception:  # pragma: no cover - Django siempre presente en runtime
        return None


def _conf(name: str, default):
    import os
    settings = _settings()
    if settings is not None and hasattr(settings, name):
        return getattr(settings, name)
    return os.environ.get(name, default)


def default_model() -> str:
    return _conf("COURSE_LLM_MODEL", "claude-sonnet-5")


def draft_model() -> str:
    return _conf("COURSE_LLM_MODEL_DRAFT", "claude-haiku-4-5-20251001")


@dataclass
class CostMeter:
    """Acumula uso y costo a lo largo de múltiples llamadas (posibles modelos
    distintos: p. ej. Sonnet para el temario, Haiku para las lecciones)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    calls: int = 0
    cost_usd: float = 0.0
    models: set[str] = field(default_factory=set)

    def record(self, usage, model: str) -> None:
        price = PRICING[_family(model)]
        it = int(getattr(usage, "input_tokens", 0) or 0)
        ot = int(getattr(usage, "output_tokens", 0) or 0)
        cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cw = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        self.input_tokens += it
        self.output_tokens += ot
        self.cache_read_tokens += cr
        self.cache_creation_tokens += cw
        self.calls += 1
        self.models.add(model)
        # input_tokens del SDK ya excluye lo cacheado; se cobra cada bucket aparte.
        self.cost_usd += (
            it * price["in"]
            + cr * price["in"] * 0.10
            + cw * price["in"] * 1.25
            + ot * price["out"]
        ) / 1_000_000

    def as_dict(self) -> dict:
        return {
            "modelos": sorted(self.models),
            "llamadas": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "costo_usd": round(self.cost_usd, 4),
        }


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, model: str | None = None):
        self.model = model or default_model()
        self._client = None
        self.meter = CostMeter()

    # -- disponibilidad --------------------------------------------------
    @staticmethod
    def is_available() -> bool:
        import os
        settings = _settings()
        enabled = True
        if settings is not None and hasattr(settings, "COURSE_LLM_ENABLED"):
            enabled = bool(settings.COURSE_LLM_ENABLED)
        if not enabled:
            return False
        key = _conf("ANTHROPIC_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            return False
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return True

    def _ensure(self):
        if self._client is None:
            import os
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise LLMError("El paquete 'anthropic' no está instalado.") from exc
            api_key = _conf("ANTHROPIC_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                raise LLMError("Falta ANTHROPIC_API_KEY.")
            self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    # -- llamada ---------------------------------------------------------
    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        model: str | None = None,
        temperature: float = 0.4,
        cache_system: bool = True,
        retries: int = 2,
    ) -> str:
        client = self._ensure()
        mdl = model or self.model
        system_param = (
            [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            if cache_system
            else system
        )
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = client.messages.create(
                    model=mdl,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system_param,
                    messages=[{"role": "user", "content": user}],
                )
                self.meter.record(resp.usage, mdl)
                return "".join(
                    block.text for block in resp.content if getattr(block, "type", None) == "text"
                ).strip()
            except Exception as exc:  # reintenta ante rate limit / red
                last_err = exc
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 8))
        raise LLMError(f"Fallo la llamada al LLM tras {retries + 1} intentos: {last_err}") from last_err


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_object(raw: str) -> dict:
    """Extrae un objeto JSON de la respuesta del LLM (tolera fences y prosa)."""
    if not raw or not raw.strip():
        raise LLMError("Respuesta vacía del LLM.")
    text = raw.strip()
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise LLMError("La respuesta del LLM no contiene un objeto JSON.")
    snippet = text[start : end + 1]
    try:
        return json.loads(snippet)
    except json.JSONDecodeError as exc:
        raise LLMError(f"JSON inválido del LLM: {exc}") from exc
