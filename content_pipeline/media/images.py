"""Figuras del libro (PDF / Word) → mapeo a las lecciones del plan.

El pipeline de texto pierde lo que el libro enseña con imágenes (señales, tableros,
maniobras). Este módulo:

1. **Extrae figuras**:
   - PDF (PyMuPDF): por página, imágenes con área relevante; descarta logos/cabeceras
     (mismo xref en muchas páginas), divisorias de capítulo (imagen a página completa
     sin cuerpo) y duplicados; une imágenes contiguas (mosaicos, señal + rótulo) en
     UNA figura y la **renderiza recortada** (``get_pixmap(clip=…)``), lo que cubre
     igual imágenes inline, CMYK, con máscara o recortadas.
   - Word: recorre el documento en orden; cada ``a:blip`` (o ``v:imagedata``) se
     resuelve a ``word/media/*`` y queda ubicada EXACTAMENTE entre dos párrafos.
2. **Anclas**: texto vecino antes/después de la figura (y un pie corto si el libro
   lo trae) → sirven para ubicarla en la lección cuyo ``texto`` fuente las contiene.
3. **Mapeo** (``map_figures_to_plan``): por ancla de texto; respaldo por página (PDF)
   o por unidad del archivo (Word); si no hay evidencia queda ``sin_leccion``.
4. **Descripción opcional con IA** (``describe_figures_llm``, visión): pie + alt-text
   y descarte de imágenes decorativas. Best-effort.
"""
from __future__ import annotations

import base64
import hashlib
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from content_pipeline.processors.outline import _norm
from content_pipeline.processors.visual_pages import _IMG_MIN_AREA, _MIN_BODY_CHARS

# Un xref presente en más de esta fracción de páginas es logo / cabecera / fondo.
_REPEATED_RATIO = 0.3
# Distancia (pt) bajo la cual dos imágenes de la misma página son UNA figura.
_MERGE_GAP = 8.0
# Una figura que cubre esta fracción de la página en una página sin cuerpo = divisoria.
_DIVIDER_AREA = 0.7
_DPI = 170
_MAX_PNG_BYTES = 250_000
# Pie del libro: bloque corto pegado a la figura.
_CAPTION_MAX_WORDS = 25
_CAPTION_GAP = 20.0
# Anclas: bloques de cuerpo (no rótulos) y cuántos tomar a cada lado de la figura.
_ANCHOR_MIN_WORDS = 6
_ANCHOR_BLOCKS = 3
# Rótulo de una anotación (callout) que se incluye dentro de la figura.
_LABEL_MAX_WORDS = 12
# Word: imágenes más chicas que esto (px) son viñetas / íconos de formato.
_DOCX_MIN_PX = 40
_EMU_PER_PX = 9525
_WEB_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
# Ventanas de palabras para buscar un ancla en el texto de la lección.
_NEEDLE_WORDS = 8

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
_V = "{urn:schemas-microsoft-com:vml}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


@dataclass
class Figura:
    data: bytes
    ext: str
    ancho: int
    alto: int
    orden: int                       # posición global en el libro
    origen: str                      # pdf | docx
    pagina: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    archivo: int = 0                 # índice de archivo (carpeta de capítulos)
    ancla_antes: str = ""
    ancla_despues: str = ""
    pie_libro: str = ""
    hash: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.hash:
            self.hash = hashlib.sha1(self.data).hexdigest()

    @property
    def media_type(self) -> str:
        return "image/jpeg" if self.ext in ("jpg", "jpeg") else f"image/{self.ext}"


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _merge_rects(rects: list, gap: float = _MERGE_GAP) -> list:
    """Une rectángulos que se solapan o quedan a menos de ``gap`` pt."""
    import fitz

    out = [fitz.Rect(r) for r in rects]
    merged = True
    while merged:
        merged = False
        for i in range(len(out)):
            grown = fitz.Rect(out[i].x0 - gap, out[i].y0 - gap, out[i].x1 + gap, out[i].y1 + gap)
            for j in range(i + 1, len(out)):
                if grown.intersects(out[j]):
                    out[i] = out[i] | out[j]
                    del out[j]
                    merged = True
                    break
            if merged:
                break
    return sorted(out, key=lambda r: (round(r.y0), r.x0))


def _expand_with_annotations(rect, drawings: list, blocks: list[tuple], page_rect):
    """Amplía la figura con sus anotaciones vectoriales: trazos (flechas, líneas guía)
    que tocan la imagen y rótulos cortos (≤ ``_LABEL_MAX_WORDS``) pegados a ESOS trazos
    (callouts). Un texto pegado a la imagen sin trazo es el pie, y queda fuera. Sin
    esto, un tablero con rótulos queda recortado sin sus explicaciones."""
    import fitz

    region = fitz.Rect(rect)
    page_area = page_rect.width * page_rect.height or 1.0
    trazos: list = []
    for _ in range(2):                                   # trazo → rótulo → trazo encadenado
        grown = fitz.Rect(region.x0 - 4, region.y0 - 4, region.x1 + 4, region.y1 + 4)
        for d in drawings:
            if d.get_area() < page_area * 0.4 and grown.intersects(d) and not region.contains(d):
                region |= d
                trazos.append(d)
        for b in blocks:
            br = fitz.Rect(b[:4])
            cerca = fitz.Rect(br.x0 - 6, br.y0 - 6, br.x1 + 6, br.y1 + 6)
            if (len(b[4].split()) <= _LABEL_MAX_WORDS and not region.contains(br)
                    and any(cerca.intersects(t) for t in trazos)):
                region |= br
    return region & page_rect


def _ruido_key(text: str) -> str:
    """Clave de cabecera/pie de página: sin dígitos ("16 Libro del…" = "18 Libro del…")."""
    return re.sub(r"\d+", "#", _norm(text))


def _es_pie(text: str) -> bool:
    t = text.strip()
    return bool(t) and not t.isdigit() and not re.match(r"^([•·●▪◦\-–—*]|\d{1,2}[.)])\s", t)


def _pos_key(bbox) -> tuple:
    """Posición/tamaño redondeados: la misma banda o logo en muchas páginas."""
    return tuple(round(v / 4) for v in bbox)


def _inner_label(blocks: list[tuple], region, img_rects: list, ruido: set[str]) -> str:
    """Nombre dentro de la figura ampliada (p. ej. la tarjeta de una señal con su
    rótulo): 1-2 bloques cortos dentro de la región y fuera de las imágenes."""
    import fitz

    labels = []
    for b in blocks:
        br = fitz.Rect(b[:4])
        if not br.get_area() or (br & region).get_area() / br.get_area() < 0.5:
            continue
        if any(br.intersects(r) for r in img_rects) or _ruido_key(b[4]) in ruido or not _es_pie(b[4]):
            continue
        labels.append((br.y0, br.x0, _clean_block(b[4])))
    texto = " ".join(t for *_k, t in sorted(labels))
    return texto if 0 < len(labels) <= 2 and len(texto.split()) <= _CAPTION_MAX_WORDS else ""


def _clean_block(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("­", "")).strip()


def _pdf_anchors(blocks: list[tuple], rect, ruido: set[str]) -> tuple[str, str, str]:
    """(antes, después, pie) desde los bloques de texto de la página.

    Las anclas usan solo bloques de CUERPO (≥ ``_ANCHOR_MIN_WORDS`` palabras, no repetidos
    entre páginas como cabeceras/pies): hasta ``_ANCHOR_BLOCKS`` a cada lado, del más
    lejano al más cercano (antes) y del más cercano al más lejano (después). El pie es el
    bloque corto pegado bajo la figura, aunque no sea cuerpo.
    """
    import fitz

    fuera = []
    for b in blocks:
        br = fitz.Rect(b[:4])
        inter = (br & rect).get_area() if br.intersects(rect) else 0.0
        if br.get_area() and inter / br.get_area() > 0.5:
            continue                         # texto dentro de la figura (rótulos del dibujo)
        fuera.append((br, _clean_block(b[4])))
    arriba = sorted([(br, t) for br, t in fuera if br.y1 <= rect.y0 + 2 and t], key=lambda x: -x[0].y1)
    abajo = sorted([(br, t) for br, t in fuera if br.y0 >= rect.y1 - 2 and t], key=lambda x: x[0].y0)

    def cuerpo(t: str) -> bool:
        return len(t.split()) >= _ANCHOR_MIN_WORDS and _ruido_key(t) not in ruido

    antes = [t for _br, t in arriba if cuerpo(t)][:_ANCHOR_BLOCKS]
    despues = [t for _br, t in abajo if cuerpo(t)][:_ANCHOR_BLOCKS]
    pie = ""
    if abajo:
        br, t = abajo[0]
        if (br.y0 - rect.y1 <= _CAPTION_GAP and len(t.split()) <= _CAPTION_MAX_WORDS
                and _ruido_key(t) not in ruido and _es_pie(t)):
            pie = t
    return " ".join(reversed(antes)), " ".join(despues), pie


def extract_pdf_figures(path: str | Path, *, dpi: int = _DPI) -> tuple[list[Figura], Counter]:
    """Figuras relevantes del PDF (renderizadas) + conteo de descartes por motivo."""
    import fitz

    descartes: Counter = Counter()
    figs: list[Figura] = []
    seen: set[str] = set()
    with fitz.open(str(path)) as doc:
        n = len(doc)
        infos = {}
        xref_pages: dict[int, set[int]] = defaultdict(set)
        pos_pages: dict[tuple, set[int]] = defaultdict(set)   # inline (sin xref): por posición
        for pno, page in enumerate(doc, start=1):
            infos[pno] = page.get_image_info(xrefs=True)
            for i in infos[pno]:
                if i.get("xref"):
                    xref_pages[i["xref"]].add(pno)
                pos_pages[_pos_key(i["bbox"])].add(pno)
        umbral = max(3, _REPEATED_RATIO * n)
        repeated = {x for x, pgs in xref_pages.items() if n >= 4 and len(pgs) > umbral}
        repeated_pos = {k for k, pgs in pos_pages.items() if n >= 4 and len(pgs) > umbral}
        # Bloques de texto repetidos en ≥3 páginas = cabeceras / pies (no sirven de ancla).
        texto_pags: dict[str, set[int]] = defaultdict(set)
        for pno, page in enumerate(doc, start=1):
            if infos[pno]:
                for b in page.get_text("blocks"):
                    if len(b) > 6 and b[6] == 0 and b[4].strip():
                        texto_pags[_ruido_key(b[4])].add(pno)
        ruido = {t for t, pgs in texto_pags.items() if len(pgs) >= 3}

        for pno, page in enumerate(doc, start=1):
            area = page.rect.width * page.rect.height or 1.0
            rects = []
            for i in infos[pno]:
                r = fitz.Rect(i["bbox"]) & page.rect
                if r.is_empty or r.get_area() < area * _IMG_MIN_AREA:
                    descartes["pequena"] += 1
                    continue
                if i.get("xref") in repeated or _pos_key(i["bbox"]) in repeated_pos:
                    descartes["repetida"] += 1
                    continue
                rects.append(r)
            if not rects:
                continue
            chars = len(page.get_text("text").strip())
            blocks = [b for b in page.get_text("blocks") if len(b) > 6 and b[6] == 0 and b[4].strip()]
            drawings = [fitz.Rect(d["rect"]) for d in page.get_drawings() if d.get("rect")]
            regiones = []
            for rect in _merge_rects(rects):
                if rect.get_area() >= area * _DIVIDER_AREA and chars < _MIN_BODY_CHARS:
                    descartes["divisoria"] += 1
                    continue
                regiones.append(_expand_with_annotations(rect, drawings, blocks, page.rect))
            # Imágenes de una misma infografía terminan en la misma región: se unen.
            for rect in _merge_rects(regiones, gap=0):
                pix = page.get_pixmap(clip=rect, dpi=dpi)
                data, ext = pix.tobytes("png"), "png"
                if len(data) > _MAX_PNG_BYTES:
                    data, ext = pix.tobytes("jpg", jpg_quality=85), "jpg"
                h = hashlib.sha1(data).hexdigest()
                if h in seen:
                    descartes["duplicada"] += 1
                    continue
                seen.add(h)
                antes, despues, pie = _pdf_anchors(blocks, rect, ruido)
                pie = pie or _inner_label(blocks, rect, rects, ruido)
                figs.append(Figura(data=data, ext=ext, ancho=pix.width, alto=pix.height,
                                   orden=len(figs) + 1, origen="pdf", pagina=pno,
                                   bbox=tuple(round(v, 1) for v in rect), hash=h,
                                   ancla_antes=antes, ancla_despues=despues, pie_libro=pie))
    return figs, descartes


# ---------------------------------------------------------------------------
# Word
# ---------------------------------------------------------------------------
def extract_docx_figures(path: str | Path, *, archivo: int = 0, orden_inicial: int = 0) -> tuple[list[Figura], Counter]:
    """Figuras del .docx en orden de lectura, ancladas entre párrafos."""
    descartes: Counter = Counter()
    figs: list[Figura] = []
    seen: set[str] = set()
    with zipfile.ZipFile(str(path)) as z:
        rels: dict[str, str] = {}
        try:
            for rel in ET.fromstring(z.read("word/_rels/document.xml.rels")).iter(_PKG_REL + "Relationship"):
                rels[rel.get("Id", "")] = rel.get("Target", "")
        except KeyError:
            return figs, descartes
        root = ET.fromstring(z.read("word/document.xml"))
        ultimo_texto = ""
        pendientes: list[Figura] = []          # esperan el próximo párrafo con texto
        for p in root.iter(_W + "p"):
            text = re.sub(r"\s+", " ", "".join(t.text or "" for t in p.iter(_W + "t"))).strip()
            embeds = []
            for drawing in p.iter(_W + "drawing"):
                ext_el = next(drawing.iter(_WP + "extent"), None)
                size = ((int(ext_el.get("cx", 0)) // _EMU_PER_PX, int(ext_el.get("cy", 0)) // _EMU_PER_PX)
                        if ext_el is not None else (0, 0))
                for blip in drawing.iter(_A + "blip"):
                    embeds.append((blip.get(_R + "embed"), size))
            for im in p.iter(_V + "imagedata"):
                embeds.append((im.get(_R + "id"), (0, 0)))
            for rid, (w, h) in embeds:
                target = rels.get(rid or "", "")
                if not target:
                    continue
                name = target.lstrip("/")
                name = name if name.startswith("word/") else "word/" + name
                ext = Path(name).suffix.lower().lstrip(".")
                if ext not in _WEB_EXT:
                    descartes["formato"] += 1          # emf/wmf: no se ven en web
                    continue
                if w and h and (w < _DOCX_MIN_PX or h < _DOCX_MIN_PX):
                    descartes["pequena"] += 1
                    continue
                try:
                    data = z.read(name)
                except KeyError:
                    descartes["sin_archivo"] += 1
                    continue
                hsh = hashlib.sha1(data).hexdigest()
                if hsh in seen:
                    descartes["duplicada"] += 1
                    continue
                seen.add(hsh)
                fig = Figura(data=data, ext="jpg" if ext == "jpeg" else ext, ancho=w, alto=h,
                             orden=orden_inicial + len(figs) + 1, origen="docx", archivo=archivo,
                             ancla_antes=ultimo_texto, hash=hsh)
                figs.append(fig)
                pendientes.append(fig)
            if text:
                for fig in pendientes:
                    fig.ancla_despues = text
                    if len(text.split()) <= _CAPTION_MAX_WORDS and re.match(r"(figura|imagen|fig\.|foto)", text, re.I):
                        fig.pie_libro = text
                pendientes = []
                ultimo_texto = text
    return figs, descartes


def extract_figures(source: str | Path | list[str | Path]) -> tuple[list[Figura], Counter]:
    """Figuras de un PDF/DOCX o de una lista de archivos de capítulo (``archivo`` = índice)."""
    files = [Path(p) for p in source] if isinstance(source, (list, tuple)) else [Path(source)]
    figs: list[Figura] = []
    descartes: Counter = Counter()
    for idx, f in enumerate(files):
        if f.suffix.lower() == ".pdf":
            got, d = extract_pdf_figures(f)
            for g in got:
                g.archivo = idx
                g.orden = len(figs) + g.orden
        else:
            got, d = extract_docx_figures(f, archivo=idx, orden_inicial=len(figs))
        figs.extend(got)
        descartes.update(d)
    return figs, descartes


# ---------------------------------------------------------------------------
# Mapeo a lecciones del plan
# ---------------------------------------------------------------------------
def _needles(text: str, *, desde_final: bool) -> list[str]:
    """Ventanas de ~8 palabras normalizadas (las más cercanas a la figura primero)."""
    words = _norm(text).split()
    if len(words) < 4:
        return []
    if len(words) <= _NEEDLE_WORDS:
        return [" ".join(words)]
    step = max(2, _NEEDLE_WORDS // 2)
    starts = list(range(0, len(words) - _NEEDLE_WORDS + 1, step))
    if desde_final:
        starts = [len(words) - _NEEDLE_WORDS] + starts[::-1]
    else:
        starts = [0] + starts
    out, seen = [], set()
    for s in starts[:8]:
        nd = " ".join(words[s:s + _NEEDLE_WORDS])
        if nd not in seen:
            seen.add(nd)
            out.append(nd)
    return out


def _flat_lessons(plan: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for ui, u in enumerate(plan.get("unidades") or []):
        for lec in u.get("lecciones") or []:
            out.append({"unidad": ui, "leccion": lec, "norm": " " + _norm(lec.get("texto", "")) + " ",
                        "paginas": lec.get("paginas") or [0, 0]})
    return out


def _pick(cands: list[dict[str, Any]], fig: Figura) -> dict[str, Any]:
    if len(cands) == 1:
        return cands[0]
    if fig.pagina:
        en_pag = [c for c in cands if c["paginas"][0] <= fig.pagina <= c["paginas"][1]]
        if en_pag:
            return en_pag[0]
    mismo = [c for c in cands if c["unidad"] == fig.archivo]
    return (mismo or cands)[0]


def map_figures_to_plan(figs: list[Figura], plan: dict[str, Any], *, por_archivo: bool = False
                        ) -> tuple[dict[str, list[tuple[Figura, str]]], list[Figura]]:
    """``({lesson_id: [(figura, mapeo)]}, sin_leccion)``; mapeo = texto | pagina | unidad.

    ``por_archivo``: las figuras vienen de una carpeta de capítulos (``archivo`` = índice
    de unidad) → se busca primero en esa unidad y el respaldo es su primera lección.
    """
    lessons = _flat_lessons(plan)
    # Páginas informativas: los planes desde Word traen [1, 1] en todas las lecciones.
    paginas_utiles = len({tuple(l["paginas"]) for l in lessons}) > 1
    asignadas: dict[str, list[tuple[Figura, str]]] = defaultdict(list)
    sin: list[Figura] = []
    for fig in figs:
        pool = [l for l in lessons if l["unidad"] == fig.archivo] if por_archivo else lessons
        elegido, mapeo = None, ""
        for texto, desde_final in ((fig.ancla_antes, True), (fig.ancla_despues, False)):
            for nd in _needles(texto, desde_final=desde_final):
                cands = [l for l in pool if f" {nd} " in l["norm"]] or \
                        ([l for l in lessons if f" {nd} " in l["norm"]] if por_archivo else [])
                if cands:
                    elegido, mapeo = _pick(cands, fig), "texto"
                    break
            if elegido:
                break
        if elegido is None and fig.pagina and paginas_utiles:
            cands = [l for l in pool if l["paginas"][0] <= fig.pagina <= l["paginas"][1]]
            if 0 < len(cands) <= 3:
                elegido, mapeo = cands[0], "pagina"
        if elegido is None and por_archivo and pool:
            elegido, mapeo = pool[0], "unidad"
        if elegido is None:
            sin.append(fig)
            continue
        asignadas[elegido["leccion"].get("id", "")].append((fig, mapeo))
    for lst in asignadas.values():
        lst.sort(key=lambda x: x[0].orden)
    return dict(asignadas), sin


# ---------------------------------------------------------------------------
# Descripción con IA (visión) — opcional
# ---------------------------------------------------------------------------
_DESC_SYSTEM = (
    "Eres editor de un curso e-learning de conducción (español de Chile). Recibes una "
    "figura extraída del libro del curso, el título de la lección y el texto que la "
    "rodea en el libro. Decide si la figura aporta al aprendizaje (señales, tableros, "
    "maniobras, esquemas, tablas, fotos explicativas) o es decorativa (adorno, logo, "
    "fondo, foto genérica sin información). Describe SOLO lo que se ve: no inventes "
    "normas, cifras ni datos que no estén en la imagen.\n"
    'Responde SOLO un objeto JSON: {"relevante": true, "pie": "pie de figura de 4 a 15 '
    'palabras", "alt": "descripción breve para lectores de pantalla", "motivo": "por qué"}'
)


def describe_figures_llm(items: list[tuple[Figura, str]], *, client: Any, model: str,
                         ) -> dict[str, dict[str, Any]]:
    """{hash: {relevante, pie, alt, motivo}} por figura. Figuras que fallan no aparecen."""
    from content_pipeline.llm.client import parse_json_object

    out: dict[str, dict[str, Any]] = {}
    for fig, leccion in items:
        contexto = " · ".join(t for t in (fig.ancla_antes[-300:], fig.pie_libro, fig.ancla_despues[:300]) if t)
        user = [
            {"type": "image", "source": {"type": "base64", "media_type": fig.media_type,
                                         "data": base64.b64encode(fig.data).decode("ascii")}},
            {"type": "text", "text": f"Lección: {leccion}\nTexto cercano en el libro: {contexto or '(sin texto)'}"},
        ]
        try:
            raw = client.complete(system=_DESC_SYSTEM, user=user, max_tokens=1500, model=model,
                                  temperature=0.0)
            data = parse_json_object(raw)
        except Exception:  # noqa: BLE001 — best-effort: la figura se conserva sin descripción IA
            continue
        out[fig.hash] = {
            "relevante": data.get("relevante") is not False,
            "pie": " ".join(str(data.get("pie") or "").split())[:200],
            "alt": " ".join(str(data.get("alt") or "").split())[:300],
            "motivo": " ".join(str(data.get("motivo") or "").split())[:200],
        }
    return out
