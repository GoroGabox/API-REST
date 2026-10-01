"""Detección de lecciones cuya fuente es mayormente GRÁFICA (figuras, señales, tablas).

El pipeline trabaja sobre el texto extraído del libro: lo que el libro enseña con
imágenes (señales de tránsito, tableros, esquemas de maniobras) no llega a la
lección. Una lección redactada solo con los rótulos de esas figuras puede quedar
incompleta o engañosa, así que se marca para REVISIÓN HUMANA contra el libro.

Señal calibrada con el manual Clase B: las páginas de señales tienen 10-25
imágenes y la mitad de texto o menos que una página normal; la cobertura por área
engaña (los íconos son pequeños) y las páginas divisorias de capítulo son un
dibujo a página completa sin cuerpo de texto (se ignoran).
"""
from __future__ import annotations

import re
import statistics
from pathlib import Path
from typing import Any

# Una imagen cuenta si ocupa al menos el 0,4% de la página (descarta viñetas y logos).
_IMG_MIN_AREA = 0.004
# Páginas con menos caracteres que esto no tienen cuerpo (portadas, divisorias).
_MIN_BODY_CHARS = 200
# Fracción de páginas visuales de una lección para marcarla con prioridad alta.
_ALTA_RATIO = 0.3

_REF_RE = re.compile(
    r"\b(?:ver|observa|mira|revisa)\s+(?:la\s+|el\s+|las\s+|los\s+)?"
    r"(?:imagen|imágenes|figura|foto(?:grafía)?|ilustración|tabla|esquema|diagrama|gráfico)"
    r"|\b(?:imagen|figura|ilustración|tabla|foto)\s+(?:superior|inferior|siguiente|anterior|adjunta|de\s+abajo|de\s+arriba)"
    r"|\bsiguiente\s+(?:imagen|figura|tabla|ilustración|esquema)",
    re.IGNORECASE,
)


def pdf_page_visuals(path: str | Path) -> dict[int, dict[str, float]]:
    """Por página (1-based): nº de imágenes relevantes, fracción de área e caracteres de texto."""
    import fitz  # PyMuPDF

    stats: dict[int, dict[str, float]] = {}
    with fitz.open(str(path)) as doc:
        for pno, page in enumerate(doc, start=1):
            area = page.rect.width * page.rect.height or 1.0
            big = [(fitz.Rect(i["bbox"]) & page.rect).get_area() for i in page.get_image_info()]
            big = [a for a in big if a > area * _IMG_MIN_AREA]
            stats[pno] = {
                "imagenes": len(big),
                "area_imagen": round(min(1.0, sum(big) / area), 3),
                "caracteres": len(page.get_text("text").strip()),
            }
    return stats


def classify_pages(stats: dict[int, dict[str, float]]) -> dict[int, str]:
    """``visual`` (la página se explica con imágenes) o ``figuras`` (tiene figuras relevantes)."""
    body = [s["caracteres"] for s in stats.values() if s["caracteres"] > _MIN_BODY_CHARS]
    if not body:
        return {}
    med = statistics.median(body)
    kinds: dict[int, str] = {}
    for p, s in stats.items():
        n, area, chars = s["imagenes"], s["area_imagen"], s["caracteres"]
        if chars <= _MIN_BODY_CHARS:
            continue                                   # portada/divisoria: sin cuerpo
        if (n >= 4 and chars < 0.6 * med) or area >= 0.25 or (n >= 2 and chars < 0.45 * med):
            kinds[p] = "visual"
        elif n >= 3 or area >= 0.12:
            kinds[p] = "figuras"
    return kinds


def image_references(texto: str) -> int:
    """Referencias del texto a figuras que la lección no mostrará ("ver imagen superior")."""
    return len(_REF_RE.findall(texto or ""))


def _span(pages: list[int]) -> str:
    if not pages:
        return ""
    return f"{pages[0]}" if len(pages) == 1 else f"{pages[0]}–{pages[-1]}"


def lesson_visual(paginas: list[int] | None, texto: str,
                  kinds: dict[int, str], stats: dict[int, dict[str, float]]) -> dict[str, Any] | None:
    """Marca de revisión visual de una lección (None = no requiere)."""
    refs = image_references(texto)
    p0, p1 = (paginas or [0, 0])[:2] if paginas else (0, 0)
    pages = list(range(int(p0), int(p1 or p0) + 1)) if p0 else []
    vis = [p for p in pages if kinds.get(p) == "visual"]
    fig = [p for p in pages if kinds.get(p) == "figuras"]
    imgs = int(sum(stats.get(p, {}).get("imagenes", 0) for p in pages))
    if vis and len(vis) / max(1, len(pages)) >= _ALTA_RATIO:
        nivel = "alta"
        motivo = (f"Las páginas {_span(vis)} son mayormente gráficas ({imgs} imágenes y poco texto): "
                  "lo que muestran las figuras no llega a la lección. Revisarla contra el libro.")
    elif vis or (fig and refs) or refs >= 2:
        nivel = "media"
        partes = []
        if vis or fig:
            partes.append(f"figuras en págs. {_span(sorted(vis + fig))}")
        if refs:
            partes.append(f"{refs} referencia(s) a imágenes en el texto")
        motivo = "Verificar que la lección no dependa de imágenes: " + " y ".join(partes) + "."
    else:
        return None
    return {"nivel": nivel, "paginas_visuales": vis, "paginas_con_figuras": fig,
            "imagenes": imgs, "referencias": refs, "motivo": motivo}


def annotate_plan_visuals(plan: dict[str, Any], source: Any) -> dict[str, int]:
    """Agrega ``visual`` a las lecciones del plan que requieren revisión visual (in place).

    Con un PDF único se usan las estadísticas por página; con carpetas o DOCX (sin
    páginas confiables) solo las referencias a imágenes del texto.
    """
    stats: dict[int, dict[str, float]] = {}
    src = Path(source) if isinstance(source, (str, Path)) else None
    if src is not None and src.is_file() and src.suffix.lower() == ".pdf":
        try:
            stats = pdf_page_visuals(src)
        except Exception:  # noqa: BLE001 — la marca visual es best-effort; nunca tumba el plan
            stats = {}
    kinds = classify_pages(stats)
    resumen = {"alta": 0, "media": 0}
    for unidad in plan.get("unidades", []):
        for lec in unidad.get("lecciones", []):
            v = lesson_visual(lec.get("paginas"), lec.get("texto", ""), kinds, stats)
            if v:
                lec["visual"] = v
                resumen[v["nivel"]] += 1
            else:
                lec.pop("visual", None)
    return resumen
