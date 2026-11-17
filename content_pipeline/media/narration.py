"""Convierte el contenido de una lección (Markdown) en un guion para locución.

El `contenido` está pensado para leerse (encabezados, viñetas, tablas). Para
escuchar suena mal leído literal. Aquí Claude lo reescribe como prosa hablada
natural en español; sin API key, hay un fallback que solo limpia el Markdown.

El guion se audita contra la lección (`audit_script`): cifras nuevas, truncado y
largo relativo. Es REPORTE (como `faithfulness`): no bloquea, sirve para revisar
los guiones antes de pagar el TTS.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from content_pipeline.llm.client import LLMClient, draft_model
from content_pipeline.processors.figure_markers import strip_markers
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

# Lecciones largas se locutan por tramos (secciones "## " agrupadas): una sola
# llamada truncaba las lecciones de ~20k caracteres.
NARRATION_CONTINUACION = """\
Este contenido es la CONTINUACIÓN (parte {parte} de {total}) de una lección cuyo guion
ya comenzó: no saludes, no presentes la lección ni la resumas desde el inicio;
continúa con naturalidad desde este tramo.
"""
TRAMO_MAX_CHARS = 7000

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
    text = strip_markers(text)                              # {{figura:…}} no se locuta
    text = re.sub(r"`{1,3}[^`]*`{1,3}", "", text)          # code spans/blocks
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)  # headings
    text = re.sub(r"^\s{0,3}[-*+]\s+", "", text, flags=re.MULTILINE)   # bullets
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)          # bold/italic
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)               # links → texto
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_sections(contenido: str, max_chars: int = TRAMO_MAX_CHARS) -> list[str]:
    """Agrupa las secciones ``## `` del Markdown en tramos de hasta ``max_chars``.

    Una sección que sola excede el tope queda como su propio tramo (se parte por
    párrafos si hace falta). Texto corto → un solo tramo.
    """
    contenido = (contenido or "").strip()
    if len(contenido) <= max_chars:
        return [contenido] if contenido else []
    partes = re.split(r"(?m)^(?=##\s)", contenido)
    piezas: list[str] = []
    for parte in partes:
        if len(parte) <= max_chars:
            piezas.append(parte)
            continue
        actual = ""
        for parrafo in re.split(r"\n{2,}", parte):
            if actual and len(actual) + len(parrafo) + 2 > max_chars:
                piezas.append(actual)
                actual = parrafo
            else:
                actual = f"{actual}\n\n{parrafo}" if actual else parrafo
        if actual:
            piezas.append(actual)
    tramos: list[str] = []
    for pieza in piezas:
        pieza = pieza.strip()
        if not pieza:
            continue
        if tramos and len(tramos[-1]) + len(pieza) + 2 <= max_chars:
            tramos[-1] = f"{tramos[-1]}\n\n{pieza}"
        else:
            tramos.append(pieza)
    return tramos


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
    tramos = split_sections(strip_markers(contenido))
    textos: list[str] = []
    truncado = False
    for k, tramo in enumerate(tramos, start=1):
        user = NARRATION_USER.format(nombre=nombre, contenido=tramo)
        if k > 1:
            user = NARRATION_CONTINUACION.format(parte=k, total=len(tramos)) + "\n" + user
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
        textos.append(resp.text.strip())
        truncado = truncado or resp.truncated
    return NarrationResult("\n\n".join(t for t in textos if t), truncado=truncado)


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


def content_hash(contenido) -> str:
    """Huella del texto locutable de la lección: si cambia, el guion quedó obsoleto."""
    texto = contenido if isinstance(contenido, str) else ""
    return hashlib.sha1(strip_markers(texto).strip().encode("utf-8")).hexdigest()[:16]


def has_findings(audit: dict | None) -> bool:
    """True si la auditoría del guion tiene algo que revisar."""
    if not audit:
        return False
    return bool(audit.get("cifras_nuevas") or audit.get("truncado") or audit.get("corto"))
