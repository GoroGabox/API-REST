"""Redacta lecciones y quizzes con un LLM, anclado a la fuente (RAG).

Reutiliza la maquinaria determinista de `lesson_generator` (mapeo tema→segmento,
fuentes trazables, títulos, duración) y reemplaza SOLO la redacción del cuerpo:
en vez de pegar oraciones del libro, el LLM escribe una lección pedagógica real
usando únicamente los segmentos mapeados de ese tema. La cita de páginas (##
Fuente) se agrega de forma determinista para garantizar trazabilidad exacta.

Si una llamada al LLM falla, esa lección cae al renderizador extractivo
(`generic_lesson_generator`) para que un fallo puntual no tumbe el curso.
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterator

from content_pipeline.llm.client import LLMClient, default_model, parse_json_object
from content_pipeline.processors.clean_text import shorten_text
from content_pipeline.processors.generic_lesson_generator import (
    _quiz_content as _extractive_quiz,
    render_generic_lesson,
)
from content_pipeline.processors.lesson_generator import (
    SOURCE_NAME,
    _clamp,
    _combined_text,
    _lesson_title,
    _mapping_lookup,
    _matched_segments,
    _quiz_sources,
    _source_markdown,
    _sources_for_segments,
    build_lesson_context,
)

# Tope de fuente por lección (~2k tokens): mantiene el costo bajo y el foco.
_MAX_SOURCE_CHARS = 8_000

# Presupuesto de salida del cuerpo. La lección tiene 9 secciones; 1800 tokens
# no alcanzaban y algunas quedaban truncadas a mitad de sección. Se sube y,
# ante corte por `max_tokens` o secciones faltantes, se reintenta con más margen.
_BODY_MAX_TOKENS = 3_200
_BODY_MAX_TOKENS_RETRY = 4_096

# Encabezados obligatorios (mismo set y orden que pide LESSON_SYSTEM, sin
# "## Fuente" que se agrega después de forma determinista). Se usan para
# detectar cuerpos incompletos (truncados o con secciones omitidas por el LLM).
_REQUIRED_SECTIONS = (
    "## Objetivo",
    "## Introducción",
    "## Desarrollo",
    "## Aplicación práctica",
    "## Puntos clave",
    "## Ejemplo aplicado",
    "## Errores frecuentes",
    "## Actividad breve",
    "## Resumen",
)


# Flujo desde plan (fiel a la fuente): solo estas secciones son obligatorias; las
# demás son opcionales y se omiten cuando el extracto no da material para ellas
# (forzarlas empujaba a inventar ejemplos, errores y consejos que el libro no trae).
CORE_SECTIONS = ("## Objetivo", "## Desarrollo", "## Puntos clave", "## Resumen")
OPTIONAL_SECTIONS = (
    "## Introducción", "## Aplicación práctica", "## Ejemplo aplicado",
    "## Errores frecuentes", "## Actividad breve",
)


def _missing_sections(body: str, required: tuple[str, ...] = _REQUIRED_SECTIONS) -> list[str]:
    """Encabezados obligatorios ausentes en el cuerpo redactado."""
    return [heading for heading in required if heading not in body]


def _safe_stub_body(
    title: str, tema: str, unidad_nombre: str,
    sources: list[dict[str, Any]], source_name: str,
) -> str:
    """Cuerpo mínimo válido cuando la redacción falla, para no romper el curso.

    Trae las 9 secciones obligatorias (neutras, sin inventar datos) + la cita de
    fuente, de modo que pase la estructura y el importador. Señal de revisión.
    """
    t = (tema or title).strip()
    u = (unidad_nombre or "").strip().lower()
    return (
        f"# {title}\n\n"
        f"## Objetivo\nComprender los aspectos esenciales de {t.lower()}.\n\n"
        f"## Introducción\n{t} es un tema de {u}. Esta lección requiere revisión "
        "editorial: no se pudo redactar automáticamente desde la fuente.\n\n"
        f"## Desarrollo\nRevisa el material fuente indicado para estudiar {t.lower()} "
        "en profundidad.\n\n"
        "## Aplicación práctica\nAplica estos conceptos al conducir con criterio preventivo.\n\n"
        "## Puntos clave\n- Revisar la fuente citada.\n- Tema pendiente de redacción.\n\n"
        "## Ejemplo aplicado\nConsulta un caso concreto en el material fuente.\n\n"
        "## Errores frecuentes\n- Estudiar sin consultar la fuente oficial.\n\n"
        "## Actividad breve\nLee las páginas indicadas y resume las ideas principales.\n\n"
        "## Resumen\nLección pendiente de redacción; ver la fuente.\n\n"
        f"## Fuente\n{_source_markdown(sources, source_name)}"
    )


def _mapping_is_weak(mapping: dict[str, Any] | None) -> bool:
    """True si el tema no tiene fuente sólida: sin segmentos o solo matches
    forzados bajo umbral (``below_min_score``).

    Con fuente débil, redactar con el LLM produce alucinación (escribe desde un
    segmento irrelevante o desde conocimiento general). En ese caso se degrada al
    renderizador extractivo neutral, que NO inventa datos.
    """
    matched = (mapping or {}).get("matched_segments") or []
    if not matched:
        return True
    return all(bool(seg.get("below_min_score")) for seg in matched)

LESSON_SYSTEM = """\
Eres un redactor pedagógico experto en cursos de conducción en Chile. Escribes
lecciones e-learning claras, en español neutro, para estudiantes adultos.
{orientacion}
Recibes: el tema de una lección, su unidad, los objetivos de aprendizaje y
extractos del material fuente oficial del curso.

Reglas estrictas:
- Enseña el tema con tus palabras; NO copies oraciones literales del material.
- Fundamenta el contenido ÚNICAMENTE en los extractos provistos. Si algo no
  está en la fuente, no lo inventes (especialmente cifras, normativa o
  sanciones). Si la fuente es insuficiente, redacta lo general y sé prudente.
- Tono didáctico y concreto; ejemplos aplicados a la conducción real.
- Escribe en Markdown EXACTAMENTE con estos encabezados y en este orden,
  sin agregar ni quitar secciones, y sin incluir "## Fuente":

# {titulo}

## Objetivo
(1-2 frases: qué podrá hacer el estudiante al terminar)

## Introducción
(2 párrafos: por qué importa el tema)

## Desarrollo
(3-4 párrafos que explican el tema a partir de la fuente)

## Aplicación práctica
(2 párrafos: cómo se usa al conducir)

## Puntos clave
(4-6 viñetas con "- ")

## Ejemplo aplicado
(2 párrafos con una situación concreta de tránsito)

## Errores frecuentes
(4-5 viñetas con "- ")

## Actividad breve
(1 pregunta o ejercicio de reflexión)

## Resumen
(1 párrafo de cierre)

Responde solo con el Markdown de la lección.
"""

LESSON_USER = """\
Tema: {tema}
Unidad: {unidad}
Objetivos de aprendizaje de la unidad:
{objetivos}

Extractos del material fuente (úsalos como base):
---
{fuente}
---
Redacta la lección "{titulo}".
"""

QUIZ_SYSTEM = """\
Eres un evaluador de cursos de conducción. Creas quizzes de opción múltiple en
español, fundamentados en el material fuente. No inventes datos que no estén en
la fuente. Responde SOLO con JSON válido, sin ```fences.

Formato exacto:
{
  "questions": [
    {
      "question": "…",
      "options": ["A", "B", "C", "D"],
      "correct_index": 0,
      "explanation": "por qué es correcta"
    }
  ],
  "passing_score": 75
}
"""

QUIZ_USER = """\
Unidad: {unidad}
Temas de la unidad: {temas}

Extractos del material fuente:
---
{fuente}
---
Crea entre 4 y 6 preguntas de opción múltiple (4 opciones cada una) que evalúen
la comprensión de esta unidad. Devuelve solo el JSON.
"""


def _objetivos_text(objetivos: list[str]) -> str:
    if not objetivos:
        return "(No se especificaron objetivos; guíate por el tema.)"
    return "\n".join(f"- {o}" for o in objetivos)


def _source_for_prompt(segments: list[dict[str, Any]]) -> str:
    text = _combined_text(segments).strip()
    return shorten_text(text, _MAX_SOURCE_CHARS) if text else ""


def _complete_lesson_body(
    *,
    system: str,
    user: str,
    client: LLMClient,
    model: str,
    required: tuple[str, ...] = _REQUIRED_SECTIONS,
    temperature: float = 0.5,
) -> str:
    """Redacta el cuerpo con LLM, validando que esté completo.

    Detecta dos formas de cuerpo incompleto que el SDK NO reporta como error:
    corte por `max_tokens` (`resp.truncated`) y secciones obligatorias
    ausentes. Ante cualquiera, reintenta una vez con más presupuesto de salida;
    si sigue incompleto, lanza para que el llamador degrade al extractivo.
    """
    last_error = "cuerpo vacío"
    for max_tokens in (_BODY_MAX_TOKENS, _BODY_MAX_TOKENS_RETRY):
        resp = client.complete_meta(
            system=system,
            user=user,
            max_tokens=max_tokens,
            model=model,
            temperature=temperature,
        )
        body = resp.text
        if not body.strip():
            last_error = "cuerpo vacío"
            continue
        if resp.truncated:
            last_error = "cuerpo truncado por max_tokens"
            continue
        missing = _missing_sections(body, required)
        if missing:
            last_error = f"secciones faltantes: {', '.join(missing)}"
            continue
        return body
    raise ValueError(last_error)


def _orientacion_txt(orientacion: str | None) -> str:
    if not orientacion:
        return ""
    return (
        f"\nESTE CURSO ES PARA LA {orientacion} El material fuente sirve a varias "
        "licencias; cuando un tema aplique a más de una, enfoca los ejemplos, el "
        "énfasis y la aplicación práctica hacia esa licencia, sin inventar datos.\n"
    )


def _write_body(
    *,
    title: str,
    tema: str,
    unidad_nombre: str,
    objetivos: list[str],
    segments: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    source_name: str,
    client: LLMClient,
    model: str,
    context,
    orientacion: str | None = None,
) -> str:
    """Redacta el cuerpo con LLM; ante fallo cae al renderizador extractivo."""
    fuente = _source_for_prompt(segments)
    if not fuente:
        fuente = "(Sin extractos mapeados para este tema en el material fuente.)"
    try:
        body = _complete_lesson_body(
            system=LESSON_SYSTEM.replace("{titulo}", title).replace(
                "{orientacion}", _orientacion_txt(orientacion)
            ),
            user=LESSON_USER.format(
                tema=tema,
                unidad=unidad_nombre,
                objetivos=_objetivos_text(objetivos),
                fuente=fuente,
                titulo=title,
            ),
            client=client,
            model=model,
        )
        # Cita de páginas determinista (trazabilidad exacta, no del LLM).
        return f"{body.rstrip()}\n\n## Fuente\n{_source_markdown(sources, source_name)}"
    except Exception:
        # Fallback: renderizador extractivo neutral (no rompe el curso).
        return render_generic_lesson(context, source_name)


def _write_quiz(
    *,
    unidad_nombre: str,
    temas: list[str],
    segments: list[dict[str, Any]],
    client: LLMClient,
    model: str,
    fuente_text: str | None = None,
) -> dict[str, Any]:
    if fuente_text is not None:
        fuente = shorten_text(fuente_text, _MAX_SOURCE_CHARS) or "(Sin extractos; evalúa lo general de la unidad.)"
    else:
        fuente = _source_for_prompt(segments) or "(Sin extractos; evalúa lo general de la unidad.)"
    try:
        resp = client.complete_meta(
            system=QUIZ_SYSTEM,
            user=QUIZ_USER.format(
                unidad=unidad_nombre, temas=", ".join(temas), fuente=fuente
            ),
            max_tokens=2_000,
            model=model,
            temperature=0.3,
        )
        # Un JSON truncado puede parsear con la última pregunta corrupta: se
        # descarta y se cae al quiz extractivo en vez de guardar basura.
        if resp.truncated:
            raise ValueError("quiz truncado por max_tokens")
        data = parse_json_object(resp.text)
        questions = data.get("questions")
        if isinstance(questions, list) and questions:
            return {"questions": questions, "passing_score": int(data.get("passing_score", 75))}
    except Exception:
        pass
    return _extractive_quiz(temas)


def generate_lessons_llm(
    manifest: dict[str, Any],
    segments: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    *,
    source_name: str = SOURCE_NAME,
    orientacion: str | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Genera lecciones con LLM, emitiéndolas una a una (para streaming).

    Misma forma de salida que `generate_lessons_generic` (compatible con
    `import_generated_course`): una lección de texto por tema + un quiz por
    unidad.
    """
    client = client or LLMClient()
    model = model or default_model()
    segment_by_id = {str(s.get("segment_id")): s for s in segments}
    mapping_by_topic = _mapping_lookup(mappings)

    for unidad in manifest.get("unidades", []):
        if not isinstance(unidad, dict):
            continue
        orden = int(unidad.get("orden", 0))
        unidad_nombre = str(unidad.get("nombre", ""))
        categoria = str(unidad.get("categoria", ""))
        objetivos = [str(o) for o in unidad.get("objetivos", []) if str(o).strip()]
        temas = [str(t) for t in unidad.get("temas", [])]
        if not temas:
            continue

        target_minutes = int(unidad.get("horas_elearning", 0)) * 60
        quiz_minutes = 25
        minutes_per_topic = max(10, (target_minutes - quiz_minutes) / len(temas))
        position = 1
        unit_sources: list[dict[str, Any]] = []
        unit_segments: list[dict[str, Any]] = []

        for tema in temas:
            mapping = mapping_by_topic.get((orden, tema))
            matched_segments = _matched_segments(mapping, segment_by_id)
            unit_segments.extend(matched_segments)
            sources = _sources_for_segments(matched_segments, tema, source_name)
            unit_sources.extend(sources)
            duration = _clamp(round(minutes_per_topic), 15, 60)
            title = _lesson_title(tema, 0, 1)
            # Toda la construcción del cuerpo va en try/except: una lección jamás
            # debe tumbar la generación completa del curso (antes un fallo aquí
            # crasheaba el comando). Ante cualquier error, cuerpo mínimo seguro.
            fuente_debil = _mapping_is_weak(mapping)
            try:
                context = build_lesson_context(
                    title, tema, unidad_nombre, 0, 1, matched_segments, sources
                )
                # Fuente débil (sin segmentos o solo matches bajo umbral): NO
                # redactar con el LLM —alucinaría desde una fuente irrelevante—;
                # degradar al extractivo neutral y marcar para revisión.
                if fuente_debil:
                    body = render_generic_lesson(context, source_name)
                else:
                    body = _write_body(
                        title=title,
                        tema=tema,
                        unidad_nombre=unidad_nombre,
                        objetivos=objetivos,
                        segments=matched_segments,
                        sources=sources,
                        source_name=source_name,
                        client=client,
                        model=model,
                        context=context,
                        orientacion=orientacion,
                    )
            except Exception:  # noqa: BLE001 — resiliencia: no tumbar el curso
                body = _safe_stub_body(title, tema, unidad_nombre, sources, source_name)
                fuente_debil = True
            yield {
                "unidad_orden": orden,
                "unidad_nombre": unidad_nombre,
                "categoria": categoria,
                "tema_regulatorio": tema,
                "nombre": title,
                "posicion": position,
                "tipo": "texto",
                "descripcion": shorten_text(
                    f"Estudia {tema} dentro de {unidad_nombre} y aplícalo en situaciones concretas.",
                    240,
                ),
                "duracion_min": duration,
                "contenido": body,
                "transcripcion": "",
                "fuentes": sources,
                "fuente_debil": fuente_debil,
            }
            position += 1

        yield {
            "unidad_orden": orden,
            "unidad_nombre": unidad_nombre,
            "categoria": categoria,
            "tema_regulatorio": f"Evaluación módulo {orden}",
            "nombre": f"Evaluación del módulo {orden}",
            "posicion": position,
            "tipo": "quiz",
            "descripcion": f"Evaluación de cierre de la unidad {unidad_nombre}.",
            "duracion_min": quiz_minutes,
            "contenido": _write_quiz(
                unidad_nombre=unidad_nombre,
                temas=temas,
                segments=unit_segments,
                client=client,
                model=model,
            ),
            "transcripcion": "",
            "fuentes": _quiz_sources(unit_sources, unidad_nombre, orden, source_name),
        }


# ---------------------------------------------------------------------------
# Redacción DESDE UN PLAN (fuente exacta por lección; procedencia 1:1)
# ---------------------------------------------------------------------------
# La fuente de una lección del plan se entrega COMPLETA (sin truncar ni colapsar
# párrafos): antes se cortaba a 8.000 caracteres y el final del extracto nunca
# se redactaba. Este tope solo protege de planes editados a mano con lecciones
# desmesuradas.
_PLAN_SOURCE_CHARS = 30_000

FAITHFUL_LESSON_SYSTEM = """\
Eres un redactor pedagógico de cursos de conducción en Chile. Conviertes un
EXTRACTO del manual oficial en una lección e-learning clara, en español neutro,
para estudiantes adultos.
{orientacion}
PRINCIPIO: la lección enseña SOLO lo que dice el extracto. Todo dato, regla,
cifra, plazo, sanción, definición o recomendación de la lección debe estar en el
extracto. Si algo no está en el extracto, no lo escribas, aunque sea "sentido
común" o sepas que es cierto.

Reglas:
- Explica con tus palabras lo explicativo, pero las DEFINICIONES, NORMAS LEGALES,
  CIFRAS, LÍMITES, PLAZOS y SANCIONES se reproducen con la redacción del extracto
  (puedes citarlas entre comillas). No redondees, no conviertas unidades.
- No agregues ejemplos, casos, errores frecuentes ni consejos que el extracto no
  contenga. Un ejemplo solo puede reformular una situación que el extracto describe.
- No completes vacíos con conocimiento general. Si el extracto es breve, la
  lección es breve.
- Si el extracto trae rótulos de figuras o referencias a imágenes ("ver imagen"),
  no describas la imagen ni inventes lo que muestra: omite la referencia.
- Extensión: entre {min_palabras} y {max_palabras} palabras.

Formato Markdown, en este orden. Las secciones marcadas (opcional) inclúyelas SOLO
si el extracto da material concreto para ellas; si no, omítelas por completo (sin
encabezado). Escribe los encabezados sin la palabra "(opcional)".

# {titulo}

## Objetivo
(1-2 frases: qué podrá hacer el estudiante)

## Introducción (opcional)
(por qué importa el tema, solo si el extracto lo explica)

## Desarrollo
(el contenido del extracto, organizado y explicado: es la sección principal)

## Aplicación práctica (opcional)
(cómo se aplica al conducir, solo lo que el extracto indica)

## Ejemplo aplicado (opcional)
(una situación que el extracto describe)

## Errores frecuentes (opcional)
(viñetas con "- ": solo errores, riesgos o prohibiciones que el extracto menciona)

## Puntos clave
(3-6 viñetas con "- " con las ideas centrales del extracto)

## Actividad breve (opcional)
(una pregunta de repaso cuya respuesta está en el extracto)

## Resumen
(1 párrafo de cierre)

No incluyas "## Fuente". Responde solo con el Markdown de la lección.
"""

FAITHFUL_LESSON_USER = """\
Tema: {tema}
Unidad: {unidad}

EXTRACTO DEL MANUAL (única fuente permitida):
---
{fuente}
---
Redacta la lección "{titulo}" entre {min_palabras} y {max_palabras} palabras.
"""

# Compatibilidad: nombre anterior del prompt de usuario del flujo desde plan.
LESSON_FROM_SOURCE_USER = FAITHFUL_LESSON_USER


def lesson_length(palabras_fuente: int, tope: int) -> tuple[int, int]:
    """Rango de palabras de la lección, PROPORCIONAL a su fuente.

    Antes se pedía siempre el tope de la banda (1.200) aunque la fuente tuviera
    700 palabras: el modelo rellenaba con contenido que no estaba en el libro.
    Ahora el máximo es ~1,1× la fuente (piso 250, techo = tope del plan).
    """
    tope = int(tope or 1200)
    maximo = min(tope, max(250, round(int(palabras_fuente or 0) * 1.1)))
    return max(150, round(maximo * 0.6)), maximo


def _plan_source(text: str) -> str:
    text = (text or "").replace("­", "").strip()
    if len(text) > _PLAN_SOURCE_CHARS:
        text = text[:_PLAN_SOURCE_CHARS].rsplit(" ", 1)[0] + " […]"
    return text


def _clean_optional_markers(body: str) -> str:
    return re.sub(r"(?m)^(#{1,6} [^\n]*?)\s*\(opcional\)\s*$", r"\1", body)


def _stub_from_plan(title: str, tema: str, unidad_nombre: str, fuente_md: str) -> str:
    body = _safe_stub_body(title, tema, unidad_nombre, [], "")
    # Reemplaza la fuente del stub por la cita del plan.
    return re.sub(r"## Fuente\n.*$", f"## Fuente\n{fuente_md}", body, flags=re.DOTALL)


def write_lesson_from_source(**kwargs: Any) -> str:
    """Redacta una lección a partir de un texto fuente EXACTO (del plan)."""
    return write_lesson_from_source_meta(**kwargs)[0]


def write_lesson_from_source_meta(
    *,
    title: str,
    tema: str,
    unidad_nombre: str,
    source_text: str,
    palabras: int,
    fuente_md: str,
    client: LLMClient,
    model: str,
    orientacion: str | None = None,
    visual: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    """Como ``write_lesson_from_source`` pero devuelve ``(cuerpo, redactada)``.

    ``palabras`` es el TOPE del plan; la extensión real se ajusta a la fuente
    (``lesson_length``). ``redactada=False`` = la redacción falló y el cuerpo es
    el stub de revisión: el llamador debe marcar la lección.
    """
    fuente = _plan_source(source_text) or "(Sin material fuente para este tema.)"
    min_p, max_p = lesson_length(len(fuente.split()), palabras)
    aviso_visual = ""
    if visual and visual.get("nivel") == "alta":
        aviso_visual = ("\nATENCIÓN: estas páginas del libro son mayormente gráficas (figuras o señales). "
                        "El extracto solo trae sus rótulos: redacta únicamente lo que el texto dice, sin "
                        "describir ni suponer lo que muestran las imágenes.\n")
    try:
        body = _complete_lesson_body(
            system=FAITHFUL_LESSON_SYSTEM
                .replace("{titulo}", title)
                .replace("{orientacion}", _orientacion_txt(orientacion))
                .replace("{min_palabras}", str(min_p))
                .replace("{max_palabras}", str(max_p)),
            user=FAITHFUL_LESSON_USER.format(
                tema=tema, unidad=unidad_nombre, fuente=fuente, titulo=title,
                min_palabras=min_p, max_palabras=max_p,
            ) + aviso_visual,
            client=client,
            model=model,
            required=CORE_SECTIONS,
            temperature=0.3,
        )
        return f"{_clean_optional_markers(body).rstrip()}\n\n## Fuente\n{fuente_md}", True
    except Exception:  # noqa: BLE001 — una lección nunca tumba el curso
        return _stub_from_plan(title, tema, unidad_nombre, fuente_md), False


# ---------------------------------------------------------------------------
# Quiz de unidad DESDE UN PLAN: por lección, con evidencia textual verificada
# ---------------------------------------------------------------------------
# Antes el quiz recibía la unidad completa truncada a 8.000 caracteres (en
# unidades grandes veía el 12-20% del material) y nada validaba las preguntas.
# Ahora cada lección aporta preguntas generadas desde SU fuente completa, y cada
# pregunta debe citar textualmente la frase del extracto que respalda la
# respuesta: si esa cita no está en la fuente, la pregunta se descarta.
QUIZ_PLAN_SYSTEM = """\
Eres un evaluador de cursos de conducción en Chile. Creas preguntas de opción
múltiple que evalúan la comprensión de un EXTRACTO del manual oficial.

Reglas:
- Cada pregunta se responde SOLO con el extracto: la respuesta correcta debe
  estar dicha en él. No evalúes datos que no están en el extracto.
- "evidencia": copia LITERALMENTE (sin cambiar palabras) la frase del extracto
  que respalda la respuesta correcta.
- 4 opciones distintas y plausibles, una sola correcta. Las incorrectas no deben
  ser correctas según el extracto. Nada de "todas/ninguna de las anteriores".
- No preguntes por detalles triviales (rótulos de figuras, números de página).

Responde SOLO con JSON válido, sin ```:
{"questions": [{"question": "…", "options": ["…", "…", "…", "…"],
  "correct_index": 0, "explanation": "…", "evidencia": "…"}]}
"""

QUIZ_PLAN_USER = """\
Lección: {titulo}

EXTRACTO:
---
{fuente}
---
Crea exactamente {n} pregunta(s). Devuelve solo el JSON.
"""

_MIN_EVIDENCE_CHARS = 15
_QUIZ_TOTAL_TARGET = 5   # preguntas mínimas por unidad (repartidas entre sus lecciones)


def _norm_evidence(text: str) -> str:
    t = str(text or "").lower().replace("­", "")
    t = re.sub(r"[^0-9a-záéíóúüñ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def validate_quiz_question(q: Any, source_norm: str) -> str | None:
    """Motivo de descarte de una pregunta (None = válida)."""
    if not isinstance(q, dict):
        return "no es un objeto"
    if not str(q.get("question") or "").strip():
        return "sin enunciado"
    opts = q.get("options")
    if not isinstance(opts, list) or len(opts) != 4 or not all(str(o).strip() for o in opts):
        return "no tiene 4 opciones"
    if len({_norm_evidence(o) for o in opts}) != 4:
        return "opciones repetidas"
    ci = q.get("correct_index")
    if not isinstance(ci, int) or isinstance(ci, bool) or not 0 <= ci < 4:
        return "correct_index inválido"
    if any(re.search(r"\b(todas|ninguna|ambas)\b.{0,25}\banteriores\b", str(o), re.IGNORECASE) for o in opts):
        return "opción comodín"
    ev = _norm_evidence(q.get("evidencia"))
    if len(ev) < _MIN_EVIDENCE_CHARS:
        return "sin evidencia"
    if ev not in source_norm:
        return "la evidencia no está en la fuente"
    return None


def _shuffle_options(q: dict[str, Any]) -> dict[str, Any]:
    """Reordena las opciones de forma determinista: los LLM tienden a dejar la
    correcta en la primera posición."""
    import hashlib
    import random

    rnd = random.Random(int(hashlib.sha256(str(q["question"]).encode("utf-8")).hexdigest(), 16))
    order = list(range(4))
    rnd.shuffle(order)
    out = dict(q)
    out["options"] = [q["options"][i] for i in order]
    out["correct_index"] = order.index(q["correct_index"])
    return out


def questions_for_lesson(
    *, titulo: str, texto: str, n: int, client: LLMClient, model: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Hasta ``n`` preguntas válidas para una lección. Devuelve (preguntas, descartes)."""
    fuente = _plan_source(texto)
    source_norm = _norm_evidence(fuente)
    kept: list[dict[str, Any]] = []
    descartes: list[str] = []
    seen: set[str] = set()
    for _attempt in range(2):          # un reintento si quedaron preguntas descartadas
        faltan = n - len(kept)
        if faltan <= 0:
            break
        try:
            resp = client.complete_meta(
                system=QUIZ_PLAN_SYSTEM,
                user=QUIZ_PLAN_USER.format(titulo=titulo, fuente=fuente, n=faltan),
                max_tokens=600 + 450 * faltan,
                model=model,
                temperature=0.3,
            )
            if resp.truncated:
                raise ValueError("respuesta truncada")
            data = parse_json_object(resp.text)
        except Exception as exc:  # noqa: BLE001 — una lección sin preguntas no tumba el quiz
            descartes.append(f"{titulo}: {exc}")
            continue
        for q in data.get("questions") or []:
            motivo = validate_quiz_question(q, source_norm)
            key = _norm_evidence(q.get("question") if isinstance(q, dict) else "")
            if motivo is None and key in seen:
                motivo = "pregunta repetida"
            if motivo:
                descartes.append(f"{titulo}: {motivo}")
                continue
            seen.add(key)
            q = _shuffle_options({
                "question": str(q["question"]).strip(),
                "options": [str(o).strip() for o in q["options"]],
                "correct_index": q["correct_index"],
                "explanation": str(q.get("explanation") or "").strip(),
            }) | {"evidencia": str(q["evidencia"]).strip(), "leccion": titulo}
            kept.append(q)
            if len(kept) >= n:
                break
    return kept, descartes


def write_quiz_from_plan(
    *, unidad_nombre: str, lecciones: list[dict[str, Any]], client: LLMClient, model: str,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Quiz de la unidad cubriendo TODAS sus lecciones. Devuelve (quiz, meta, ok).

    Cada lección aporta ``ceil(5 / n_lecciones)`` preguntas (mínimo 1), así toda
    lección queda evaluada. ``ok=False`` si ninguna pregunta pasó la validación
    (se cae al quiz extractivo y la unidad queda marcada para revisión).
    """
    per = max(1, math.ceil(_QUIZ_TOTAL_TARGET / max(1, len(lecciones))))
    questions: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"lecciones": len(lecciones), "preguntas": 0, "descartadas": 0,
                            "lecciones_sin_preguntas": [], "descartes": []}
    for lec in lecciones:
        titulo = str(lec.get("nombre", "")).strip()
        qs, descartes = questions_for_lesson(
            titulo=titulo, texto=str(lec.get("texto", "")), n=per, client=client, model=model)
        questions.extend(qs)
        meta["descartes"].extend(descartes)
        if not qs:
            meta["lecciones_sin_preguntas"].append(titulo)
    meta["preguntas"] = len(questions)
    meta["descartadas"] = len(meta["descartes"])
    if not questions:
        return _extractive_quiz([str(l.get("nombre", "")) for l in lecciones]), meta, False
    return {"questions": questions, "passing_score": 75}, meta, True


def generate_lessons_from_plan(
    plan: dict[str, Any],
    *,
    source_name: str,
    orientacion: str | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Redacta el curso DESDE el plan editable (fuente exacta por lección).

    Salida compatible con `import_generated_course`: 1 lección de texto por
    lección del plan + 1 quiz por unidad (con preguntas de todas sus lecciones).
    """
    client = client or LLMClient()
    model = model or default_model()
    import hashlib

    from content_pipeline.review.ids import lesson_id

    for unidad in plan.get("unidades", []):
        orden = int(unidad.get("orden", 0))
        unidad_nombre = str(unidad.get("nombre", ""))
        categoria = str(unidad.get("categoria", "") or "General")
        lecciones = unidad.get("lecciones", [])
        if not lecciones:
            continue
        position = 1
        for lec in lecciones:
            title = str(lec.get("nombre", "")).strip() or f"Lección {position}"
            texto = str(lec.get("texto", ""))
            pgs = lec.get("paginas") or [0, 0]
            tope = int(lec.get("palabras_objetivo") or 900)
            _min_p, max_p = lesson_length(len(texto.split()), tope)
            fuente_md = f"{source_name}, páginas {pgs[0]}-{pgs[1]}."
            body, redactada = write_lesson_from_source_meta(
                title=title, tema=title, unidad_nombre=unidad_nombre,
                source_text=texto, palabras=tope, fuente_md=fuente_md,
                client=client, model=model, orientacion=orientacion, visual=lec.get("visual"),
            )
            yield {
                "unidad_orden": orden,
                "unidad_nombre": unidad_nombre,
                "categoria": categoria,
                "tema_regulatorio": title,
                "nombre": title,
                "posicion": position,
                "tipo": "texto",
                "descripcion": shorten_text(
                    f"{title} — dentro de {unidad_nombre}.", 240),
                "duracion_min": _clamp(round(max_p / 130) * 1, 15, 60),  # ~130 wpm lectura
                "contenido": body,
                "transcripcion": "",
                "fuentes": [{
                    "fuente_nombre": source_name,
                    "pagina_inicio": int(pgs[0] or 0),
                    "pagina_fin": int(pgs[1] or 0),
                    "tema_regulatorio": title,
                    "fragmento_resumen": shorten_text(texto, 600),
                    "hash_fragmento": hashlib.sha256(texto.encode("utf-8")).hexdigest(),
                }],
                # Stub de revisión (la redacción falló): marcado para que la
                # validación y el import lo bloqueen en vez de publicarlo.
                "fuente_debil": not redactada,
                "plan_id": lec.get("id") or lesson_id(texto),  # vínculo estable con el plan (revisión humana)
                # Fuente mayormente gráfica: revisar la lección contra el libro (no bloquea).
                "revision_visual": lec.get("visual"),
                "_source_text": texto,  # para la auditoría de fidelidad (no se persiste)
            }
            position += 1

        quiz, quiz_meta, quiz_ok = write_quiz_from_plan(
            unidad_nombre=unidad_nombre, lecciones=lecciones, client=client, model=model)
        yield {
            "unidad_orden": orden,
            "unidad_nombre": unidad_nombre,
            "categoria": categoria,
            "tema_regulatorio": f"Evaluación módulo {orden}",
            "nombre": f"Evaluación del módulo {orden}",
            "posicion": position,
            "tipo": "quiz",
            "descripcion": f"Evaluación de cierre de la unidad {unidad_nombre}.",
            "duracion_min": _clamp(round(len(quiz.get("questions") or []) * 1.5), 10, 30),
            "contenido": quiz,
            "transcripcion": "",
            "fuentes": [{
                "fuente_nombre": source_name,
                "pagina_inicio": int((unidad.get("paginas") or [0, 0])[0] or 0),
                "pagina_fin": int((unidad.get("paginas") or [0, 0])[1] or 0),
                "tema_regulatorio": f"Evaluación módulo {orden}",
                "fragmento_resumen": f"Evaluación de la unidad {unidad_nombre}.",
                "hash_fragmento": "",
            }],
            # Sin preguntas válidas → quiz extractivo de respaldo, bloqueado hasta revisión.
            "fuente_debil": not quiz_ok,
            "quiz_meta": quiz_meta,
        }
