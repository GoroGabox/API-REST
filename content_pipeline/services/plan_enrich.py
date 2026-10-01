"""Enriquecimiento del plan con IA (opcional): categorías + nombres de lección.

La estructura la decide `plan_structure` (IA por tema/densidad) o el corte por
palabras; acá el LLM solo hace trabajo LIGERO de rotulado, así que corre barato (Haiku):
  - **Categoría por unidad**: elige de la taxonomía CERRADA (misma que lecciones y
    banco de exámenes) según el título + un extracto del capítulo.
  - **Nombre de lección**: propone un título breve, específico y enseñable por
    lección, a partir de su texto fuente (reemplaza los "Capítulo — parte N"). Las
    lecciones cortadas por la IA de estructura ya traen título y se conservan.

Todo es best-effort: ante fallo del LLM/JSON, se conserva lo determinista.
"""
from __future__ import annotations

from typing import Any

from content_pipeline.llm.client import LLMClient, default_model, parse_json_object
from content_pipeline.processors.clean_text import shorten_text
from content_pipeline.taxonomy import CATEGORY_NAMES, FALLBACK, resolve

_TITULADAS = ("ia", "tope")  # cortes con título de la IA de estructura

_CATS = "\n".join(f"- {c}" for c in CATEGORY_NAMES)

_CAT_SYSTEM = (
    "Eres un diseñador instruccional de cursos de conducción en Chile. Asigna a "
    "CADA unidad UNA categoría de esta lista CERRADA (copia el texto exacto, con "
    "tildes). Si ninguna encaja, usa \"" + FALLBACK + "\":\n" + _CATS +
    "\n\nResponde SOLO un objeto JSON que mapea el número de unidad (string) a la "
    "categoría. Sin texto adicional. Ej: {\"1\": \"Señales de Tránsito\"}"
)

_NAME_SYSTEM = (
    "Eres editor de un curso e-learning de conducción (español de Chile). Para cada "
    "lección numerada, propone un TÍTULO breve (4 a 9 palabras), específico y "
    "enseñable, basado en su extracto. NO uses 'Lección', 'Parte', 'Unidad' ni "
    "numeración. Responde SOLO un objeto JSON {numero: titulo}, sin texto adicional."
)


def classify_unit_categories(
    unidades: list[dict[str, Any]], *, client: LLMClient, model: str,
) -> dict[int, str]:
    """{orden: categoria canónica} para las unidades. Vacío ante fallo."""
    if not unidades:
        return {}
    partes = []
    for u in unidades:
        snippet = shorten_text(" ".join((u.get("lecciones") or [{}])[0].get("texto", "").split()), 300)
        partes.append(f'#{u["orden"]}: {u.get("nombre","")}\n   {snippet}')
    try:
        raw = client.complete(
            system=_CAT_SYSTEM, user="Clasifica estas unidades:\n\n" + "\n\n".join(partes),
            max_tokens=1200, model=model, temperature=0.0,
        )
        data = parse_json_object(raw)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[int, str] = {}
    for u in unidades:
        val = data.get(str(u["orden"])) or data.get(u["orden"])
        if val:
            out[int(u["orden"])] = resolve(val)
    return out


def name_unit_lessons(
    unidad: dict[str, Any], *, client: LLMClient, model: str,
) -> dict[int, str]:
    """{indice_leccion(1-based): titulo} para las lecciones de UNA unidad."""
    lecciones = unidad.get("lecciones") or []
    if not lecciones:
        return {}
    partes = []
    for i, lec in enumerate(lecciones, 1):
        snippet = shorten_text(" ".join(str(lec.get("texto", "")).split()), 320)
        partes.append(f'#{i}: {snippet}')
    user = f'Unidad: {unidad.get("nombre","")}\n\nLecciones:\n' + "\n\n".join(partes)
    try:
        raw = client.complete(system=_NAME_SYSTEM, user=user, max_tokens=800,
                              model=model, temperature=0.2)
        data = parse_json_object(raw)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[int, str] = {}
    for i in range(1, len(lecciones) + 1):
        val = data.get(str(i)) or data.get(i)
        if val and str(val).strip():
            out[i] = shorten_text(str(val).strip(), 100)
    return out


def enrich_plan(
    plan: dict[str, Any], *, client: LLMClient | None = None, model: str | None = None,
    categorias: bool = True, nombres: bool = True,
) -> dict[str, Any]:
    """Rellena categorías y nombres del plan con IA (in place). Best-effort."""
    client = client or LLMClient()
    model = model or default_model()
    unidades = plan.get("unidades", [])

    if categorias:
        cats = classify_unit_categories(unidades, client=client, model=model)
        for u in unidades:
            if cats.get(int(u["orden"])):
                u["categoria"] = cats[int(u["orden"])]

    if nombres:
        for u in unidades:
            # Las lecciones cortadas por la IA de estructura ya traen título por tema.
            if all(lec.get("corte") in _TITULADAS for lec in u.get("lecciones", [])):
                continue
            nombres_map = name_unit_lessons(u, client=client, model=model)
            for i, lec in enumerate(u.get("lecciones", []), 1):
                if nombres_map.get(i) and lec.get("corte") not in _TITULADAS:
                    lec["nombre"] = nombres_map[i]
                    lec["seccion"] = lec.get("seccion") or None
    return plan
