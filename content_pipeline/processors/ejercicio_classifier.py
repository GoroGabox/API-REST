"""Clasifica ejercicios (texto) en una categoría temática usando el LLM.

Las preguntas del cuestionario no vienen etiquetadas. Este módulo asigna a cada
una una categoría de un **vocabulario controlado** (TAXONOMIA) — así el examen
por categoría tiene grupos coherentes y no se crean categorías infinitas.

Trabaja por lotes (un request por lote) con el modelo "draft" (Haiku) para
minimizar costo. Si el LLM no está disponible o falla un lote, cae a
`CATEGORIA_FALLBACK` para no bloquear el pipeline.
"""
from __future__ import annotations

from typing import Any

from content_pipeline.llm.client import LLMClient, draft_model, parse_json_object
from content_pipeline.taxonomy import CATEGORY_NAMES, FALLBACK, resolve

# Vocabulario controlado: la MISMA taxonomía canónica que las lecciones de curso
# (ver content_pipeline.taxonomy). Así lecciones y banco de exámenes comparten
# categorías y el filtro por categoría es consistente en todo el frontend.
TAXONOMIA = list(CATEGORY_NAMES)
CATEGORIA_FALLBACK = FALLBACK

_BATCH = 25

_SYSTEM = (
    "Eres un clasificador de preguntas de un examen teórico de conducción "
    "(licencia Clase B, Chile). Debes asignar a CADA pregunta exactamente UNA "
    "categoría de la siguiente lista cerrada (usa el texto EXACTO):\n"
    + "\n".join(f"- {c}" for c in TAXONOMIA)
    + "\n\nResponde SOLO un objeto JSON que mapea el número de pregunta (string) "
    "a la categoría elegida. Sin texto adicional. Ejemplo: "
    '{"1": "Mecánica y mantención", "2": "Señales de tránsito"}'
)


def _render_batch(lote: list[dict[str, Any]]) -> str:
    partes = []
    for e in lote:
        ops = " | ".join(f"{k}) {v}" for k, v in e["opciones"].items())
        partes.append(f'#{e["numero"]}: {e["pregunta"]}\n   Opciones: {ops}')
    return "Clasifica estas preguntas:\n\n" + "\n\n".join(partes)


def clasificar(
    ejercicios: list[dict[str, Any]],
    *,
    client: LLMClient | None = None,
) -> dict[int, str]:
    """Devuelve {numero: categoria} para los ejercicios dados.

    No muta los ejercicios. Usa fallback por lote ante fallos del LLM.
    """
    resultado: dict[int, str] = {}
    if not ejercicios:
        return resultado

    if not LLMClient.is_available():
        return {e["numero"]: CATEGORIA_FALLBACK for e in ejercicios}

    cli = client or LLMClient(model=draft_model())

    for inicio in range(0, len(ejercicios), _BATCH):
        lote = ejercicios[inicio:inicio + _BATCH]
        try:
            raw = cli.complete(
                system=_SYSTEM,
                user=_render_batch(lote),
                max_tokens=1500,
                temperature=0.0,
            )
            mapping = parse_json_object(raw)
        except Exception:
            mapping = {}

        for e in lote:
            cat = mapping.get(str(e["numero"])) or mapping.get(e["numero"])
            # resolve() normaliza variantes/tildes y manda a "General" lo que no
            # sea una etiqueta canónica válida.
            resultado[e["numero"]] = resolve(cat)

    return resultado
