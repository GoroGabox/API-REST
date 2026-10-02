"""Figuras del libro → lecciones del plan (fase de plan, antes de redactar).

Extrae las figuras (``media.images``), las ubica en la lección y el PÁRRAFO del plan
donde aparecen en el libro, opcionalmente las describe con IA de visión, las guarda
en una carpeta junto al plan y deja en cada lección ``figuras: [{id, archivo, pagina,
parrafo, pie, alt, ancho, alto, mapeo, hash, ext}]``. El redactor las recibe e inserta
``{{figura:<id>}}`` en el texto; la Brújula las muestra para revisarlas antes.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from content_pipeline.media.images import (
    describe_figures_llm, extract_figures, map_figures_to_plan, save_figures,
)
from content_pipeline.processors.outline import chapter_files


def figures_source(source: str | Path | list) -> tuple[Any, bool]:
    """(fuente para ``extract_figures``, por_archivo). Carpeta → archivos de capítulo."""
    if isinstance(source, (list, tuple)):
        return [Path(p) for p in source], True
    p = Path(source)
    if p.is_dir():
        return chapter_files(p), True
    return p, False


def attach_figures_to_plan(plan: dict[str, Any], source: Any, out_dir: str | Path, *,
                           rel_to: str | Path, client: Any = None, model: str | None = None,
                           limit: int = 0) -> dict[str, Any]:
    """Agrega ``figuras`` a las lecciones del plan (in place) y devuelve el resumen."""
    src, por_archivo = figures_source(source)
    figs, descartes = extract_figures(src)
    if limit:
        figs = figs[:limit]
    asignadas, sin = map_figures_to_plan(figs, plan, por_archivo=por_archivo)

    lecciones = {lec.get("id"): lec for u in plan.get("unidades") or [] for lec in u.get("lecciones") or []}
    descripciones, ia = {}, None
    if client is not None:
        items = [(fig, lecciones.get(lid, {}).get("nombre", "")) for lid, lst in asignadas.items()
                 for fig, _m, _p in lst]
        descripciones = describe_figures_llm(items, client=client, model=model)
        meter = getattr(client, "meter", None)
        ia = meter.as_dict() if meter is not None and hasattr(meter, "as_dict") else None

    por_leccion, motivos = save_figures(asignadas, out_dir, rel_to=rel_to, descripciones=descripciones)
    for lid, lec in lecciones.items():
        if lid in por_leccion:
            lec["figuras"] = por_leccion[lid]
        else:
            lec.pop("figuras", None)

    descartadas = Counter(descartes)
    descartadas.update(motivos)
    return {
        "carpeta": Path(out_dir).as_posix(),
        "extraidas": len(figs),
        "asignadas": sum(len(v) for v in por_leccion.values()),
        "lecciones_con_figuras": len(por_leccion),
        "descartadas": dict(descartadas),
        "mapeo": dict(Counter(e["mapeo"] for v in por_leccion.values() for e in v)),
        "sin_parrafo": sum(1 for v in por_leccion.values() for e in v if e.get("parrafo") is None),
        "sin_leccion": [{"pagina": f.pagina, "ancla": (f.ancla_antes or f.ancla_despues)[:120]} for f in sin],
        "visuales_sin_figuras": [lec.get("nombre", "") for lid, lec in lecciones.items()
                                 if (lec.get("visual") or {}).get("nivel") == "alta" and lid not in por_leccion],
        "ia": ia,
    }
