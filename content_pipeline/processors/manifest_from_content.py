"""Infiere la estructura de un curso (manifest) desde el CONTENIDO, sin temario.

Cuando no hay un temario/Anexo oficial, se deriva la malla del propio material
fuente: se segmenta el libro (paso previo del pipeline) y, a partir de los
títulos/keywords de esos segmentos, se arma un manifest con la MISMA forma que
producen `manifest_builder` / `manifest_llm`:

    {"curso": {...}, "unidades": [{orden, nombre, categoria, horas_elearning, temas: [...]}, ...]}

Dos caminos, igual que el resto del pipeline:
- `build_manifest_from_content_llm`: el LLM organiza los segmentos en unidades y
  temas coherentes. Lanza excepción si falla (el llamador cae a la heurística).
- `build_manifest_from_content`: fallback determinista sin IA — reparte los
  segmentos en N unidades contiguas y usa sus títulos como temas.

AVISO DE DOMINIO: para cursos regulados (A2, A4…) la estructura la fija el
programa aprobado por la autoridad; inferirla del libro NO garantiza cumplir esa
malla. Este modo es para cursos no regulados, material propio o borradores.
"""
from __future__ import annotations

import math
import re
from typing import Any

from content_pipeline.llm.client import LLMClient, LLMError, default_model, parse_json_object
from content_pipeline.processors.clean_text import shorten_text
from content_pipeline.processors.manifest_builder import _cap_to_max_lessons
from content_pipeline.taxonomy import CATEGORY_NAMES, FALLBACK, resolve

# Lista cerrada de categorías (taxonomía compartida) para clasificar cada unidad.
_CATEGORIAS_BLOQUE = "\n".join(f"- {name}" for name in CATEGORY_NAMES)

# Tope defensivo de segmentos que resumimos para el LLM (compacto: título + kw).
# Se dimensiona para cubrir libros completos; si un libro lo excede, el
# orquestador avisa (course_planning.truncation_notes) para no truncar en silencio.
_MAX_SEGMENTS = 400

CONTENT_SYSTEM = """\
Eres un diseñador instruccional experto. Recibes una lista de SEGMENTOS extraídos
en orden de lectura de un libro/manual fuente. Cada segmento trae su página, un
título aproximado y palabras clave. NO tienes un temario oficial.
{orientacion}
Tu tarea: organizar ese material en una estructura de curso coherente y devolverla
como JSON válido, sin texto adicional.

Reglas:
- Agrupa los segmentos en unidades temáticas, en un orden lógico de aprendizaje.
- Cada "tema" debe ser un tema enseñable y concreto derivado del material
  (p. ej. "Distancia de frenado"), NO una palabra clave suelta, un encabezado de
  página, una línea de índice ni un título ruidoso.
{provenance_regla}- Produce aproximadamente {max_lecciones} temas en total (cuenta: 1 lección por
  tema + 1 quiz por unidad).
- {unidades_instr}
- No inventes temas ajenos al material.
- "categoria" DEBE ser EXACTAMENTE una de estas etiquetas (copia el texto tal
  cual, con sus tildes); elige la que mejor describe la unidad. Si ninguna
  encaja, usa "{fallback}":
{categorias}
- Responde EXCLUSIVAMENTE con el JSON, sin ```fences ni comentarios. Cuida la
  sintaxis: comillas dobles, comas entre elementos y sin comas colgantes.

Formato JSON exacto:
{{
  "curso": {{"descripcion": "1-2 frases sobre el curso"}},
  "unidades": [
    {{
      "orden": 1,
      "nombre": "Nombre de la unidad",
      "categoria": "una etiqueta EXACTA de la lista",
      "horas_elearning": 0,
      "objetivos": ["objetivo de aprendizaje", "..."],
      "temas": {temas_ejemplo}
    }}
  ]
}}
"""

_PROVENANCE_REGLA = (
    "- Cada tema DEBE declarar en \"segmentos\" los IDs (los [segxxxx] entre corchetes\n"
    "  de la lista) de los segmentos en que se basa: 1 a 4 IDs, copiados EXACTOS. NO\n"
    "  inventes IDs ni uses IDs que no estén en la lista. Esa procedencia es la fuente\n"
    "  real del tema (se redacta desde ahí), así que elígela con cuidado.\n"
)
_TEMAS_EJEMPLO_PROV = (
    '[\n'
    '        {{"nombre": "tema 1", "segmentos": ["seg_0012", "seg_0013"]}},\n'
    '        {{"nombre": "tema 2", "segmentos": ["seg_0020"]}}\n'
    '      ]'
)
_TEMAS_EJEMPLO_PLANO = '["tema 1", "tema 2", "..."]'

CONTENT_USER = """\
Curso: {nombre} (código {codigo}).

Segmentos del material (orden de lectura):
---
{digest}
---
Devuelve el JSON de la estructura del curso.
"""


def _segments_digest(segments: list[dict[str, Any]], *, limit: int = _MAX_SEGMENTS, kw: int = 6) -> str:
    lines: list[str] = []
    for s in segments[:limit]:
        sid = s.get("segment_id")
        title = shorten_text(str(s.get("title") or "").strip(), 120)
        kws = ", ".join(str(k) for k in (s.get("keywords") or [])[:kw])
        pg = s.get("page_start")
        # El ID [segxxxx] permite al LLM anclar cada tema a sus segmentos reales
        # (procedencia), evitando que map_topics tenga que re-adivinar la fuente.
        # Un fragmento del texto ayuda a inferir el tema cuando el título es ruidoso.
        snippet = shorten_text(" ".join(str(s.get("text") or "").split()), 200)
        lines.append(f"- [{sid}] (p{pg}) {title} :: {kws}\n    {snippet}")
    return "\n".join(lines)


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


def _temas_con_procedencia(value: Any) -> tuple[list[str], dict[str, list[str]]]:
    """Parsea ``temas`` que pueden ser strings u objetos {nombre, segmentos}.

    Devuelve (nombres_dedup, {nombre_lower: [segment_ids]}). Tolera el formato
    viejo (lista de strings): entonces la procedencia queda vacía y el mapeo cae
    a lo lexical.
    """
    if not isinstance(value, list):
        return [], {}
    nombres: list[str] = []
    seen: set[str] = set()
    prov: dict[str, list[str]] = {}
    for item in value:
        if isinstance(item, dict):
            nombre = shorten_text(str(item.get("nombre") or "").strip(), 200)
            seg_ids = [str(s).strip() for s in (item.get("segmentos") or []) if str(s).strip()]
        else:
            nombre = shorten_text(str(item).strip(), 200)
            seg_ids = []
        key = nombre.lower()
        if not nombre or key in seen:
            continue
        seen.add(key)
        nombres.append(nombre)
        if seg_ids:
            prov[key] = seg_ids
    return nombres, prov


def _finalize(
    unidades: list[dict[str, Any]],
    *,
    nombre: str,
    codigo: str,
    is_profesional: bool,
    max_lecciones: int,
    descripcion: str,
) -> dict[str, Any]:
    unidades = [u for u in unidades if u.get("temas")]
    if not unidades:
        raise ValueError("No se pudo inferir ninguna unidad con temas desde el contenido.")
    for index, unit in enumerate(unidades, start=1):
        unit["orden"] = index
    _cap_to_max_lessons(unidades, max_lecciones)
    return {
        "curso": {
            "nombre": nombre,
            "codigo": codigo,
            "descripcion": shorten_text(descripcion, 500),
            "is_profesional": bool(is_profesional),
            "costo": 0,
        },
        "unidades": unidades,
    }


def build_manifest_from_content_llm(
    segments: list[dict[str, Any]],
    *,
    nombre: str,
    codigo: str,
    is_profesional: bool,
    max_lecciones: int = 20,
    n_unidades: int | None = None,
    orientacion: str | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    retries: int = 2,
) -> dict[str, Any]:
    """Infiere la estructura del curso con LLM a partir de los segmentos.

    ``orientacion`` (opcional) enfoca la estructura hacia una licencia concreta
    cuando el mismo libro sirve a varias (A1–A5).

    Reintenta si el LLM devuelve JSON malformado o sin unidades válidas (ocurre
    de vez en cuando y suele resolverse con otra pasada). Si agota los intentos,
    lanza excepción y el orquestador cae a la heurística.
    """
    digest = _segments_digest(segments)
    if not digest.strip():
        raise ValueError("El contenido no produjo segmentos con texto para inferir la estructura.")

    unidades_instr = (
        f"Usa exactamente {n_unidades} unidades."
        if n_unidades and n_unidades > 0
        else "Usa entre 4 y 8 unidades según lo que pida el material."
    )
    orientacion_txt = (
        f"\nORIENTACIÓN: este curso es para la {orientacion} El libro fuente cubre "
        "varias licencias; prioriza, agrupa y enmarca los temas hacia esa licencia "
        "y no incluyas contenido propio de otras clases salvo como referencia breve.\n"
        if orientacion else ""
    )
    client = client or LLMClient()
    user = CONTENT_USER.format(nombre=nombre, codigo=codigo, digest=digest)

    def _system(with_provenance: bool) -> str:
        return CONTENT_SYSTEM.format(
            max_lecciones=max_lecciones,
            unidades_instr=unidades_instr,
            categorias=_CATEGORIAS_BLOQUE,
            fallback=FALLBACK,
            orientacion=orientacion_txt,
            provenance_regla=_PROVENANCE_REGLA if with_provenance else "",
            temas_ejemplo=_TEMAS_EJEMPLO_PROV if with_provenance else _TEMAS_EJEMPLO_PLANO,
        )

    last_err: Exception | None = None
    # Dos tiers: primero con procedencia (temas como objetos, mejor mapeo). Si el
    # JSON con esa estructura anidada falla repetido, se reintenta SIN procedencia
    # (temas como strings, JSON más simple) para NO caer a la heurística ruidosa.
    for with_provenance in (True, False):
        system = _system(with_provenance)
        for _attempt in range(retries + 1):
            raw = client.complete(
                system=system,
                user=user,
                # Presupuesto amplio: un curso de libro completo puede tener muchas
                # unidades/temas y el JSON no debe cortarse.
                max_tokens=12000,
                model=model or default_model(),
                temperature=0.2,
            )
            try:
                return _parse_content_manifest(
                    raw,
                    nombre=nombre,
                    codigo=codigo,
                    is_profesional=is_profesional,
                    max_lecciones=max_lecciones,
                )
            except (LLMError, ValueError) as exc:
                last_err = exc  # JSON malformado / sin unidades: reintenta
    raise LLMError(
        f"El LLM no devolvió una estructura válida tras varios intentos "
        f"(con y sin procedencia): {last_err}"
    )


def _parse_content_manifest(
    raw: str,
    *,
    nombre: str,
    codigo: str,
    is_profesional: bool,
    max_lecciones: int,
) -> dict[str, Any]:
    data = parse_json_object(raw)  # puede lanzar LLMError si el JSON es inválido

    raw_units = data.get("unidades")
    if not isinstance(raw_units, list) or not raw_units:
        raise ValueError("El LLM no devolvió unidades desde el contenido.")

    unidades: list[dict[str, Any]] = []
    prov_by_name: dict[str, list[str]] = {}  # nombre_tema_lower -> [segment_ids]
    for raw_unit in raw_units:
        if not isinstance(raw_unit, dict):
            continue
        temas, prov = _temas_con_procedencia(raw_unit.get("temas"))
        if not temas:
            continue
        prov_by_name.update(prov)
        nombre_unidad = shorten_text(str(raw_unit.get("nombre") or f"Unidad {len(unidades) + 1}"), 100)
        try:
            horas = int(raw_unit.get("horas_elearning") or 0)
        except (TypeError, ValueError):
            horas = 0
        unidades.append(
            {
                "orden": len(unidades) + 1,
                "nombre": nombre_unidad,
                # Categoría canónica: el LLM elige de la lista cerrada; resolve()
                # normaliza variantes y manda a "General" lo que no encaje.
                "categoria": resolve(raw_unit.get("categoria")),
                "horas_elearning": max(0, horas),
                "objetivos": _clean_str_list(raw_unit.get("objetivos"), 300),
                "temas": temas,
            }
        )

    curso_obj = data.get("curso") if isinstance(data.get("curso"), dict) else {}
    descripcion = str((curso_obj or {}).get("descripcion") or "").strip() or (
        f"Curso generado a partir del contenido de {nombre}."
    )
    manifest = _finalize(
        unidades,
        nombre=nombre,
        codigo=codigo,
        is_profesional=is_profesional,
        max_lecciones=max_lecciones,
        descripcion=descripcion,
    )
    # Procedencia (tema -> segmentos que el LLM declaró): se arma con el orden
    # FINAL del manifest (tras _finalize/cap). El orquestador la usa para mapear
    # directo, sin re-adivinar con map_topics.
    provenance: list[dict[str, Any]] = []
    for unidad in manifest["unidades"]:
        for tema in unidad.get("temas", []):
            seg_ids = prov_by_name.get(str(tema).lower())
            if seg_ids:
                provenance.append({
                    "unidad_orden": unidad["orden"],
                    "tema": tema,
                    "segment_ids": seg_ids,
                })
    manifest["_provenance"] = provenance
    return manifest


# Ruido típico de títulos de segmento: encabezados/pies de página del libro,
# líneas de índice (puntos suspensivos), fragmentos numéricos ("50 km/h", "440 g").
_NOISE_TITLE_RE = re.compile(r"\.{4,}|libro del nuevo conductor|conaset|^\s*\d+\s", re.IGNORECASE)


def _is_noise_title(text: str) -> bool:
    t = (text or "").strip()
    if not t or _NOISE_TITLE_RE.search(t):
        return True
    letters = re.sub(r"[^a-záéíóúñü]", "", t.lower())
    words = re.findall(r"[a-záéíóúñü]{3,}", t.lower())
    return len(letters) < 6 or not words


def _tema_from_segment(segment: dict[str, Any]) -> str:
    """Nombre de tema legible desde un segmento (sin el prefijo 'Tema:').

    Si el título es ruido (encabezado, índice, fragmento numérico), intenta con
    las keywords; si tampoco sirve, devuelve "" para que el llamador lo descarte.
    """
    title = str(segment.get("title") or "").strip()
    if title.lower().startswith("tema:"):
        title = title[len("tema:"):].strip()
    # El título del segmentador puede ser una lista de keywords ("A, B, C");
    # nos quedamos con la primera para un nombre de tema más limpio.
    if "," in title:
        title = title.split(",")[0].strip()
    if not title or _is_noise_title(title):
        kws = [str(k) for k in (segment.get("keywords") or []) if len(str(k)) >= 4]
        title = ", ".join(kws[:3]) if kws else ""
    if not title or _is_noise_title(title):
        return ""
    return shorten_text(title, 200)


def build_manifest_from_content(
    segments: list[dict[str, Any]],
    *,
    nombre: str,
    codigo: str,
    is_profesional: bool,
    max_lecciones: int = 20,
    n_unidades: int | None = None,
) -> dict[str, Any]:
    """Fallback determinista (sin IA): reparte segmentos en N unidades contiguas.

    Calidad inferior al modo IA —los nombres de unidad/tema salen de los títulos
    del segmentador— pero permite generar sin API key.
    """
    # Filtra segmentos de bajo valor (índice/portada/catálogo) antes de nombrar
    # temas — así el fallback no produce temas-ruido tipo "12 Libro del Nuevo...".
    from content_pipeline.processors.map_topics import _is_low_value_segment
    seg = [
        s for s in segments
        if (s.get("title") or s.get("keywords")) and not _is_low_value_segment(s)
    ]
    if not seg:
        raise ValueError("No hay segmentos de contenido para inferir la estructura.")

    if not n_unidades or n_unidades < 1:
        # Heurística de granularidad: ~raíz de max_lecciones, acotado a [3, 8].
        n_unidades = min(8, max(3, round(max_lecciones ** 0.5)))
    n_unidades = min(n_unidades, len(seg))

    per = math.ceil(len(seg) / n_unidades)
    unidades: list[dict[str, Any]] = []
    for i in range(n_unidades):
        chunk = seg[i * per : (i + 1) * per]
        if not chunk:
            continue
        temas: list[str] = []
        seen: set[str] = set()
        for s in chunk:
            tema = _tema_from_segment(s)
            key = tema.lower()
            if tema and key not in seen:
                seen.add(key)
                temas.append(tema)
        if not temas:
            continue
        nombre_unidad = temas[0]
        unidades.append(
            {
                "orden": len(unidades) + 1,
                "nombre": shorten_text(nombre_unidad, 100),
                # Fallback determinista (sin IA): no clasifica bien, cae a "General".
                "categoria": resolve(nombre_unidad),
                "horas_elearning": 0,
                "temas": temas,
            }
        )

    return _finalize(
        unidades,
        nombre=nombre,
        codigo=codigo,
        is_profesional=is_profesional,
        max_lecciones=max_lecciones,
        descripcion=f"Curso generado automáticamente a partir del contenido de {nombre}.",
    )
