"""Convierte el contenido de una lección (Markdown) en un guion para locución.

El `contenido` está pensado para leerse (encabezados, viñetas, tablas). Para
escuchar suena mal leído literal. Aquí Claude lo reescribe como prosa hablada
natural en español; sin API key, hay un fallback que solo limpia el Markdown.

El guion se audita contra la lección (`audit_script`): cifras nuevas, truncado y
largo relativo. Es REPORTE (como `faithfulness`): no bloquea, sirve para revisar
los guiones antes de pagar el TTS.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from content_pipeline.llm.client import LLMClient, draft_model
from content_pipeline.processors.faithfulness import (
    _ANY_NUM_RE,
    _num_core,
    _source_number_cores,
)

NARRATION_SYSTEM = """\
Eres un locutor y guionista de audiocursos en español (Chile). Recibes el
contenido de una lección en Markdown y lo reescribes como GUION para locución.

Reglas:
- Texto corrido, natural y claro para ESCUCHAR (no para leer).
- Dirígete al estudiante en segunda persona (tú).
- Sin encabezados de Markdown, sin viñetas, sin asteriscos, sin decir "título" ni "sección".
- Expande abreviaturas y símbolos a su forma hablada: "km/h" → "kilómetros por hora",
  "%" → "por ciento", "art." → "artículo", "N°" → "número".
- Mantén las cifras en DÍGITOS tal como aparecen (ej. "60 kilómetros por hora",
  "Ley 18.290"); la voz sintética las lee correctamente.
- Cubre TODO el contenido de la lección, sin resumir ni omitir ideas.
- NO agregues información nueva ni inventes datos: solo reformula lo que ya está.
- Devuelve ÚNICAMENTE el guion, sin comentarios ni marcas.
"""

NARRATION_USER = """\
Lección: {nombre}

Contenido (Markdown):
---
{contenido}
---
Devuelve el guion locutado.
"""

# Tokens de salida: una lección ronda 7-8k chars (~2.5k tokens). Si se trunca,
# se reintenta una vez con el tope mayor.
MAX_TOKENS = 4000
MAX_TOKENS_RETRY = 8000

# Guion mucho más corto que la lección ⇒ probable omisión de contenido.
RATIO_MIN = 0.5


@dataclass
class NarrationResult:
    script: str
    truncado: bool = False


def strip_markdown(text: str) -> str:
    """Fallback sin IA: quita la sintaxis Markdown para que se pueda locutar."""
    text = re.sub(r"`{1,3}[^`]*`{1,3}", "", text)          # code spans/blocks
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)  # headings
    text = re.sub(r"^\s{0,3}[-*+]\s+", "", text, flags=re.MULTILINE)   # bullets
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)          # bold/italic
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)               # links → texto
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def build_narration_script_meta(
    contenido: str,
    *,
    nombre: str,
    client: LLMClient | None = None,
    model: str | None = None,
) -> NarrationResult:
    """Guion hablado con Claude (con detección de truncamiento); sin IA, limpia el Markdown."""
    contenido = (contenido or "").strip()
    if not contenido:
        return NarrationResult("")
    if client is None and not LLMClient.is_available():
        return NarrationResult(strip_markdown(contenido))
    client = client or LLMClient()
    user = NARRATION_USER.format(nombre=nombre, contenido=contenido)
    resp = None
    for max_tokens in (MAX_TOKENS, MAX_TOKENS_RETRY):
        resp = client.complete_meta(
            system=NARRATION_SYSTEM,
            user=user,
            max_tokens=max_tokens,
            model=model or draft_model(),
            temperature=0.3,
        )
        if not resp.truncated:
            break
    return NarrationResult(resp.text.strip(), truncado=resp.truncated)


def build_narration_script(
    contenido: str,
    *,
    nombre: str,
    client: LLMClient | None = None,
    model: str | None = None,
) -> str:
    """Solo el texto del guion. Ver `build_narration_script_meta`."""
    return build_narration_script_meta(contenido, nombre=nombre, client=client, model=model).script


def audit_script(script: str, contenido: str, *, truncado: bool = False) -> dict:
    """Audita el guion contra la lección de la que sale (reporte, no bloquea).

    - ``cifras_nuevas``: números del guion que NO están en el contenido (posible
      dato inventado al reformular). Ignora el 0 y números de un dígito, demasiado
      comunes en prosa ("2 tipos", "1 vez") para ser señal fiable.
    - ``truncado``: el LLM cortó por ``max_tokens`` aun tras reintentar.
    - ``ratio_largo``: largo del guion / largo de la lección sin Markdown.
    """
    src = _source_number_cores(contenido or "")
    nuevas: list[str] = []
    seen: set[str] = set()
    for token in _ANY_NUM_RE.findall(script or ""):
        token = token.rstrip(".,")
        core = _num_core(token)
        if len(core) < 2 or core in src or core in seen:
            continue
        seen.add(core)
        nuevas.append(token)
    base = len(strip_markdown(contenido or "")) or 1
    ratio = round(len(script or "") / base, 2)
    return {
        "cifras_nuevas": nuevas,
        "truncado": bool(truncado),
        "ratio_largo": ratio,
        "corto": ratio < RATIO_MIN,
    }


def has_findings(audit: dict | None) -> bool:
    """True si la auditoría del guion tiene algo que revisar."""
    if not audit:
        return False
    return bool(audit.get("cifras_nuevas") or audit.get("truncado") or audit.get("corto"))
