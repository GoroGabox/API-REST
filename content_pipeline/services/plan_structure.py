"""Estructura del plan con IA: agrupa párrafos en lecciones por TEMA y DENSIDAD.

Las guías llegan en Word sin distinciones de formato (sin estilos, negritas ni
tamaños), así que no hay señal tipográfica fiable de dónde empieza un tema. Acá el
LLM lee los párrafos numerados de un capítulo y decide qué párrafos CONSECUTIVOS
forman cada lección, con criterio pedagógico (un tema/subtema coherente por
lección; contenido denso → lecciones más acotadas).

**Procedencia 1:1**: el LLM NO reescribe texto, solo devuelve rangos de índices
(`desde`/`hasta`). El texto de cada lección se arma con los párrafos originales.
La respuesta se valida estrictamente (rangos contiguos, en orden, sin huecos ni
solapes); ante error hay un reintento con el error concreto; si el reintento aún
trae bordes inválidos se reparan (``repair_groups``) y, si no hay nada utilizable,
se devuelve ``None`` → el planificador cae al corte por palabras.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from content_pipeline.llm.client import LLMClient, default_model, parse_json_object
from content_pipeline.processors.outline import Para

# Granularidad por `--largo`: instrucción para el LLM. Las palabras son solo una
# REFERENCIA (manda el tema); el límite duro es el tope de la banda.
GRANULARIDAD = {
    "corta": "lecciones ACOTADAS: un subtema por lección (p. ej. solo 'Semáforos'). "
             "Referencia: 150 a 400 palabras de fuente.",
    "media": "lecciones MEDIANAS: 2 a 4 subtemas afines por lección (p. ej. 'Señales de "
             "carabineros y semáforos'). Referencia: 300 a 700 palabras de fuente.",
    "larga": "lecciones AMPLIAS: un tema completo por lección (p. ej. todas las 'Señales de "
             "tránsito'). Referencia: 600 a 1200 palabras de fuente.",
}
# Mínimo orientativo por banda: bajo esto, la lección debería unirse a un subtema afín.
MINIMO_PALABRAS = {"corta": 120, "media": 250, "larga": 500}

DENSIDADES = ("alta", "media", "baja")

# Ventana máxima de palabras por llamada (capítulos más grandes se procesan por tramos).
_WINDOW_WORDS = 8000
# Para proponer unidades basta el comienzo de cada párrafo.
_UNIT_PARA_WORDS = 14
_UNIT_WINDOW_PARAS = 900
# Tope de max_tokens por llamada (solo se cobra lo usado).
_MAX_TOKENS_TOPE = 16000   # sobre ~21k el SDK exige streaming

_LESSON_SYSTEM = (
    "Eres diseñador instruccional de cursos e-learning de conducción (español de Chile). "
    "Recibes los párrafos NUMERADOS de un capítulo de un libro, en orden. Tu tarea es "
    "decidir qué párrafos CONSECUTIVOS forman cada lección.\n\n"
    "Criterios:\n"
    "- Cada lección trata UN tema o subtema coherente que un estudiante puede estudiar de "
    "una sentada. Corta donde el libro cambia de tema, no por cantidad de palabras.\n"
    "- Considera la DENSIDAD: listas de normas, cifras, señales o definiciones concentran "
    "muchas ideas por palabra → lecciones más acotadas; la prosa explicativa admite "
    "lecciones más amplias.\n"
    "- Nunca separes un párrafo que termina en ':' de la lista o explicación que introduce.\n"
    "- Los recuadros ('DEBES SABER', 'NO OLVIDES', 'RECORDATORIO', 'IMPORTANTE', ejemplos) "
    "van en la lección del tema al que pertenecen.\n"
    "- Una introducción o agenda del capítulo va con la primera lección.\n"
    "- La referencia de palabras de la granularidad es orientativa: manda el tema. Pero no "
    "dejes lecciones diminutas: un subtema breve se une al subtema AFÍN vecino (y el título "
    "lo refleja). No mezcles temas sin relación.\n"
    "- Cubre TODOS los párrafos, sin huecos ni solapes, en orden.\n\n"
    "Responde SOLO un objeto JSON, sin texto adicional:\n"
    '{"lecciones": [{"desde": 0, "hasta": 14, "titulo": "Semáforos y señales de carabineros", '
    '"temas": ["Señales de carabineros", "Semáforos"], "densidad": "alta", '
    '"motivo": "cambia de las señales a las reglas de preferencia"}]}\n'
    "- desde/hasta: índices INCLUSIVOS de los párrafos.\n"
    "- titulo: 4 a 9 palabras, específico y enseñable; sin 'Lección', 'Parte', 'Unidad' ni numeración.\n"
    "- temas: los subtemas que cubre la lección, como los nombra el libro.\n"
    "- densidad: alta | media | baja (ideas por palabra).\n"
    "- motivo: por qué la lección termina ahí (breve)."
)

_UNIT_SYSTEM = (
    "Eres diseñador instruccional. Recibes el COMIENZO de cada párrafo NUMERADO de un libro "
    "de conducción (español de Chile), en orden. El documento no trae formato, así que debes "
    "detectar dónde empieza cada CAPÍTULO o gran tema del libro y agrupar los párrafos en "
    "UNIDADES consecutivas (normalmente entre 4 y 15 unidades).\n"
    "- Usa los títulos de capítulo del propio libro cuando aparecen ('Capítulo 3 ...', "
    "'Normas de circulación'); si no, nombra la unidad por su tema (3 a 7 palabras).\n"
    "- Portada, presentación o índice van con la primera unidad.\n"
    "- Cubre TODOS los párrafos, sin huecos ni solapes, en orden.\n\n"
    "Responde SOLO un objeto JSON, sin texto adicional:\n"
    '{"unidades": [{"desde": 0, "hasta": 120, "nombre": "Normas de circulación"}]}\n'
    "desde/hasta son índices INCLUSIVOS."
)


@dataclass
class Grupo:
    """Rango de párrafos [desde, hasta] (inclusivo) que forma una lección o unidad."""
    desde: int
    hasta: int
    titulo: str
    temas: list[str] = field(default_factory=list)
    densidad: str | None = None
    motivo: str | None = None


def _clean(s: Any, limit: int = 100) -> str:
    return " ".join(str(s or "").split())[:limit]


def validate_groups(items: Any, n: int, *, title_key: str = "titulo") -> tuple[list[Grupo], str | None]:
    """Valida rangos contiguos que cubren ``0..n-1``. Devuelve (grupos, error)."""
    if not isinstance(items, list) or not items:
        return [], "la lista está vacía o no es una lista"
    grupos: list[Grupo] = []
    esperado = 0
    for k, it in enumerate(items):
        if not isinstance(it, dict):
            return [], f"el elemento {k} no es un objeto"
        try:
            a, b = int(it.get("desde")), int(it.get("hasta"))
        except (TypeError, ValueError):
            return [], f"el elemento {k} no trae desde/hasta numéricos"
        if a != esperado:
            return [], (f"el elemento {k} empieza en {a} pero debía empezar en {esperado} "
                        "(hay un hueco o un solape)")
        if b < a or b >= n:
            return [], f"el elemento {k} tiene un rango inválido {a}-{b} (párrafos 0..{n - 1})"
        titulo = _clean(it.get(title_key))
        if not titulo:
            return [], f"el elemento {k} no trae {title_key}"
        temas = [_clean(t) for t in (it.get("temas") or []) if _clean(t)] if isinstance(it.get("temas"), list) else []
        dens = str(it.get("densidad") or "").strip().lower()
        grupos.append(Grupo(desde=a, hasta=b, titulo=titulo, temas=temas,
                            densidad=dens if dens in DENSIDADES else None,
                            motivo=_clean(it.get("motivo"), 200) or None))
        esperado = b + 1
    if esperado != n:
        return [], f"faltan los párrafos {esperado}..{n - 1} (debe cubrir todos)"
    return grupos, None


def repair_groups(items: Any, n: int, *, title_key: str = "titulo") -> list[Grupo] | None:
    """Ajusta bordes de rangos casi válidos (huecos/solapes) sin perder texto.

    Ordena por ``desde``, hace contiguo cada rango (``desde`` = fin previo + 1), extiende
    el último hasta ``n-1`` y descarta los que quedan contenidos en el anterior. Como el
    texto siempre sale de los párrafos originales, mover un borde no inventa ni pierde
    contenido: solo cambia a qué lección va un párrafo limítrofe.
    """
    if not isinstance(items, list):
        return None
    validos = []
    for it in items:
        try:
            a, b = int(it.get("desde")), int(it.get("hasta"))
        except (AttributeError, TypeError, ValueError):
            continue
        if _clean(it.get(title_key)) and 0 <= a <= b:
            validos.append({**it, "desde": a, "hasta": min(b, n - 1)})
    validos.sort(key=lambda it: it["desde"])
    fixed: list[dict[str, Any]] = []
    siguiente = 0
    for it in validos:
        if it["hasta"] < siguiente:
            continue
        fixed.append({**it, "desde": siguiente})
        siguiente = it["hasta"] + 1
    if not fixed:
        return None
    fixed[-1]["hasta"] = n - 1
    grupos, error = validate_groups(fixed, n, title_key=title_key)
    return grupos if error is None else None


def _complete(client: Any, *, system: str, user: str, max_tokens: int, model: str) -> tuple[str, bool]:
    """(texto, truncada). Usa ``complete_meta`` si el cliente lo tiene (detecta max_tokens)."""
    if hasattr(client, "complete_meta"):
        r = client.complete_meta(system=system, user=user, max_tokens=max_tokens,
                                 model=model, temperature=0.0)
        return r.text, bool(r.truncated)
    return client.complete(system=system, user=user, max_tokens=max_tokens,
                           model=model, temperature=0.0), False


def _ask(client: LLMClient, model: str, system: str, user: str, key: str, n: int,
         title_key: str, max_tokens: int) -> list[Grupo] | None:
    """Una llamada + un reintento con el error concreto; si el reintento aún trae bordes
    inválidos se reparan (``repair_groups``). ``None`` si no hay nada utilizable."""
    error = None
    data: dict = {}
    for _ in range(2):
        prompt = user if error is None else (
            f"{user}\n\nTu respuesta anterior era inválida: {error}. Corrígela y responde "
            "solo el JSON.")
        # Errores de red / rate limit se propagan: el segmentador reintenta el capítulo.
        raw, truncada = _complete(client, system=system, user=prompt, max_tokens=max_tokens, model=model)
        if truncada:
            # El modelo puede gastar el presupuesto razonando antes de escribir: se duplica.
            max_tokens = min(_MAX_TOKENS_TOPE, max_tokens * 2)
            error = "la respuesta quedó truncada; responde solo el JSON, sin explicaciones"
            data = {}
            continue
        try:
            data = parse_json_object(raw)
        except Exception as exc:  # noqa: BLE001 — JSON roto: se reintenta una vez
            error = f"no se pudo leer el JSON ({exc})"
            data = {}
            continue
        grupos, error = validate_groups(data.get(key), n, title_key=title_key)
        if error is None:
            return grupos
    return repair_groups(data.get(key), n, title_key=title_key) if data else None


def _segment_window(titulo: str, paras: list[Para], largo: str, tope: int, *,
                    client: LLMClient, model: str) -> list[Grupo] | None:
    numbered = "\n".join(f"[{i}] {p.text}" for i, p in enumerate(paras))
    total = sum(len(p.text.split()) for p in paras)
    minimo = MINIMO_PALABRAS.get(largo, MINIMO_PALABRAS["media"])
    user = (
        f"Capítulo: {titulo} ({total} palabras)\n"
        f"Granularidad pedida: {GRANULARIDAD.get(largo, GRANULARIDAD['media'])}\n"
        f"Evita lecciones de menos de {minimo} palabras. Si el capítulo tiene menos de "
        f"{2 * minimo} palabras, es UNA sola lección.\n"
        f"Límite duro: ninguna lección puede superar {tope} palabras de fuente.\n"
        f"Párrafos (0 a {len(paras) - 1}):\n\n{numbered}"
    )
    # Holgado: los modelos con razonamiento lo consumen antes de escribir el JSON.
    max_tokens = min(_MAX_TOKENS_TOPE, 8000 + 30 * len(paras))
    return _ask(client, model, _LESSON_SYSTEM, user, "lecciones", len(paras), "titulo", max_tokens)


def merge_tiny(grupos: list[Grupo], paras: list[Para], minimo: int, tope: int) -> list[Grupo]:
    """Une al vecino las lecciones de menos de ``minimo`` palabras (red de seguridad).

    Se une con la lección previa (o la siguiente si es la primera) si el resultado no
    supera ``tope``. Conserva el título de la parte más larga y suma los temas.
    """
    def words(g: Grupo) -> int:
        return sum(len(p.text.split()) for p in paras[g.desde:g.hasta + 1])

    out = list(grupos)
    changed = True
    while changed and len(out) > 1:
        changed = False
        for k, g in enumerate(out):
            if words(g) >= minimo:
                continue
            j = k - 1 if k > 0 else k + 1
            a, b = (out[j], g) if j < k else (g, out[j])
            if words(a) + words(b) > tope:
                continue
            mayor = a if words(a) >= words(b) else b
            temas = list(dict.fromkeys((a.temas or [a.titulo]) + (b.temas or [b.titulo])))
            out[min(j, k)] = Grupo(desde=a.desde, hasta=b.hasta, titulo=mayor.titulo, temas=temas,
                                   densidad=mayor.densidad or a.densidad or b.densidad,
                                   motivo=b.motivo)
            del out[max(j, k)]
            changed = True
            break
    return out


def fix_colon_borders(grupos: list[Grupo], paras: list[Para]) -> list[Grupo]:
    """Un párrafo que termina en ':' introduce lo que sigue: si cierra una lección, pasa
    al inicio de la siguiente (salvo que la lección quede vacía)."""
    out = list(grupos)
    for k in range(len(out) - 1):
        g, nxt = out[k], out[k + 1]
        while g.hasta > g.desde and paras[g.hasta].text.rstrip().endswith(":"):
            g = Grupo(g.desde, g.hasta - 1, g.titulo, g.temas, g.densidad, g.motivo)
            nxt = Grupo(nxt.desde - 1, nxt.hasta, nxt.titulo, nxt.temas, nxt.densidad, nxt.motivo)
        out[k], out[k + 1] = g, nxt
    return out


def segment_chapter_llm(titulo: str, paras: list[Para], largo: str, tope: int, *,
                        client: LLMClient, model: str | None = None) -> list[Grupo] | None:
    """Agrupa los párrafos del capítulo en lecciones. ``None`` si la IA falla.

    Capítulos grandes se procesan por ventanas: de cada ventana se aceptan todas las
    lecciones menos la última (que puede estar cortada por el borde) y la siguiente
    ventana arranca en el inicio de esa última lección.
    """
    model = model or default_model()
    if not paras:
        return []
    out: list[Grupo] = []
    start = 0
    while start < len(paras):
        end, w = start, 0
        while end < len(paras) and (end == start or w + len(paras[end].text.split()) <= _WINDOW_WORDS):
            w += len(paras[end].text.split())
            end += 1
        grupos = _segment_window(titulo, paras[start:end], largo, tope, client=client, model=model)
        if grupos is None:
            return None
        ultimo_tramo = end >= len(paras)
        if not ultimo_tramo and len(grupos) > 1:
            grupos = grupos[:-1]               # la última puede estar cortada por el borde
        for g in grupos:
            out.append(Grupo(desde=g.desde + start, hasta=g.hasta + start, titulo=g.titulo,
                             temas=g.temas, densidad=g.densidad, motivo=g.motivo))
        start = out[-1].hasta + 1
    return out


def segment_units_llm(paras: list[Para], *, client: LLMClient,
                      model: str | None = None) -> list[Grupo] | None:
    """Propone las UNIDADES (capítulos) de un libro sin formato. ``None`` si falla."""
    model = model or default_model()
    if not paras:
        return None
    if len(paras) > _UNIT_WINDOW_PARAS:
        # Libros enormes: ventanas cosidas como en las lecciones (la última unidad de
        # cada ventana se re-evalúa en la siguiente).
        out: list[Grupo] = []
        start = 0
        while start < len(paras):
            end = min(len(paras), start + _UNIT_WINDOW_PARAS)
            grupos = _units_window(paras[start:end], client=client, model=model)
            if grupos is None:
                return None
            if end < len(paras) and len(grupos) > 1:
                grupos = grupos[:-1]
            out.extend(Grupo(desde=g.desde + start, hasta=g.hasta + start, titulo=g.titulo) for g in grupos)
            start = out[-1].hasta + 1
        return out
    return _units_window(paras, client=client, model=model)


def _units_window(paras: list[Para], *, client: LLMClient, model: str) -> list[Grupo] | None:
    def head(p: Para) -> str:
        ws = p.text.split()
        return " ".join(ws[:_UNIT_PARA_WORDS]) + (" …" if len(ws) > _UNIT_PARA_WORDS else "")
    numbered = "\n".join(f"[{i}] {head(p)}" for i, p in enumerate(paras))
    user = f"Párrafos (0 a {len(paras) - 1}):\n\n{numbered}"
    return _ask(client, model, _UNIT_SYSTEM, user, "unidades", len(paras), "nombre", 8000)


class LLMSegmenter:
    """Segmentador que usa ``build_plan`` (inyectable; en tests se usa un cliente falso).

    Ante errores de red / rate limit reintenta el capítulo completo con pausa creciente.
    Los capítulos que igual fallan quedan en ``errores`` ({titulo: motivo}) y el
    planificador los corta por palabras.
    """

    def __init__(self, client: LLMClient | None = None, model: str | None = None,
                 intentos: int = 3, pausa: float = 15.0):
        self.model = model or default_model()
        self.client = client or LLMClient(model=self.model)
        self.intentos = intentos
        self.pausa = pausa
        self.errores: dict[str, str] = {}

    def _run(self, etiqueta: str, fn) -> list[Grupo] | None:
        motivo = "la IA no devolvió rangos válidos"
        for intento in range(self.intentos):
            try:
                grupos = fn()
            except Exception as exc:  # noqa: BLE001 — red / rate limit / sobrecarga
                motivo = f"error del LLM: {exc}"[:300]
                if intento + 1 < self.intentos:
                    time.sleep(self.pausa * (intento + 1))
                continue
            if grupos:
                return grupos
            break                      # respuesta inválida tras reintento + reparación
        self.errores[etiqueta] = motivo
        return None

    def lessons(self, titulo: str, paras: list[Para], largo: str, tope: int) -> list[Grupo] | None:
        return self._run(titulo, lambda: segment_chapter_llm(
            titulo, paras, largo, tope, client=self.client, model=self.model))

    def units(self, paras: list[Para]) -> list[Grupo] | None:
        return self._run("(unidades)", lambda: segment_units_llm(
            paras, client=self.client, model=self.model))
