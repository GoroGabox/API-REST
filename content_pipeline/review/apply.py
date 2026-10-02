"""Aplica las observaciones ACEPTADAS de un ``review.json`` (Brújula del Libro).

- Fase ``estructura`` → sobre el ``plan.json`` (ops por id; misma semántica que la
  Brújula: renumera unidades y recalcula páginas/palabras).
- Fase ``contenido`` → sobre el JSON de ``generate_course`` (reemplazos acotados:
  ``buscar`` debe aparecer EXACTAMENTE una vez en el campo, si no es conflicto).

Nada se aplica si ``estado != "aceptada"``. Sin IA; determinista.
"""
from __future__ import annotations

import copy
import math
import re
from datetime import datetime, timezone
from typing import Any

from content_pipeline.review.ids import course_key, ensure_plan_ids


class ReviewConflict(Exception):
    """Una observación aceptada que no se puede aplicar sobre el JSON actual."""


# ---------------------------------------------------------------------------
# Carga / fusión
# ---------------------------------------------------------------------------

def merge_reviews(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fusiona los items de varios review.json por ``id`` (el último gana). Orden por fecha."""
    by_id: dict[str, dict[str, Any]] = {}
    for rev in reviews:
        for item in rev.get("items", []) or []:
            if item.get("id"):
                by_id[item["id"]] = item
    return sorted(by_id.values(), key=lambda it: (str(it.get("fecha") or ""), it["id"]))


# ---------------------------------------------------------------------------
# Estructura (plan.json)
# ---------------------------------------------------------------------------

def split_segments(texto: str) -> list[str]:
    """Trozos de corte: párrafos; si hay uno solo, oraciones (igual que la Brújula)."""
    segs = [s.strip() for s in re.split(r"\n\s*\n", str(texto or "")) if s.strip()]
    if len(segs) < 2:
        segs = [s.strip() for s in re.split(r"(?<=[.!?])\s+", str(texto or "")) if s.strip()]
    return segs


def _strip_parte(nombre: str) -> str:
    n = re.sub(r"\s*—\s*parte\s*\d+\s*$", "", str(nombre or ""), flags=re.I)
    return re.sub(r"\s*\(cont\.\)\s*$", "", n, flags=re.I)


def _words(text: str) -> int:
    return len(str(text or "").split())


def recalc_plan(plan: dict[str, Any]) -> None:
    plan["unidades"] = [u for u in plan.get("unidades", []) if u.get("lecciones")]
    for i, u in enumerate(plan["unidades"], start=1):
        u["orden"] = i
        pgs = [l["paginas"] for l in u["lecciones"] if l.get("paginas") and l["paginas"][0]]
        u["paginas"] = [min(p[0] for p in pgs), max(p[1] or p[0] for p in pgs)] if pgs else [0, 0]
        u["palabras"] = sum(int(l.get("palabras_fuente") or 0) for l in u["lecciones"])
    resumen = dict(plan.get("resumen") or {})
    resumen.update(
        unidades=len(plan["unidades"]),
        lecciones=sum(len(u["lecciones"]) for u in plan["unidades"]),
        palabras_totales=sum(u["palabras"] for u in plan["unidades"]),
    )
    plan["resumen"] = resumen


def _find_unit(plan: dict[str, Any], uid: str) -> int:
    for i, u in enumerate(plan.get("unidades", [])):
        if u.get("id") == uid:
            return i
    raise ReviewConflict(f"unidad {uid} no existe en el plan")


def _find_lesson(plan: dict[str, Any], lid: str) -> tuple[int, int]:
    for ui, u in enumerate(plan.get("unidades", [])):
        for li, l in enumerate(u.get("lecciones", [])):
            if l.get("id") == lid:
                return ui, li
    raise ReviewConflict(f"lección {lid} no existe en el plan")


def _band_max(plan: dict[str, Any]) -> int:
    return int(((plan.get("resumen") or {}).get("banda") or {}).get("max") or 1200)


def _n_paras(texto: str) -> int:
    return len([x for x in re.split(r"\n\s*\n", str(texto or "")) if x.strip()])


def _shift_figs(figs: list[dict[str, Any]] | None, delta: int) -> list[dict[str, Any]]:
    """Figuras de la 2ª lección al unir: su párrafo se desplaza (-1 = tras el último de la 1ª)."""
    out = []
    for f in figs or []:
        par = f.get("parrafo")
        out.append(dict(f, parrafo=None if par is None else int(par) + delta))
    return out


def _split_figs(figs: list[dict[str, Any]] | None, k: int, por_parrafo: bool):
    """Reparte las figuras al dividir en el trozo ``k``: van con su párrafo."""
    a, b = [], []
    for f in figs or []:
        par = f.get("parrafo")
        if por_parrafo and par is not None and int(par) >= k:
            b.append(dict(f, parrafo=int(par) - k))
        else:
            a.append(dict(f))
    return a, b


def _find_fig(lec: dict[str, Any], fid: str) -> int:
    for i, f in enumerate(lec.get("figuras") or []):
        if f.get("id") == fid:
            return i
    raise ReviewConflict(f"la figura {fid} ya no está en la lección")


def merge_lessons(a: dict[str, Any], b: dict[str, Any], band_max: int) -> dict[str, Any]:
    pgs = [p for p in (a.get("paginas"), b.get("paginas")) if p and p[0]]
    out = dict(a)
    out.update(
        id=f"{a.get('id')}+{b.get('id')}",
        nombre=_strip_parte(a.get("nombre", "")) or a.get("nombre", ""),
        paginas=[min(p[0] for p in pgs), max(p[1] or p[0] for p in pgs)] if pgs else [0, 0],
        texto=f"{a.get('texto', '')}\n\n{b.get('texto', '')}",
        palabras_fuente=int(a.get("palabras_fuente") or 0) + int(b.get("palabras_fuente") or 0),
        palabras_objetivo=max(int(a.get("palabras_objetivo") or 0), int(b.get("palabras_objetivo") or 0)) or band_max,
    )
    if a.get("figuras") or b.get("figuras"):
        out["figuras"] = [dict(f) for f in a.get("figuras") or []] + _shift_figs(b.get("figuras"), _n_paras(a.get("texto")))
    return out


def split_lesson(lec: dict[str, Any], k: int) -> list[dict[str, Any]]:
    segs = split_segments(lec.get("texto", ""))
    if not 0 < k < len(segs):
        raise ReviewConflict(f"corte {k} fuera de rango (lección con {len(segs)} trozos)")
    a_txt, b_txt = "\n\n".join(segs[:k]), "\n\n".join(segs[k:])
    wa, wb = _words(a_txt), _words(b_txt)
    p = lec.get("paginas") or [0, 0]
    pa = pb = [0, 0]
    if p and p[0]:
        p1 = p[1] or p[0]
        cut = math.floor(p[0] + (p1 - p[0]) * wa / max(1, wa + wb) + 0.5)  # = Math.round de JS
        pa, pb = [p[0], cut], [cut, p1]
    base = _strip_parte(lec.get("nombre", "")) or lec.get("nombre", "")
    a = dict(lec, id=f"{lec.get('id')}-a", nombre=base, texto=a_txt, palabras_fuente=wa, paginas=pa)
    b = dict(lec, id=f"{lec.get('id')}-b", nombre=f"{base} (cont.)", texto=b_txt, palabras_fuente=wb, paginas=pb)
    if "figuras" in lec:
        a["figuras"], b["figuras"] = _split_figs(lec.get("figuras"), k, _n_paras(lec.get("texto")) >= 2)
    return [a, b]


def _replace_once(text: str, buscar: str, reemplazar: str) -> str:
    n = text.count(buscar) if buscar else 0
    if n != 1:
        raise ReviewConflict(
            "texto a corregir no encontrado" if n == 0 else f"texto a corregir aparece {n} veces (ambiguo)")
    return text.replace(buscar, reemplazar, 1)


def apply_structure_op(plan: dict[str, Any], cambio: dict[str, Any], target: dict[str, Any]) -> None:
    """Aplica UNA op de estructura (in-place). Lanza ReviewConflict si no aplica."""
    op = cambio.get("op")
    args = cambio.get("args") or {}
    tid = target.get("id") or ""
    units = plan["unidades"]

    if target.get("tipo") == "unidad":
        ui = _find_unit(plan, tid)
        if op == "rename":
            units[ui]["nombre"] = str(args["nombre"])
        elif op == "categoria":
            units[ui]["categoria"] = str(args["categoria"])
        elif op == "move_unit":
            j = ui + int(args.get("dir", 0))
            if not 0 <= j < len(units) or j == ui:
                raise ReviewConflict("movimiento de unidad fuera de rango")
            units[ui], units[j] = units[j], units[ui]
        elif op == "merge_unit":
            if ui + 1 >= len(units):
                raise ReviewConflict("no hay unidad siguiente para unir")
            units[ui]["lecciones"].extend(units[ui + 1]["lecciones"])
            del units[ui + 1]
        elif op == "delete":
            del units[ui]
        else:
            raise ReviewConflict(f"op de unidad desconocida: {op}")
    else:
        ui, li = _find_lesson(plan, tid)
        lessons = units[ui]["lecciones"]
        if op == "rename":
            lessons[li]["nombre"] = str(args["nombre"])
        elif op == "texto":
            # corrección del texto fuente (p. ej. ruido de extracción); el id no cambia
            lec = lessons[li]
            lec["texto"] = _replace_once(str(lec.get("texto") or ""), str(args["buscar"]), str(args.get("reemplazar", "")))
            lec["palabras_fuente"] = _words(lec["texto"])
        elif op == "cap":
            lessons[li]["palabras_objetivo"] = int(args["palabras_objetivo"])
        elif op == "reorder":
            j = li + int(args.get("dir", 0))
            if not 0 <= j < len(lessons) or j == li:
                raise ReviewConflict("reordenamiento fuera de rango")
            lessons[li], lessons[j] = lessons[j], lessons[li]
        elif op == "merge":
            if li + 1 >= len(lessons):
                raise ReviewConflict("no hay lección siguiente para unir")
            if args.get("con") and lessons[li + 1].get("id") != args["con"]:
                raise ReviewConflict("la lección siguiente ya no es la propuesta")
            lessons[li:li + 2] = [merge_lessons(lessons[li], lessons[li + 1], _band_max(plan))]
        elif op == "split":
            lessons[li:li + 1] = split_lesson(lessons[li], int(args["k"]))
        elif op == "move":
            to = _find_unit(plan, str(args["unidad_id"]))
            if to == ui:
                raise ReviewConflict("la lección ya está en esa unidad")
            lec = lessons.pop(li)
            # hacia atrás: al final del destino; hacia adelante: al inicio (orden del libro)
            if to < ui:
                units[to]["lecciones"].append(lec)
            else:
                units[to]["lecciones"].insert(0, lec)
        elif op == "new_unit":
            if li == 0:
                raise ReviewConflict("la lección ya inicia su unidad")
            rest = lessons[li:]
            del lessons[li:]
            nombre = str(args.get("nombre") or _strip_parte(rest[0].get("nombre", "")) or rest[0].get("nombre", ""))
            units.insert(ui + 1, {
                "id": f"U-{rest[0].get('id', '')[2:10]}",
                "orden": 0, "nombre": nombre,
                "categoria": units[ui].get("categoria", "General"),
                "paginas": [0, 0], "palabras": 0, "lecciones": rest,
            })
        elif op == "figura_quitar":
            figs = lessons[li].get("figuras") or []
            del figs[_find_fig(lessons[li], str(args["figura"]))]
        elif op == "figura_pie":
            fig = lessons[li]["figuras"][_find_fig(lessons[li], str(args["figura"]))]
            fig["pie"] = str(args.get("pie", ""))
            if "alt" in args:
                fig["alt"] = str(args["alt"])
        elif op == "figura_mover":
            fig = lessons[li]["figuras"].pop(_find_fig(lessons[li], str(args["figura"])))
            dui, dli = _find_lesson(plan, str(args.get("leccion") or tid))
            par = args.get("parrafo")
            fig["parrafo"] = None if par is None else int(par)
            destino = units[dui]["lecciones"][dli].setdefault("figuras", [])
            destino.append(fig)
            destino.sort(key=lambda f: (10 ** 9 if f.get("parrafo") is None else int(f["parrafo"])))
        elif op == "delete":
            del lessons[li]
        else:
            raise ReviewConflict(f"op de lección desconocida: {op}")
    recalc_plan(plan)


# ---------------------------------------------------------------------------
# Contenido (JSON de generate_course)
# ---------------------------------------------------------------------------


def _find_course_lesson(course: dict[str, Any], target: dict[str, Any], renamed: dict[int, str]) -> dict[str, Any]:
    lessons = course.get("lessons", [])
    if target.get("id"):
        for l in lessons:
            if l.get("plan_id") == target["id"]:
                return l
    for idx, l in enumerate(lessons):
        if course_key(l) == target.get("clave"):
            nombre = target.get("nombre")
            if nombre and l.get("nombre") != nombre and renamed.get(idx) != nombre:
                raise ReviewConflict(f"{target.get('clave')} ya no es «{nombre}» (ahora «{l.get('nombre')}»)")
            return l
    raise ReviewConflict(f"lección {target.get('clave') or target.get('id')} no existe en el curso")


def apply_content_change(course: dict[str, Any], cambio: dict[str, Any], target: dict[str, Any],
                         renamed: dict[int, str]) -> None:
    lesson = _find_course_lesson(course, target, renamed)
    campo = cambio.get("campo")
    buscar, reemplazar = cambio.get("buscar"), cambio.get("reemplazar", "")

    def edit(obj: dict[str, Any], key: str) -> None:
        if "valor" in cambio:
            obj[key] = cambio["valor"]
        else:
            obj[key] = _replace_once(str(obj.get(key) or ""), str(buscar or ""), str(reemplazar))

    if target.get("tipo") == "pregunta":
        cont = lesson.get("contenido")
        qs = cont.get("questions") if isinstance(cont, dict) else None
        q = int(target.get("q", -1))
        if not qs or not 0 <= q < len(qs):
            raise ReviewConflict(f"pregunta {q} no existe en {course_key(lesson)}")
        question = qs[q]
        if campo == "pregunta":
            edit(question, "question")
        elif campo == "explicacion":
            edit(question, "explanation")
        elif campo == "opcion":
            i = int(cambio.get("i", -1))
            opts = question.get("options") or []
            if not 0 <= i < len(opts):
                raise ReviewConflict(f"opción {i} no existe")
            opts[i] = str(cambio["valor"]) if "valor" in cambio else _replace_once(str(opts[i]), str(buscar or ""), str(reemplazar))
        elif campo == "correcta":
            v = int(cambio["valor"])
            if not 0 <= v < len(question.get("options") or []):
                raise ReviewConflict(f"respuesta correcta {v} fuera de rango")
            question["correct_index"] = v
        else:
            raise ReviewConflict(f"campo de pregunta desconocido: {campo}")
        return

    if campo in ("contenido", "descripcion"):
        if not isinstance(lesson.get(campo, ""), str):
            raise ReviewConflict(f"{campo} no es texto en {course_key(lesson)}")
        edit(lesson, campo)
    elif campo == "nombre":
        old = lesson.get("nombre")
        edit(lesson, "nombre")
        lessons = course.get("lessons", [])
        renamed[lessons.index(lesson)] = old
    else:
        raise ReviewConflict(f"campo desconocido: {campo}")


# ---------------------------------------------------------------------------
# Orquestación
# ---------------------------------------------------------------------------

def _summary(item: dict[str, Any]) -> str:
    c = item.get("cambio") or {}
    t = item.get("target") or {}
    what = c.get("op") or c.get("campo") or item.get("tipo")
    return f"{t.get('clave') or t.get('id') or '?'} · {what}"


def apply_review(data: dict[str, Any], items: list[dict[str, Any]], *, fase: str) -> dict[str, Any]:
    """Devuelve ``{"data": copia modificada, "aplicadas": [...], "conflictos": [...], "ignoradas": n}``."""
    out = copy.deepcopy(data)
    if fase == "estructura":
        ensure_plan_ids(out)
    aplicadas: list[dict[str, Any]] = []
    conflictos: list[dict[str, Any]] = []
    ignoradas = 0
    renamed: dict[int, str] = {}

    for item in items:
        if item.get("fase") != fase:
            continue
        if item.get("estado") != "aceptada" or not item.get("cambio"):
            ignoradas += 1  # pendientes, rechazadas y comentarios sin cambio
            continue
        try:
            if fase == "estructura":
                apply_structure_op(out, item["cambio"], item.get("target") or {})
            else:
                apply_content_change(out, item["cambio"], item.get("target") or {}, renamed)
            aplicadas.append({"id": item["id"], "autor": item.get("autor", ""), "resumen": _summary(item)})
        except (ReviewConflict, KeyError, ValueError, TypeError) as exc:
            conflictos.append({"id": item["id"], "autor": item.get("autor", ""),
                               "resumen": _summary(item), "motivo": str(exc)})

    registro = {
        "fecha": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fase": fase,
        "autores": sorted({i.get("autor", "") for i in items if i.get("fase") == fase and i.get("autor")}),
        "aplicadas": aplicadas,
        "conflictos": conflictos,
    }
    out.setdefault("revision_humana", []).append(registro)
    return {"data": out, "aplicadas": aplicadas, "conflictos": conflictos, "ignoradas": ignoradas}
