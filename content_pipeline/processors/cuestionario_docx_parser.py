"""Cuestionario en Word (.docx) → ejercicios del banco, por capítulo/unidad.

Los cuestionarios que escriben los instructores no siguen un formato fijo: la
respuesta va como «(A)», « A», «C)» o «(a)»; las alternativas en una línea o
una por línea; Word se come el «A)» de las listas numeradas; una pregunta puede
traer varias filas de alternativas (variantes con la misma respuesta), números
como «36 (A)» / «(B)» y preguntas «(Extra)» sin número.

Por eso la estructura la propone el LLM (por bloques de líneas) y aquí se
valida de forma determinista: el enunciado y cada alternativa deben estar en el
documento, 2 a 6 alternativas y la letra de la respuesta debe existir. Lo que no
valida se reintenta una vez con el error; si sigue mal, queda ``diferir``.

Cada fila de alternativas es un ejercicio; las variantes de una misma pregunta
comparten ``grupo`` (un quiz nunca muestra dos del mismo grupo).
"""
from __future__ import annotations

import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

from content_pipeline.llm.client import LLMClient, default_model, parse_json_object

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_CAPITULO_RE = re.compile(r"^\s*CAP[IÍ]TULO\s*:?\s*(\d+)", re.IGNORECASE)
_LETRAS = ("a", "b", "c", "d", "e", "f")
_LINEAS_POR_BLOQUE = 70


# ---------------------------------------------------------------------------
# Lectura del .docx
# ---------------------------------------------------------------------------

def leer_lineas(path: str) -> list[str]:
    """Párrafos no vacíos del documento, en orden. Los de lista numerada de Word
    llevan el prefijo «• » (Word no guarda el «A)»/«1)» como texto)."""
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(_W + "body")
    lineas: list[str] = []
    for el in body.iter(_W + "p"):
        texto = "".join(t.text or "" for t in el.iter(_W + "t"))
        texto = texto.replace("\t", " ").rstrip()
        if not texto.strip():
            continue
        if el.find(f"{_W}pPr/{_W}numPr") is not None:
            texto = "• " + texto.strip()
        lineas.append(texto)
    return lineas


def dividir_capitulos(lineas: list[str]) -> dict[int, list[str]]:
    """{número de capítulo: líneas}. Lo anterior al primer «CAPITULO» se ignora."""
    capitulos: dict[int, list[str]] = {}
    actual: int | None = None
    for linea in lineas:
        m = _CAPITULO_RE.match(linea)
        if m:
            actual = int(m.group(1))
            capitulos.setdefault(actual, [])
            continue
        if actual is not None:
            capitulos[actual].append(linea)
    return capitulos


def _inicia_pregunta(linea: str) -> bool:
    s = linea.strip()
    return "¿" in s or bool(re.match(r"^\d+\s*[\).(]", s)) or s.lower().startswith("(extra")


def bloques(lineas: list[str], tam: int = _LINEAS_POR_BLOQUE) -> list[list[str]]:
    """Corta en bloques de ~``tam`` líneas, siempre al inicio de una pregunta."""
    out: list[list[str]] = []
    actual: list[str] = []
    for linea in lineas:
        if len(actual) >= tam and _inicia_pregunta(linea):
            out.append(actual)
            actual = []
        actual.append(linea)
    if actual:
        out.append(actual)
    return out


# ---------------------------------------------------------------------------
# Validación determinista
# ---------------------------------------------------------------------------

def norm(texto: str) -> str:
    """Minúsculas, sin tildes ni puntuación, espacios colapsados."""
    t = unicodedata.normalize("NFKD", str(texto or ""))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[^a-z0-9ñ%$]+", " ", t)
    return " ".join(t.split())


def validar_item(item: dict[str, Any], fuente_norm: str) -> list[str]:
    """Errores de un ítem estructurado (lista vacía = válido)."""
    errores: list[str] = []
    pregunta = (item.get("pregunta") or "").strip()
    if not pregunta:
        errores.append("sin enunciado")
    elif norm(pregunta) not in fuente_norm:
        errores.append(f"el enunciado no aparece en el documento: {pregunta[:60]!r}")
    variantes = item.get("variantes") or []
    if not variantes:
        errores.append("sin alternativas")
    respuesta = str(item.get("respuesta") or "").strip().lower()
    for i, v in enumerate(variantes, start=1):
        opciones = {k.lower(): (t or "").strip() for k, t in (v or {}).items() if (t or "").strip()}
        if not 2 <= len(opciones) <= 6:
            errores.append(f"variante {i}: {len(opciones)} alternativas (se esperan 2 a 6)")
        if any(k not in _LETRAS for k in opciones):
            errores.append(f"variante {i}: letras inválidas {sorted(opciones)}")
        if respuesta not in opciones:
            errores.append(f"variante {i}: la respuesta «{respuesta}» no es una alternativa")
        for k, t in opciones.items():
            if norm(t) and norm(t) not in fuente_norm:
                errores.append(f"variante {i}: la alternativa {k}) no aparece en el documento: {t[:50]!r}")
    return errores


# ---------------------------------------------------------------------------
# Estructuración con LLM
# ---------------------------------------------------------------------------

_SYSTEM = """Estructuras cuestionarios de examen teórico de conducción escritos a mano en Word.
Recibes líneas numeradas de UN capítulo. Devuelve SOLO JSON:
{"preguntas": [{"numero": "19", "pregunta": "...", "respuesta": "a", "variantes": [{"a": "...", "b": "...", "c": "..."}]}]}

Reglas:
- Copia el enunciado y las alternativas TEXTUALES del documento (no corrijas ortografía ni redacción). Quita solo los rótulos: el número de la pregunta, «A)», «B)», «C)» y la marca de respuesta («(A)», « A», «C)», «(a)» al final del enunciado).
- La respuesta es la letra marcada al final del enunciado, en minúscula.
- Cada fila de alternativas (A) … B) … C) …) es UNA variante. Si una pregunta trae varias filas, todas son variantes de la misma pregunta y comparten la respuesta.
- Las alternativas pueden venir en una línea o una por línea. Una línea que empieza con «• » es un ítem de lista de Word que perdió su «A)» o su número: su texto es la alternativa a) (o el enunciado si es una pregunta).
- «36 (A) ¿…?» y «(B) ¿…?» son preguntas distintas: numero «36A», «36B». «(Extra)» sin número: numero «extra-<n>».
- Ignora títulos de sección (p. ej. «SISTEMA DE FRENOS») y líneas que no sean preguntas ni alternativas.
- No inventes preguntas ni alternativas. Si una pregunta no tiene alternativas claras, omítela."""


def _render(lineas: list[str]) -> str:
    return "\n".join(f"L{i}: {l}" for i, l in enumerate(lineas, start=1))


def _pedir(cli: LLMClient, lineas: list[str], feedback: str = "") -> list[dict[str, Any]]:
    user = "Líneas del capítulo:\n\n" + _render(lineas)
    if feedback:
        user += "\n\nCorrige estos problemas de tu respuesta anterior (vuelve a entregar TODAS las preguntas del bloque):\n" + feedback
    raw = cli.complete(system=_SYSTEM, user=user, max_tokens=16000, temperature=0.0)
    data = parse_json_object(raw)
    preguntas = data.get("preguntas") if isinstance(data, dict) else None
    return preguntas if isinstance(preguntas, list) else []


@dataclass
class ResultadoCapitulo:
    capitulo: int
    preguntas: list[dict[str, Any]] = field(default_factory=list)
    diferidas: list[dict[str, Any]] = field(default_factory=list)


def estructurar_capitulo(capitulo: int, lineas: list[str], *, client: LLMClient | None = None) -> ResultadoCapitulo:
    """Estructura un capítulo bloque a bloque, con validación y un reintento."""
    cli = client or LLMClient(model=default_model())
    fuente_norm = norm(" ".join(lineas))
    res = ResultadoCapitulo(capitulo)
    for bloque in bloques(lineas):
        items = _pedir(cli, bloque)
        malos = {str(it.get("numero")): validar_item(it, fuente_norm) for it in items}
        malos = {k: v for k, v in malos.items() if v}
        if malos:
            feedback = "\n".join(f"- pregunta {k}: {'; '.join(v)}" for k, v in malos.items())
            reintento = _pedir(cli, bloque, feedback)
            if reintento:
                items = reintento
        for it in items:
            errores = validar_item(it, fuente_norm)
            (res.diferidas if errores else res.preguntas).append({**it, "errores": errores})
    return res


# ---------------------------------------------------------------------------
# Salida: un ejercicio por variante (formato de import_ejercicios)
# ---------------------------------------------------------------------------

def a_ejercicios(res: ResultadoCapitulo, *, prefijo_grupo: str, fuente: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it, diferir in [(p, False) for p in res.preguntas] + [(p, True) for p in res.diferidas]:
        numero = str(it.get("numero") or "").strip() or "s/n"
        grupo = f"{prefijo_grupo}-c{res.capitulo}-{numero}"
        respuesta = str(it.get("respuesta") or "").strip().lower()
        for i, v in enumerate(it.get("variantes") or [{}], start=1):
            opciones = {k.lower(): (t or "").strip() for k, t in (v or {}).items() if (t or "").strip()}
            out.append({
                "numero": f"c{res.capitulo}-{numero}-{i}",
                "pregunta": (it.get("pregunta") or "").strip(),
                "opciones": opciones,
                "respuestas": [respuesta] if respuesta in opciones else [],
                "multi": False,
                "explicacion": "",
                "tiene_imagen": False,
                "diferir": diferir,
                "motivo": "; ".join(it.get("errores") or []) or None,
                "unidad_orden": res.capitulo,
                "grupo": grupo,
                "fuente": fuente,
            })
    return out


def numeros_faltantes(res: ResultadoCapitulo) -> list[int]:
    """Números enteros ausentes entre el menor y el mayor encontrados (aviso)."""
    nums = set()
    for it in res.preguntas + res.diferidas:
        m = re.match(r"^(\d+)", str(it.get("numero") or ""))
        if m:
            nums.add(int(m.group(1)))
    if not nums:
        return []
    return [n for n in range(min(nums), max(nums) + 1) if n not in nums]
