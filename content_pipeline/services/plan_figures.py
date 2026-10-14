"""Figuras del libro → lecciones del plan (fase de plan, antes de redactar).

Extrae las figuras (``media.images``), las ubica en la lección y el PÁRRAFO del plan
donde aparecen en el libro, opcionalmente las describe con IA de visión, las guarda
en una carpeta junto al plan y deja en cada lección ``figuras: [{id, archivo, pagina,
parrafo, pie, alt, ancho, alto, mapeo, hash, ext}]``. El redactor las recibe e inserta
``{{figura:<id>}}`` en el texto; la Brújula las muestra para revisarlas antes.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from content_pipeline.llm.client import parse_json_object
from content_pipeline.media.images import (
    Figura, describe_figures_llm, extract_figures, map_figures_to_plan, save_figures,
)
from content_pipeline.processors.outline import _norm, build_outline, chapter_files
from content_pipeline.review.ids import ensure_plan_ids
from content_pipeline.services.plan_structure import _complete
from content_pipeline.taxonomy import resolve as resolve_categoria


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



# ---------------------------------------------------------------------------
# Figuras de OTRO documento → plan ya revisado (p. ej. libro oficial → guía en Word)
# ---------------------------------------------------------------------------
# El plan revisado sale de una guía sin imágenes; las figuras están en el libro oficial.
# El texto vecino de la figura (libro) no coincide con el de la lección (guía, un
# resumen), así que el mapeo es: capítulo del libro → unidad del plan por NOMBRE, y
# dentro de la unidad una IA elige lección y párrafo (o ninguna). El plan no cambia:
# solo se agregan ``figuras`` y, al final, el anexo de señales.
_MAP_BATCH = 40
# Densidad: más figuras que esto tras un mismo párrafo (o dentro de una lección) saturan el
# texto; el exceso pasa a la galería de la lección (`en_texto: false`), en orden del libro.
MAX_POR_PARRAFO = 3
MAX_EN_TEXTO = 12
_ANEXO_NOMBRE = "Anexo: Señales de tránsito"

_MAP_SYSTEM = (
    "Eres editor de un curso e-learning de conducción (español de Chile). Recibes las "
    "LECCIONES de una unidad (cada una con sus párrafos numerados) y una lista de FIGURAS "
    "tomadas del libro oficial sobre el mismo capítulo (pie, descripción y el texto que las "
    "rodea en el libro). Decide en qué lección y tras qué párrafo va cada figura.\n"
    "- Asigna una figura solo si la lección TRATA lo que la figura muestra; ubícala tras el "
    "párrafo que habla de eso. Si ninguna lección lo trata, usa \"leccion\": null.\n"
    "- No repartas por repartir: una figura sin relación clara queda en null.\n"
    "{estricto}"
    "Responde SOLO un objeto JSON, sin texto adicional:\n"
    '{"asignaciones": [{"figura": "F1", "leccion": 0, "parrafo": 3, "motivo": "breve"}, '
    '{"figura": "F2", "leccion": null}]}\n'
    "leccion = número de la lección; parrafo = número del párrafo TRAS el cual va (-1 = al "
    "inicio de la lección). Incluye todas las figuras."
)
_MAP_ESTRICTO = ("- Estas figuras son tarjetas de señales de un anexo: asigna una SOLO si el texto "
                 "de la lección NOMBRA esa señal (o su significado exacto); el resto va en null.\n")


def _paras(texto: str) -> list[str]:
    return [x.strip() for x in re.split(r"\n\s*\n", str(texto or "")) if x.strip()]


def _fig_line(k: int, fig: Figura, desc: dict[str, Any]) -> str:
    pie = desc.get("pie") or fig.pie_libro
    partes = [f"pie: «{pie}»" if pie else "", f"muestra: «{desc['alt']}»" if desc.get("alt") else ""]
    ctx = " ".join((fig.ancla_antes[-220:] + " … " + fig.ancla_despues[:220]).split())
    if ctx.strip(" …"):
        partes.append(f"texto del libro: «{ctx}»")
    return f"F{k}: " + "; ".join(x for x in partes if x)


def map_figures_llm(unidad: dict[str, Any], figs: list[Figura], descripciones: dict[str, dict[str, Any]], *,
                    client: Any, model: str, estricto: bool = False,
                    ) -> dict[str, tuple[int, int | None, str]]:
    """``{hash: (indice_leccion, parrafo, motivo)}`` para las figuras que la IA ubicó.

    Las que no aparecen en el resultado no corresponden a ninguna lección (o la IA
    falló en su lote: nunca se asignan a ciegas).
    """
    lecciones = unidad.get("lecciones") or []
    if not lecciones or not figs:
        return {}
    n_paras = [len(_paras(lec.get("texto", ""))) for lec in lecciones]
    bloque = "\n\n".join(
        f"LECCIÓN {i}: {lec.get('nombre', '')}\n" + "\n".join(f"[{j}] {t}" for j, t in enumerate(_paras(lec.get("texto", ""))))
        for i, lec in enumerate(lecciones))
    system = _MAP_SYSTEM.replace("{estricto}", _MAP_ESTRICTO if estricto else "")
    out: dict[str, tuple[int, int | None, str]] = {}
    for start in range(0, len(figs), _MAP_BATCH):
        lote = figs[start:start + _MAP_BATCH]
        user = (f"Unidad: {unidad.get('nombre', '')}\n\n{bloque}\n\nFIGURAS:\n"
                + "\n".join(_fig_line(k, f, descripciones.get(f.hash) or {}) for k, f in enumerate(lote, start=1)))
        error = None
        for _ in range(2):
            prompt = user if error is None else f"{user}\n\nTu respuesta anterior era inválida: {error}. Corrígela."
            # Un fallo de la LLAMADA (clave inválida, red, cuota) se propaga: seguir daría un
            # plan sin figuras que parece correcto. Solo la respuesta inválida se reintenta.
            raw, truncada = _complete(client, system=system, user=prompt, max_tokens=6000, model=model)
            if truncada:
                error = "la respuesta quedó truncada; responde solo el JSON"
                continue
            try:
                items = parse_json_object(raw).get("asignaciones")
            except Exception as exc:  # noqa: BLE001 — JSON roto: se reintenta una vez
                error = f"no se pudo leer el JSON ({exc})"
                continue
            parcial, error = _validar_asignaciones(items, lote, n_paras)
            if error is None:
                out.update(parcial)
                break
    return out


def _validar_asignaciones(items: Any, lote: list[Figura], n_paras: list[int]):
    if not isinstance(items, list):
        return {}, "falta la lista 'asignaciones'"
    out: dict[str, tuple[int, int | None, str]] = {}
    for it in items:
        if not isinstance(it, dict):
            return {}, "cada asignación debe ser un objeto"
        m = re.fullmatch(r"F(\d+)", str(it.get("figura", "")).strip())
        if not m or not 1 <= int(m.group(1)) <= len(lote):
            return {}, f"figura desconocida: {it.get('figura')}"
        if it.get("leccion") is None:
            continue
        try:
            li = int(it["leccion"])
            par = None if it.get("parrafo") is None else int(it["parrafo"])
        except (TypeError, ValueError):
            return {}, f"leccion/parrafo no numéricos en {it.get('figura')}"
        if not 0 <= li < len(n_paras):
            return {}, f"la lección {li} no existe (hay {len(n_paras)})"
        if par is not None and not -1 <= par < n_paras[li]:
            return {}, f"el párrafo {par} no existe en la lección {li} (tiene {n_paras[li]})"
        out[lote[int(m.group(1)) - 1].hash] = (li, par, " ".join(str(it.get("motivo") or "").split())[:160])
    return out, None


_STOP = {"de", "del", "la", "las", "los", "el", "en", "y", "a", "al", "e", "o", "un", "una"}


def _tokens(nombre: str) -> set[str]:
    return {t for t in _norm(nombre).split() if t not in _STOP}


def _match_chapters(capitulos: list[str], unidades: list[str], minimo: float = 0.6) -> dict[int, int | None]:
    """{capítulo: unidad | None} por nombre: mismas palabras de contenido (Jaccard ≥ ``minimo``),
    así "Los siniestros de tránsito" = "LOS SINIESTROS DEL TRANSITO". Cada unidad se usa una vez."""
    pares = []
    for ci, cap in enumerate(capitulos):
        a = _tokens(cap)
        for ui, uni in enumerate(unidades):
            b = _tokens(uni)
            if a and b:
                pares.append((len(a & b) / len(a | b), ci, ui))
    out: dict[int, int | None] = {ci: None for ci in range(len(capitulos))}
    usadas: set[int] = set()
    for score, ci, ui in sorted(pares, reverse=True):
        if score >= minimo and out[ci] is None and ui not in usadas:
            out[ci] = ui
            usadas.add(ui)
    return out


def _chapter_of(chapters: list, pagina: int | None) -> int | None:
    for i, ch in enumerate(chapters):
        if pagina and ch.page_start <= pagina <= ch.page_end:
            return i
    return None


def attach_figures_cross(plan: dict[str, Any], source: str | Path, out_dir: str | Path, *,
                         rel_to: str | Path, map_client: Any, map_model: str,
                         vision_client: Any = None, vision_model: str | None = None,
                         anexo: bool = True, limit: int = 0) -> dict[str, Any]:
    """Agrega al plan (in place) las figuras de OTRO documento. Devuelve el resumen.

    No toca unidades, lecciones, textos ni ids: solo ``figuras`` por lección y, si el
    documento trae un capítulo de señales sin unidad en el plan, la unidad anexo.
    """
    figs, descartes = extract_figures(Path(source))
    if limit:
        figs = figs[:limit]
    chapters = build_outline(Path(source))

    # Re-ejecución: fuera las figuras y el anexo de una corrida anterior.
    plan["unidades"] = [u for u in plan.get("unidades") or []
                        if not all(lec.get("anexo") for lec in u.get("lecciones") or [{}])]
    unidades = plan["unidades"]
    for u in unidades:
        for lec in u.get("lecciones") or []:
            lec.pop("figuras", None)

    cap_unidad = _match_chapters([ch.titulo for ch in chapters], [u.get("nombre", "") for u in unidades])
    cap_senales = next((ci for ci, ch in enumerate(chapters)
                        if cap_unidad[ci] is None and "senales" in _norm(ch.titulo)), None)

    por_cap: dict[int, list[Figura]] = {}
    fuera: Counter = Counter()
    for f in figs:
        ci = _chapter_of(chapters, f.pagina)
        if ci is None:
            fuera["sin_capitulo"] += 1
        elif cap_unidad[ci] is None and ci != cap_senales:
            fuera[f"capitulo_sin_unidad: {chapters[ci].titulo}"] += 1
        else:
            por_cap.setdefault(ci, []).append(f)

    # Visión (opcional): pie/alt + descarte de decorativas, antes de ubicar.
    usables = [f for lst in por_cap.values() for f in lst]
    # Caché por hash junto a las figuras: re-correr (o reintentar tras un fallo) no vuelve a
    # pagar la visión de las figuras ya descritas.
    cache = Path(out_dir) / "descripciones.json"
    descripciones: dict[str, dict[str, Any]] = {}
    if cache.exists():
        try:
            descripciones = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            descripciones = {}
    if vision_client is not None:
        pendientes = [f for f in usables if f.hash not in descripciones]
        if pendientes:
            descripciones.update(describe_figures_llm(
                [(f, chapters[_chapter_of(chapters, f.pagina)].titulo) for f in pendientes],
                client=vision_client, model=vision_model))
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(descripciones, ensure_ascii=False, indent=1), encoding="utf-8")
    relevante = lambda f: (descripciones.get(f.hash) or {}).get("relevante", True)  # noqa: E731

    asignadas: dict[str, list[tuple[Figura, str, int | None]]] = {}
    extra: dict[tuple[str, str], dict[str, Any]] = {}         # (lesson_id, hash) → {en_texto, motivo}
    sin: list[Figura] = []

    def asignar(lec: dict[str, Any], f: Figura, mapeo: str, par: int | None, motivo: str = "", en_texto: bool = True):
        asignadas.setdefault(lec["id"], []).append((f, mapeo, par))
        extra[(lec["id"], f.hash)] = {"en_texto": en_texto, "motivo": motivo}

    for ci, lst in por_cap.items():
        ui = cap_unidad[ci]
        if ui is None:
            continue
        unidad = unidades[ui]
        lst = [f for f in lst if relevante(f)]
        por_texto, resto = map_figures_to_plan(lst, {"unidades": [unidad]})
        por_id = {lec["id"]: lec for lec in unidad["lecciones"]}
        for lid, items in por_texto.items():
            for f, mapeo, par in items:
                if mapeo == "texto":
                    asignar(por_id[lid], f, "texto", par)
                else:
                    resto.append(f)
        resto.sort(key=lambda f: f.orden)
        ubicadas = map_figures_llm(unidad, resto, descripciones, client=map_client, model=map_model)
        for f in resto:
            if f.hash in ubicadas:
                li, par, motivo = ubicadas[f.hash]
                asignar(unidad["lecciones"][li], f, "ia", par, motivo)
            else:
                sin.append(f)

    # Anexo de señales: las que el texto nombra van en el texto; el resto, galería en la
    # lección de señales; y TODAS se listan en la unidad anexo.
    n_anexo = n_galeria = 0
    if cap_senales is not None and por_cap.get(cap_senales):
        senales = [f for f in por_cap[cap_senales] if relevante(f)]
        ui = _unidad_de_senales(unidades)
        if ui is not None:
            unidad = unidades[ui]
            ubicadas = map_figures_llm(unidad, senales, descripciones, client=map_client, model=map_model, estricto=True)
            votos = Counter(li for li, _p, _m in ubicadas.values())
            destino = unidad["lecciones"][votos.most_common(1)[0][0]] if votos else _leccion_de_senales(unidad)
            for f in senales:
                # La IA propone; solo entra al texto si la lección NOMBRA la señal (su rótulo
                # del libro aparece en el texto). "Es un ejemplo de señal reglamentaria" no basta.
                if f.hash in ubicadas and _nombrada(f, unidad["lecciones"][ubicadas[f.hash][0]]):
                    li, par, motivo = ubicadas[f.hash]
                    asignar(unidad["lecciones"][li], f, "ia", par, motivo)
                elif destino is not None:
                    asignar(destino, f, "anexo", None, "señal del anexo no mencionada en el texto", en_texto=False)
                    n_galeria += 1
        if anexo and senales:
            ch = chapters[cap_senales]
            texto = "\n\n".join(p.text for p in ch.paras)
            banda = ((plan.get("resumen") or {}).get("banda") or {}).get("max") or 1200
            unidades.append({
                "orden": len(unidades) + 1, "nombre": _ANEXO_NOMBRE,
                "categoria": resolve_categoria("Señales de Tránsito"),
                "paginas": [ch.page_start, ch.page_end], "palabras": len(texto.split()),
                "lecciones": [{
                    "nombre": "Señales de tránsito", "seccion": None, "anexo": True, "corte": "anexo",
                    "paginas": [ch.page_start, ch.page_end], "palabras_objetivo": banda,
                    "palabras_fuente": len(texto.split()), "texto": texto,
                }],
            })
            ensure_plan_ids(plan)
            lec_anexo = unidades[-1]["lecciones"][0]
            for f in senales:
                asignar(lec_anexo, f, "anexo", None)
            n_anexo = len(senales)

    n_galeria += _limitar_densidad(asignadas, extra, {lec["id"] for u in unidades
                                                     for lec in u.get("lecciones") or [] if lec.get("anexo")})

    por_leccion, motivos = save_figures(asignadas, out_dir, rel_to=rel_to, descripciones=descripciones)
    for u in unidades:
        for lec in u.get("lecciones") or []:
            entradas = por_leccion.get(lec.get("id"))
            if not entradas:
                continue
            for e in entradas:
                info = extra.get((lec["id"], e["hash"])) or {}
                if info.get("motivo"):
                    e["motivo"] = info["motivo"]
                if info.get("en_texto") is False:
                    e["en_texto"] = False
            # en el texto primero (por párrafo), la galería al final
            entradas.sort(key=lambda e: (e.get("en_texto") is False,
                                         10 ** 9 if e.get("parrafo") is None else e["parrafo"]))
            lec["figuras"] = entradas

    descartadas = Counter(descartes)
    descartadas.update(motivos)
    descartadas.update(fuera)
    descartadas["decorativa_ia"] += sum(1 for f in usables if not relevante(f)) - motivos.get("decorativa_ia", 0)
    todas = [e for u in unidades for lec in u.get("lecciones") or [] for e in lec.get("figuras") or []]
    meters = [getattr(c, "meter", None) for c in (vision_client, map_client) if c is not None]
    return {
        "modo": "cruzado", "fuente": str(source), "carpeta": Path(out_dir).as_posix(),
        "extraidas": len(figs),
        "asignadas": len(todas),
        "en_texto": sum(1 for e in todas if e.get("en_texto") is not False) - n_anexo,
        "galeria": n_galeria, "anexo": n_anexo,
        "lecciones_con_figuras": sum(1 for u in unidades for lec in u.get("lecciones") or [] if lec.get("figuras")),
        "mapeo": dict(Counter(e["mapeo"] for e in todas)),
        "capitulos": {ch.titulo: (unidades[cap_unidad[ci]]["nombre"] if cap_unidad[ci] is not None
                                  else ("anexo" if ci == cap_senales else None))
                      for ci, ch in enumerate(chapters)},
        "descartadas": {k: v for k, v in descartadas.items() if v},
        "sin_leccion": [{"pagina": f.pagina, "pie": (descripciones.get(f.hash) or {}).get("pie") or f.pie_libro}
                        for f in sin],
        "ia": [m.as_dict() for m in meters if m is not None and hasattr(m, "as_dict")],
    }


def _nombrada(fig: Figura, leccion: dict[str, Any]) -> bool:
    """¿El texto de la lección nombra la señal? Usa el rótulo que trae el LIBRO junto a la
    tarjeta (``pie_libro``, p. ej. "PARE", "CEDA EL PASO"): todas sus palabras de contenido
    deben aparecer en el texto."""
    nombre = _tokens(fig.pie_libro)
    if not nombre or (len(nombre) == 1 and len(next(iter(nombre))) < 4):
        return False
    return nombre <= set(_norm(leccion.get("texto", "")).split())


def _limitar_densidad(asignadas: dict, extra: dict, anexos: set[str]) -> int:
    """Pasa a galería el exceso de figuras por párrafo / por lección. Devuelve cuántas movió."""
    movidas = 0
    for lid, items in asignadas.items():
        if lid in anexos:
            continue
        por_parrafo: Counter = Counter()
        en_texto = 0
        for f, _mapeo, par in sorted(items, key=lambda x: x[0].orden):
            info = extra.setdefault((lid, f.hash), {"en_texto": True, "motivo": ""})
            if info.get("en_texto") is False:
                continue
            if por_parrafo[par] >= MAX_POR_PARRAFO or en_texto >= MAX_EN_TEXTO:
                info["en_texto"] = False
                info["motivo"] = (info.get("motivo") or "") + " · a galería por densidad"
                movidas += 1
                continue
            por_parrafo[par] += 1
            en_texto += 1
    return movidas


def _unidad_de_senales(unidades: list[dict[str, Any]]) -> int | None:
    """Unidad con más lecciones que hablan de señales (por nombre o texto)."""
    def score(u):
        return sum(("senal" in _norm(lec.get("nombre", ""))) * 5 + _norm(lec.get("texto", "")).count("senal")
                   for lec in u.get("lecciones") or [])
    mejor = max(range(len(unidades)), key=lambda i: score(unidades[i]), default=None)
    return mejor if mejor is not None and score(unidades[mejor]) > 0 else None


def _leccion_de_senales(unidad: dict[str, Any]) -> dict[str, Any] | None:
    lecs = unidad.get("lecciones") or []
    for clave in ("senales verticales", "senales de transito", "senal"):
        for lec in lecs:
            if clave in _norm(lec.get("nombre", "")):
                return lec
    return max(lecs, key=lambda lec: _norm(lec.get("texto", "")).count("senal"), default=None)

