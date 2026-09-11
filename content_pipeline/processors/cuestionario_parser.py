"""Parser del «Cuestionario del nuevo conductor» (PDF) → ejercicios estructurados.

El PDF trae, en este orden:
  - Preguntas numeradas ``N.-`` con enunciado, una directiva ``Marque ...`` y
    opciones ``a) ... f)`` (cada letra en su propia línea, el texto debajo).
  - Una clave de respuestas **compacta** (``N. x)``) y luego una **detallada**
    con explicaciones opcionales (``N. x)`` seguido de una o más líneas de texto).

Algunas preguntas dependen de una imagen (señales de tránsito): su enunciado es
deíctico (``¿Qué significa esta señal?``). Esas se marcan ``tiene_imagen=True``
para diferirlas (Fase 3) — no se pueden responder solo con texto.

Uso programático::

    from content_pipeline.processors.cuestionario_parser import parse_cuestionario
    ejercicios, stats = parse_cuestionario("Cuestionario ... CLASE B.pdf")

Cada ejercicio: ``{numero, pregunta, opciones:{a..f}, respuestas:[keys],
multi:bool, explicacion, tiene_imagen:bool}``. La resolución a texto de opción
correcta y el mapeo a modelos ocurre en el importador, no aquí.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Páginas (1-based) — el layout del PDF es fijo.
PAGINAS_PREGUNTAS = range(1, 42)      # 1..41
PAGINAS_CLAVE_DETALLADA = range(45, 49)  # 45..48 (con explicaciones)
PAGINAS_CLAVE_COMPACTA = range(42, 46)   # 42..45 (fallback)

_NUM_PREGUNTA = re.compile(r"^\s*(\d+)\.-\s*(.*)$")
_LETRA_SOLA = re.compile(r"^\s*([a-f])\)\s*(.*)$")
_MARQUE = re.compile(r"marque\b", re.IGNORECASE)
# Una entrada de la clave: 'N. x)' con posibles letras extra 'a), b) y c)'.
_CLAVE_ENTRY = re.compile(r"(\d+)\.\s*((?:[a-f]\)[\s,y]*)+)")
# Enunciados que delatan dependencia de imagen (deícticos).
_DEICTICO = re.compile(
    r"esta\s+se[ñn]al|esta\s+luz|esta\s+figura|este\s+dibujo|siguiente\s+se[ñn]al|"
    r"indicad[oa]\s+con\s+la\s+flecha|la\s+se[ñn]al\s+que|esta\s+se[ñn]alizaci[oó]n|"
    r"qu[eé]\s+significa\s+esto",
    re.IGNORECASE,
)


@dataclass
class ParseStats:
    total: int = 0
    single: int = 0
    multi: int = 0
    con_imagen: int = 0
    sin_respuesta: int = 0
    sin_opciones: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "single": self.single,
            "multi": self.multi,
            "con_imagen": self.con_imagen,
            "sin_respuesta": self.sin_respuesta,
            "sin_opciones": self.sin_opciones,
            "warnings": self.warnings,
        }


def _import_fitz():
    try:
        import fitz  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "PyMuPDF (fitz) no está instalado. Instala: env/Scripts/pip.exe install pymupdf"
        ) from exc
    return fitz


def _directiva_es_multi(directiva: str) -> bool:
    d = directiva.lower()
    # 'marque una respuesta' = single; todo lo demás (dos/tres/'la o las'/
    # 'respuesta(s) correcta(s)') se trata como multi.
    if re.search(r"marque\s+una\s+respuesta", d):
        return False
    if re.search(r"marque\s+(dos|tres|cuatro|las|la\s+o\s+las)", d):
        return True
    # 'respuesta(s)' con paréntesis o plural → multi permitido.
    if "respuesta(s)" in d or "correcta(s)" in d or "respuestas" in d:
        return True
    return False


def _parse_preguntas(fitz, doc) -> dict[int, dict[str, Any]]:
    """Extrae enunciado + opciones + flag multi/imagen por número de pregunta.

    El estado de parseo (`current`/`state`/`capturing_opt`) se mantiene entre
    páginas: hay preguntas cuyas últimas opciones continúan en la página
    siguiente (p.ej. #69, #78, #190), y reiniciar por página las perdería.
    """
    preguntas: dict[int, dict[str, Any]] = {}

    current: dict[str, Any] | None = None
    capturing_opt: str | None = None
    state = "pregunta"  # pregunta -> (marque) -> opciones
    # Detección de imagen decoplada del flush: se aplica al final para no
    # depender de si la pregunta ya fue volcada a `preguntas`.
    imagen_por_numero: set[int] = set()

    def _flush(cur):
        if cur is None:
            return
        cur["pregunta"] = _norm(cur["pregunta"])
        for k in list(cur["opciones"].keys()):
            cur["opciones"][k] = _norm(cur["opciones"][k])
            if not cur["opciones"][k]:
                del cur["opciones"][k]
        preguntas[cur["numero"]] = cur

    for page_idx in (p - 1 for p in PAGINAS_PREGUNTAS):
        if page_idx >= doc.page_count:
            break
        page = doc[page_idx]
        text = page.get_text()
        # ¿La página tiene imágenes "de contenido"? (el header/logo se repite en
        # p1/p5; se filtra quedándose con imágenes dentro del cuerpo).
        img_rects = []
        for info in page.get_image_info():
            bbox = info.get("bbox")
            if bbox:
                img_rects.append(bbox)

        lines = text.splitlines()
        # Localiza el offset vertical de cada número de pregunta y cada bloque de
        # imagen para decidir tiene_imagen por-pregunta (por cercanía vertical).
        blocks = page.get_text("dict").get("blocks", [])
        # y de cada "N.-" en la página
        num_y: list[tuple[int, float]] = []
        for b in blocks:
            for line in b.get("lines", []):
                span_text = "".join(s.get("text", "") for s in line.get("spans", []))
                m = _NUM_PREGUNTA.match(span_text)
                if m:
                    y = line.get("bbox", [0, 0, 0, 0])[1]
                    num_y.append((int(m.group(1)), y))
        num_y.sort(key=lambda t: t[1])
        img_ys = sorted(r[1] for r in img_rects) if img_rects else []

        # Parseo lineal del texto de la página (el estado persiste entre páginas).
        i = 0
        while i < len(lines):
            raw = lines[i]
            line = raw.strip()
            mnum = _NUM_PREGUNTA.match(raw)
            if mnum:
                _flush(current)
                numero = int(mnum.group(1))
                current = {
                    "numero": numero,
                    "pregunta": mnum.group(2).strip(),
                    "opciones": {},
                    "directiva": "",
                    "multi": False,
                    "tiene_imagen": False,
                    "pagina": page_idx + 1,
                }
                capturing_opt = None
                state = "pregunta"
                i += 1
                continue
            if current is None:
                i += 1
                continue
            if _MARQUE.search(line):
                current["directiva"] = line
                current["multi"] = _directiva_es_multi(line)
                state = "opciones"
                capturing_opt = None
                i += 1
                continue
            mopt = _LETRA_SOLA.match(raw)
            if state == "opciones" and mopt:
                capturing_opt = mopt.group(1)
                current["opciones"][capturing_opt] = mopt.group(2).strip()
                i += 1
                continue
            # Línea de continuación
            if state == "opciones" and capturing_opt:
                if line:
                    current["opciones"].setdefault(capturing_opt, "")
                    current["opciones"][capturing_opt] += " " + line
            elif state == "pregunta" and line:
                current["pregunta"] += " " + line
            i += 1

        # tiene_imagen por-pregunta: una imagen de cuerpo cuyo y cae entre el
        # y de esta pregunta y el de la siguiente (en la misma página).
        for idx, (numero, y0) in enumerate(num_y):
            y1 = num_y[idx + 1][1] if idx + 1 < len(num_y) else float("inf")
            if any(y0 - 5 <= iy < y1 for iy in img_ys):
                imagen_por_numero.add(numero)

    _flush(current)

    # Aplica flags de imagen (geometría + deíctico) tras volcar todas las preguntas.
    for numero, p in preguntas.items():
        if numero in imagen_por_numero or _DEICTICO.search(p["pregunta"]):
            p["tiene_imagen"] = True

    return preguntas


def _parse_clave(doc, paginas) -> dict[int, dict[str, Any]]:
    """Extrae {numero: {respuestas:[keys], explicacion:str}} de una clave."""
    clave: dict[int, dict[str, Any]] = {}
    texto = "\n".join(
        doc[p - 1].get_text() for p in paginas if p - 1 < doc.page_count
    )
    # Posiciones de cada entrada para delimitar la explicación (texto entre
    # una entrada y la siguiente).
    matches = list(_CLAVE_ENTRY.finditer(texto))
    for idx, m in enumerate(matches):
        numero = int(m.group(1))
        letras = re.findall(r"([a-f])\)", m.group(2))
        fin = matches[idx + 1].start() if idx + 1 < len(matches) else len(texto)
        explicacion = _norm(texto[m.end():fin])
        # La explicación no debe arrastrar números de la siguiente entrada ni ruido.
        clave[numero] = {
            "respuestas": letras,
            "explicacion": explicacion,
        }
    return clave


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def parse_cuestionario(pdf_path: str | Path) -> tuple[list[dict[str, Any]], ParseStats]:
    fitz = _import_fitz()
    doc = fitz.open(str(pdf_path))

    preguntas = _parse_preguntas(fitz, doc)
    clave = _parse_clave(doc, PAGINAS_CLAVE_DETALLADA)
    clave_compacta = _parse_clave(doc, PAGINAS_CLAVE_COMPACTA)

    stats = ParseStats()
    ejercicios: list[dict[str, Any]] = []

    for numero in sorted(preguntas):
        p = preguntas[numero]
        ans = clave.get(numero) or clave_compacta.get(numero)
        respuestas = ans["respuestas"] if ans else []
        explicacion = ans.get("explicacion", "") if ans else ""
        # Solo letras que existan como opción.
        respuestas = [r for r in respuestas if r in p["opciones"]]

        if not p["opciones"]:
            stats.sin_opciones += 1
            stats.warnings.append(f"#{numero}: sin opciones parseadas")
        if not respuestas:
            stats.sin_respuesta += 1
            stats.warnings.append(f"#{numero}: sin respuesta en la clave")

        # Motivo de diferimiento (Fase 3 / revisión manual): imagen, o datos
        # insuficientes para importar como ejercicio de texto.
        motivo = None
        if p["tiene_imagen"]:
            motivo = "imagen"
        elif not p["opciones"]:
            motivo = "sin_opciones"
        elif not respuestas:
            motivo = "sin_respuesta"

        ejercicios.append({
            "numero": numero,
            "pregunta": p["pregunta"],
            "opciones": p["opciones"],
            "respuestas": respuestas,
            "multi": p["multi"],
            "explicacion": explicacion,
            "tiene_imagen": p["tiene_imagen"],
            "diferir": motivo is not None,
            "motivo": motivo,
            "pagina": p["pagina"],
        })
        stats.total += 1
        if p["multi"]:
            stats.multi += 1
        else:
            stats.single += 1
        if p["tiene_imagen"]:
            stats.con_imagen += 1

    return ejercicios, stats
