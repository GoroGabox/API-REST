"""Interpreta el temario (Anexo gubernamental) con un LLM → manifest.

El Anexo de curso e-learning es un formulario oficial: mezcla datos
administrativos (escuela, región, responsable, plataforma) con la malla real
(módulos, objetivos de aprendizaje, horas, contenidos). El parser por regex
(`manifest_builder`) confunde campos del formulario con temas del curso.

Aquí le pedimos al LLM que lea el Anexo completo y devuelva SOLO la malla
pedagógica como JSON estructurado, descartando lo administrativo. El documento
es pequeño (~4-8k tokens) → una sola llamada barata y de alto impacto.
"""
from __future__ import annotations

from typing import Any

from content_pipeline.llm.client import LLMClient, default_model, parse_json_object
from content_pipeline.processors.clean_text import shorten_text
from content_pipeline.processors.manifest_builder import _cap_to_max_lessons

# Tope defensivo de tokens del temario que mandamos (por si llega un PDF enorme
# mal clasificado como temario). ~40k caracteres ≈ ~10k tokens.
_MAX_TEMARIO_CHARS = 40_000

MANIFEST_SYSTEM = """\
Eres un diseñador instruccional experto en cursos de licencias de conducir en Chile.
Recibes el texto de un "Anexo" oficial: el formulario de un curso e-learning que
mezcla información administrativa con la malla del curso.

Tu tarea: extraer ÚNICAMENTE la estructura pedagógica del curso y devolverla como
JSON válido, sin texto adicional.

Incluye SOLO contenido de enseñanza:
- módulos/unidades del curso (en su orden real),
- objetivos de aprendizaje de cada módulo,
- horas e-learning de cada módulo (número entero; 0 si no se indica),
- los temas/contenidos concretos de cada módulo.

DESCARTA todo lo administrativo: nombre/región de la escuela, responsable,
plataforma LMS, requisitos técnicos, formato de ejecución, datos de contacto,
firmas, encabezados y pies de página del formulario.

Reglas:
- Los "temas" deben ser temas enseñables (p. ej. "Distancia de frenado",
  "Señales reglamentarias"), NUNCA campos de formulario.
- No inventes módulos ni temas que no estén en el Anexo.
- Si un módulo no lista temas explícitos, deriva 2-5 temas razonables desde su
  objetivo de aprendizaje.
- Responde EXCLUSIVAMENTE con el JSON, sin ```fences ni comentarios.

Formato JSON exacto:
{
  "curso": {"descripcion": "1-2 frases sobre el curso"},
  "unidades": [
    {
      "orden": 1,
      "nombre": "Nombre del módulo",
      "categoria": "Nombre del módulo",
      "horas_elearning": 4,
      "objetivos": ["objetivo de aprendizaje", "..."],
      "temas": ["tema 1", "tema 2", "..."]
    }
  ]
}
"""

MANIFEST_USER = """\
Curso: {nombre} (código {codigo}).
Máximo de lecciones a producir después: {max_lecciones} (usa esto como guía de granularidad).

Texto del Anexo:
---
{temario}
---
Devuelve el JSON de la malla del curso.
"""


def _temario_text(pages: list[dict[str, Any]]) -> str:
    parts = [str(p.get("text", "")) for p in pages if p.get("has_text")]
    text = "\n".join(parts).strip()
    return text[:_MAX_TEMARIO_CHARS]


def _clean_str_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        s = shorten_text(str(item), limit)
        key = s.lower()
        if s and key not in seen:
            seen.add(key)
            out.append(s)
    return out


def _normalize_manifest(
    data: dict[str, Any],
    *,
    nombre: str,
    codigo: str,
    is_profesional: bool,
    max_lecciones: int,
) -> dict[str, Any]:
    raw_units = data.get("unidades")
    if not isinstance(raw_units, list) or not raw_units:
        raise ValueError("El LLM no devolvió unidades para el temario.")

    unidades: list[dict[str, Any]] = []
    for raw in raw_units:
        if not isinstance(raw, dict):
            continue
        temas = _clean_str_list(raw.get("temas"), 200)
        if not temas:
            continue
        nombre_unidad = shorten_text(str(raw.get("nombre") or f"Unidad {len(unidades) + 1}"), 100)
        try:
            horas = int(raw.get("horas_elearning") or 0)
        except (TypeError, ValueError):
            horas = 0
        unidades.append(
            {
                "orden": len(unidades) + 1,
                "nombre": nombre_unidad,
                "categoria": shorten_text(str(raw.get("categoria") or nombre_unidad), 100),
                "horas_elearning": max(0, horas),
                "objetivos": _clean_str_list(raw.get("objetivos"), 300),
                "temas": temas,
            }
        )

    if not unidades:
        raise ValueError("El LLM no devolvió temas válidos para ninguna unidad.")

    _cap_to_max_lessons(unidades, max_lecciones)

    curso_desc = ""
    curso_obj = data.get("curso")
    if isinstance(curso_obj, dict):
        curso_desc = str(curso_obj.get("descripcion") or "").strip()

    return {
        "curso": {
            "nombre": nombre,
            "codigo": codigo,
            "descripcion": shorten_text(
                curso_desc or f"Curso generado a partir del temario de {nombre}.", 500
            ),
            "is_profesional": bool(is_profesional),
            "costo": 0,
        },
        "unidades": unidades,
    }


def build_manifest_from_temario_llm(
    pages: list[dict[str, Any]],
    *,
    nombre: str,
    codigo: str,
    is_profesional: bool,
    max_lecciones: int = 20,
    client: LLMClient | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Interpreta el temario con LLM. Lanza excepción si falla (el orquestador
    hace fallback al parser por regex)."""
    temario = _temario_text(pages)
    if not temario:
        raise ValueError("El temario no tiene texto extraíble.")
    client = client or LLMClient()
    raw = client.complete(
        system=MANIFEST_SYSTEM,
        user=MANIFEST_USER.format(
            nombre=nombre, codigo=codigo, max_lecciones=max_lecciones, temario=temario
        ),
        max_tokens=4000,
        model=model or default_model(),
        temperature=0.2,
    )
    data = parse_json_object(raw)
    return _normalize_manifest(
        data,
        nombre=nombre,
        codigo=codigo,
        is_profesional=is_profesional,
        max_lecciones=max_lecciones,
    )
