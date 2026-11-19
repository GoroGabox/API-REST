"""Guías del alumno en Word (ya redactadas como curso) → JSON de curso + ejercicios.

Conversión FIEL: no se reescribe el texto. Cada guía es un módulo (unidad del
curso) y cada «Unidad N · …» / «Módulo N · …» interna es una lección:

- ``## Objetivo``      ← «Objetivos de la unidad» (si falta, lo completa la IA)
- ``## Desarrollo``    ← capítulos (H1 → ``### …``; H2/Subtitle → línea en negrita),
                          párrafos, viñetas, recuadros (tabla de 1 celda), tablas e imágenes
- ``## Puntos clave``  ← «Ideas clave de la unidad N» (si falta, la IA)
- ``## Resumen``       ← lo escribe la IA (las guías no lo traen), auditado
- ``## Fuente``        ← guía + páginas del libro CONASET de la unidad

Además: «Actividad de aplicación» → ``## Aplicación práctica`` de la última lección
del módulo; glosario, correspondencias y fuentes → lección anexo «Material de
referencia»; cuestionarios + «Respuestas de los cuestionarios» → ejercicios del
banco (con explicación); un quiz embebido por módulo (respaldo offline).

El JSON de salida tiene el mismo formato que ``generate_course`` para reutilizar
``validate_generated_course``, ``publish_media``, ``import_course`` y la Brújula.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from content_pipeline.review.ids import lesson_id

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

FUENTE_LIBRO = "Libro del Nuevo Conductor Profesional (CONASET)"

_UNIDAD_RE = re.compile(r"^(?:Unidad|Módulo)\s+(\d+)\s*·\s*(.+)$")
_MODULO_TITULO_RE = re.compile(r"^Módulo\s*\d+\s*", re.IGNORECASE)
_PAGINAS_RE = re.compile(r"Libro CONASET:\s*p[áa]ginas?\s*(.+)", re.IGNORECASE)
_TIEMPO_RE = re.compile(r"Tiempo estimado(?: de estudio)?:\s*(\d+)\s*min", re.IGNORECASE)
_PREGUNTA_RE = re.compile(r"^(\d+)[.)]\s+(.+)$")
_OPCION_RE = re.compile(r"^([a-f])\)\s*(.+)$")
_RESPUESTA_RE = re.compile(r"^(\d+)[.)]\s*Alternativa\s+([a-f])\)\s*(.*)$", re.IGNORECASE)

# Rótulo del recuadro (tabla de 1 celda) → tipo del contrato de componentes.
TIPOS_RECUADRO = {
    "criterio central": "DEBES_SABER",
    "alcance por clase": "DEBES_SABER",
    "recuerde": "DEBES_SABER",
    "aplicación profesional": "CONSEJO",
    "como profesional": "CONSEJO",
    "información importante": "ATENCION",
    "importante": "ATENCION",
    "atención": "ATENCION",
    "ante una emergencia": "ATENCION",
    "norma": "NORMA",
    "sanción": "NORMA",
    "dato": "DATO",
}
_SECCIONES_FIN = ("organización del curso online", "actividad de aplicación",
                  "anexo · material de referencia", "respuestas de los cuestionarios")


def _norm(texto: str) -> str:
    t = unicodedata.normalize("NFKD", str(texto or ""))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9ñ%]+", " ", t).split())


# ---------------------------------------------------------------------------
# Lectura del .docx en bloques ordenados
# ---------------------------------------------------------------------------

@dataclass
class Bloque:
    tipo: str                         # p | tabla | img
    estilo: str = ""
    texto: str = ""                   # texto plano (p)
    md: str = ""                      # texto con **negritas** (p)
    filas: list[list[str]] = field(default_factory=list)   # tabla: celdas (párrafos unidos por \n)
    filas_md: list[list[str]] = field(default_factory=list)
    lista: bool = False
    img: bytes = b""
    ext: str = ""
    pie: str = ""                     # img: rótulo conocido (celda de una tabla de imágenes)


def _es_negrita(run) -> bool:
    b = run.find(f"{_W}rPr/{_W}b")
    return b is not None and b.get(_W + "val", "true") not in ("0", "false")


def _md_parrafo(p) -> tuple[str, str]:
    """(texto plano, texto con **negritas**) de un párrafo."""
    segs: list[tuple[bool, str]] = []
    for run in p.iter(_W + "r"):
        t = "".join(x.text or "" for x in run.iter(_W + "t"))
        if run.find(_W + "tab") is not None:
            t += " "
        if not t:
            continue
        neg = _es_negrita(run)
        if segs and segs[-1][0] == neg:
            segs[-1] = (neg, segs[-1][1] + t)
        else:
            segs.append((neg, t))
    plano = "".join(t for _, t in segs).strip()
    if not segs:
        return "", ""
    # Párrafo entero en negrita: no se marca (lo decide el contexto).
    if all(neg for neg, _ in segs):
        return plano, plano
    md = ""
    for neg, t in segs:
        if neg and t.strip():
            lead = t[: len(t) - len(t.lstrip())]
            trail = t[len(t.rstrip()):]
            md += f"{lead}**{t.strip()}**{trail}"
        else:
            md += t
    return plano, " ".join(md.split())


def leer_bloques(path: str | Path) -> list[Bloque]:
    z = zipfile.ZipFile(path)
    root = ET.fromstring(z.read("word/document.xml"))
    rels = {}
    if "word/_rels/document.xml.rels" in z.namelist():
        for r in ET.fromstring(z.read("word/_rels/document.xml.rels")).iter(_REL + "Relationship"):
            rels[r.get("Id")] = r.get("Target")
    body = root.find(_W + "body")
    bloques: list[Bloque] = []

    def imagenes(el, pie: str = "") -> list[Bloque]:
        out = []
        for blip in el.iter(_A + "blip"):
            destino = rels.get(blip.get(_R + "embed"), "")
            nombre = "word/" + destino.lstrip("/") if not destino.startswith("word/") else destino
            ext = destino.rsplit(".", 1)[-1].lower()
            if nombre in z.namelist() and ext in ("png", "jpg", "jpeg", "gif"):
                out.append(Bloque("img", img=z.read(nombre), ext="jpg" if ext == "jpeg" else ext, pie=pie))
        return out

    for el in body:
        if el.tag == _W + "tbl" and any(True for _ in el.iter(_A + "blip")):
            # Tabla de imágenes (grilla de esquemas con su rótulo): cada celda es una figura.
            for tc in el.iter(_W + "tc"):
                rotulo = " ".join(_md_parrafo(p)[0] for p in tc.iter(_W + "p")).strip()
                bloques.extend(imagenes(tc, rotulo))
            continue
        if el.tag == _W + "p":
            bloques.extend(imagenes(el))
            st = el.find(f"{_W}pPr/{_W}pStyle")
            estilo = st.get(_W + "val") if st is not None else ""
            plano, md = _md_parrafo(el)
            if plano:
                lista = estilo.lower().startswith("list") or el.find(f"{_W}pPr/{_W}numPr") is not None
                bloques.append(Bloque("p", estilo=estilo, texto=plano, md=md, lista=lista))
        elif el.tag == _W + "tbl":
            filas, filas_md = [], []
            for tr in el.iter(_W + "tr"):
                celdas, celdas_md = [], []
                for tc in tr.iter(_W + "tc"):
                    ps = [_md_parrafo(p) for p in tc.iter(_W + "p")]
                    ps = [x for x in ps if x[0]]
                    celdas.append("\n".join(x[0] for x in ps))
                    celdas_md.append("\n".join(x[1] for x in ps))
                filas.append(celdas)
                filas_md.append(celdas_md)
            bloques.append(Bloque("tabla", filas=filas, filas_md=filas_md))
    return bloques


# ---------------------------------------------------------------------------
# Render de componentes
# ---------------------------------------------------------------------------

def _titulo_estilo(estilo: str) -> str:
    e = estilo.lower()
    if e == "title":
        return "title"
    if e == "heading1":
        return "h1"
    if e in ("heading2", "heading3", "subtitle"):
        return "h2"
    return ""


def recuadro(b: Bloque) -> tuple[str, str]:
    """Tabla de 1 celda → (markdown del recuadro, rótulo)."""
    lineas = [l for l in b.filas_md[0][0].split("\n") if l.strip()]
    planas = [l for l in b.filas[0][0].split("\n") if l.strip()]
    rotulo, cuerpo = "", lineas
    if len(planas) > 1 and len(planas[0]) <= 40:
        rotulo, cuerpo = planas[0].strip(), lineas[1:]
    else:
        # Rótulo pegado al texto en negrita: «**Criterio central** El conductor…»
        m = re.match(r"^\*\*(.{2,40}?)\*\*\s*(.*)$", lineas[0]) if lineas else None
        if m:
            rotulo, cuerpo = m.group(1).strip(), [m.group(2)] + lineas[1:]
        else:
            # Rótulo y texto en una sola cadena: «Recuerde  La distancia…», «INFORMACIÓN IMPORTANTEEn…»
            m = re.match(r"^([A-ZÁÉÍÓÚÑ][^.:]{1,38}?)(?:\s{2,}|(?<=[A-ZÁÉÍÓÚÑ])(?=[A-ZÁÉÍÓÚÑ][a-záéíóúñ]))(.+)$",
                         planas[0]) if planas else None
            if m and _norm(m.group(1)) in {_norm(k) for k in TIPOS_RECUADRO}:
                rotulo, cuerpo = m.group(1).strip(), [m.group(2).strip()] + lineas[1:]
    tipo = next((v for k, v in TIPOS_RECUADRO.items() if _norm(k) == _norm(rotulo)), "DEBES_SABER")
    titulo = rotulo[:1].upper() + rotulo[1:].lower() if rotulo.isupper() else rotulo
    md = [f"> [!{tipo}] {titulo}".rstrip()]
    for l in cuerpo:
        l = l.strip()
        if l:
            md.append(f"> {l}")
    return "\n".join(md), rotulo or "(sin rótulo)"


def tabla_md(b: Bloque, titulo: str = "") -> str:
    filas = [[" ".join(c.replace("|", "\\|").split()) for c in f] for f in b.filas_md if any(x.strip() for x in f)]
    if not filas:
        return ""
    ancho = max(len(f) for f in filas)
    filas = [f + [""] * (ancho - len(f)) for f in filas]
    out = [f"Tabla: {titulo}"] if titulo else []
    out.append("| " + " | ".join(filas[0]) + " |")
    out.append("|" + "---|" * ancho)
    out += ["| " + " | ".join(f) + " |" for f in filas[1:]]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Estructura del módulo
# ---------------------------------------------------------------------------

@dataclass
class Leccion:
    orden: int
    nombre: str
    paginas: str = ""
    minutos: int = 0
    objetivos: list[str] = field(default_factory=list)
    desarrollo: list[str] = field(default_factory=list)       # bloques markdown
    ideas: list[str] = field(default_factory=list)
    aplicacion: list[str] = field(default_factory=list)
    preguntas: list[dict[str, Any]] = field(default_factory=list)
    figuras: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Modulo:
    numero: int
    archivo: str
    titulo: str = ""
    presentacion: list[str] = field(default_factory=list)
    lecciones: list[Leccion] = field(default_factory=list)
    anexo: list[str] = field(default_factory=list)
    anexo_figuras: list[dict[str, Any]] = field(default_factory=list)
    respuestas: dict[tuple[int, int], dict[str, str]] = field(default_factory=dict)
    rotulos: dict[str, int] = field(default_factory=dict)
    avisos: list[str] = field(default_factory=list)


def _figura(b: Bloque, codigo: str) -> dict[str, Any]:
    h = hashlib.sha1(b.img).hexdigest()
    return {"id": f"F-{h[:10]}", "hash": h, "ext": b.ext, "data": b.img, "pie": b.pie,
            "archivo": f"figuras_{codigo}/{h[:16]}.{b.ext}"}


def _pie_desde(texto: str) -> str:
    t = re.sub(r"^(Esquema didáctico|Ilustración didáctica)[^·]*·\s*", "", texto).strip()
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    return (m.group(1) if m else t)[:200]


def parsear_modulo(path: str | Path, numero: int, codigo: str) -> Modulo:
    bloques = leer_bloques(path)
    mod = Modulo(numero=numero, archivo=Path(path).name)
    modo = "inicio"            # inicio | presentacion | omitir | leccion | aplicacion | anexo | respuestas
    sub = "desarrollo"         # objetivos | desarrollo | ideas | cuestionario
    lec: Leccion | None = None
    resp_unidad = 0
    resp_actual: dict[str, Any] | None = None
    preg_actual: dict[str, Any] | None = None
    figura_pendiente: dict[str, Any] | None = None

    def destino() -> tuple[list[str], list[dict[str, Any]]] | None:
        if modo == "leccion" and lec is not None and sub == "desarrollo":
            return lec.desarrollo, lec.figuras
        if modo == "aplicacion" and mod.lecciones:
            return mod.lecciones[-1].aplicacion, mod.lecciones[-1].figuras
        if modo == "anexo":
            return mod.anexo, mod.anexo_figuras
        return None

    for b in bloques:
        nivel = _titulo_estilo(b.estilo) if b.tipo == "p" else ""
        texto = b.texto if b.tipo == "p" else ""

        # Pie de figura: el párrafo siguiente a la imagen.
        if figura_pendiente is not None and b.tipo == "p" and not nivel and not figura_pendiente.get("pie"):
            figura_pendiente["pie"] = _pie_desde(texto)
        if b.tipo != "img":
            figura_pendiente = None if b.tipo == "p" else figura_pendiente

        if nivel == "title":
            mod.titulo = _MODULO_TITULO_RE.sub("", texto).strip()
            continue
        if nivel == "h1":
            m = _UNIDAD_RE.match(texto)
            low = texto.lower()
            if m and modo != "respuestas":
                lec = Leccion(orden=int(m.group(1)), nombre=m.group(2).strip())
                mod.lecciones.append(lec)
                modo, sub = "leccion", "desarrollo"
                continue
            if low.startswith("organización del curso"):
                modo = "omitir"
                continue
            if low.startswith("actividad de aplicación"):
                modo = "aplicacion"
                continue
            if low.startswith("anexo"):
                modo = "anexo"
                continue
            if low.startswith("respuestas de los cuestionarios"):
                modo = "respuestas"
                continue
            if modo == "respuestas" and ("fuente" in low or "referencia" in low):
                modo = "anexo"
            if modo in ("leccion", "anexo", "aplicacion"):
                sub = "desarrollo"
                dest = destino()
                if dest is not None:
                    dest[0].append(f"### {texto}")
            continue
        if nivel == "h2":
            low = texto.lower()
            if modo == "inicio" and low.startswith("presentación"):
                modo = "presentacion"
                continue
            if modo == "presentacion":
                modo = "inicio"
                continue
            if modo == "respuestas":
                m = _UNIDAD_RE.match(texto)
                if m:
                    resp_unidad = int(m.group(1))
                continue
            if modo == "leccion" and lec is not None:
                if low.startswith("objetivos de la unidad") or low.startswith("objetivos del módulo"):
                    sub = "objetivos"
                    continue
                if low.startswith("ideas clave"):
                    sub = "ideas"
                    continue
                if low.startswith("cuestionario"):
                    sub = "cuestionario"
                    continue
                sub = "desarrollo"
            dest = destino()
            if dest is not None:
                dest[0].append(f"**{texto.rstrip('.:')}**")
            continue

        # --- contenido ---
        if modo == "presentacion" and b.tipo == "p":
            mod.presentacion.append(texto)
            continue
        if modo == "respuestas" and b.tipo == "p":
            m = _RESPUESTA_RE.match(texto)
            if m:
                resp_actual = {"letra": m.group(2).lower(), "texto": m.group(3).strip(), "explicacion": ""}
                mod.respuestas[(resp_unidad, int(m.group(1)))] = resp_actual
            elif resp_actual is not None:
                resp_actual["explicacion"] = (resp_actual["explicacion"] + " " + texto).strip()
            continue
        if modo == "leccion" and lec is not None and b.tipo == "p":
            if not lec.paginas and (m := _PAGINAS_RE.match(texto)) and not lec.desarrollo:
                lec.paginas = m.group(1).strip().rstrip(".")
                continue
            if (m := _TIEMPO_RE.match(texto)) and not lec.desarrollo:
                lec.minutos = int(m.group(1))
                continue
            if texto.lower().startswith("capítulos:") and not lec.desarrollo:
                continue
            if sub == "objetivos":
                lec.objetivos.append(b.md)
                continue
            if sub == "ideas":
                lec.ideas.append(b.md)
                continue
            if sub == "cuestionario":
                if texto.lower().startswith("marque la alternativa"):
                    continue
                if (m := _OPCION_RE.match(texto)) and preg_actual is not None:
                    preg_actual["opciones"][m.group(1)] = m.group(2).strip()
                elif m := _PREGUNTA_RE.match(texto):
                    preg_actual = {"numero": int(m.group(1)), "pregunta": m.group(2).strip(), "opciones": {}}
                    lec.preguntas.append(preg_actual)
                else:
                    mod.avisos.append(f"U{lec.orden}: línea de cuestionario no reconocida: {texto[:60]}")
                continue

        dest = destino()
        if dest is None:
            continue
        partes, figs = dest
        if b.tipo == "img":
            fig = _figura(b, codigo)
            figs.append(fig)
            partes.append(f"{{{{figura:{fig['id']}}}}}")
            figura_pendiente = fig
        elif b.tipo == "tabla":
            if len(b.filas) == 1 and len(b.filas[0]) == 1:
                md, rotulo = recuadro(b)
                mod.rotulos[rotulo] = mod.rotulos.get(rotulo, 0) + 1
                partes.append(md)
            elif len(b.filas[0]) == 1:
                # Tabla de una columna con varias filas: lista.
                partes.append("\n".join(f"- {' '.join(f[0].split())}" for f in b.filas_md if f and f[0].strip()))
            else:
                partes.append(tabla_md(b))
        elif b.lista:
            # Viñetas consecutivas forman una sola lista.
            if partes and partes[-1].startswith("- "):
                partes[-1] += f"\n- {b.md}"
            else:
                partes.append(f"- {b.md}")
        else:
            partes.append(b.md)
    return mod


# ---------------------------------------------------------------------------
# Salida: curso + ejercicios
# ---------------------------------------------------------------------------

def tramos_paginas(texto: str) -> list[tuple[int, int]]:
    """«40 y 205 a 210», «38, 43 a 45, 241», «99–101 (visión general)» → [(40,40), (205,210)]…
    Ordenados y sin solapes (los contiguos se unen)."""
    limpio = re.sub(r"\([^)]*\)", " ", texto or "")
    tramos = []
    for parte in re.split(r",|\by\b|;", limpio):
        nums = [int(n) for n in re.findall(r"\d+", parte)]
        if len(nums) >= 2 and re.search(r"\d\s*(a|–|-|al)\s*\d", parte):
            tramos.append((min(nums[0], nums[1]), max(nums[0], nums[1])))
        else:
            tramos += [(n, n) for n in nums]
    unidos: list[tuple[int, int]] = []
    for x, y in sorted(tramos):
        if unidos and x <= unidos[-1][1] + 1:
            unidos[-1] = (unidos[-1][0], max(y, unidos[-1][1]))
        else:
            unidos.append((x, y))
    return unidos


def _cuerpo(partes: list[str]) -> str:
    return "\n\n".join(p.strip() for p in partes if p and p.strip())


def _vinetas(items: list[str]) -> str:
    return "\n".join(f"- {i.strip()}" for i in items if i.strip())


def _recursos(figs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for k, f in enumerate(figs, start=1):
        pie = f.get("pie") or ""
        out.append({
            "tipo": "imagen", "rol": "figura", "clave": f["id"], "url": "", "orden": k, "titulo": "",
            "meta": {"pie": pie, "alt": pie, "origen": "docx", "mapeo": "texto", "hash": f["hash"], "ext": f["ext"]},
            "archivo_local": f["archivo"],
        })
    return out


def armar_curso(modulos: list[Modulo], *, nombre: str, codigo: str, precio: int,
                profesional: bool, guia: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """(curso_json, ejercicios, figuras) a partir de los módulos parseados."""
    unidades, lessons, ejercicios, figuras = [], [], [], []
    for mod in modulos:
        nombre_guia = f"{guia} · Módulo {mod.numero}: {mod.titulo}"
        minutos = sum(l.minutos for l in mod.lecciones)
        unidades.append({"orden": mod.numero, "nombre": mod.titulo, "categoria": "General",
                         "horas_elearning": round(minutos / 60, 1),
                         "temas": [l.nombre for l in mod.lecciones]})
        pos = 0
        for l in mod.lecciones:
            pos += 1
            tramos = tramos_paginas(l.paginas)
            secciones = []
            if l.objetivos:
                secciones.append("## Objetivo\n\n" + _vinetas(l.objetivos))
            secciones.append("## Desarrollo\n\n" + _cuerpo(l.desarrollo))
            if l.aplicacion:
                secciones.append("## Aplicación práctica\n\n" + _cuerpo(l.aplicacion))
            if l.ideas:
                secciones.append("## Puntos clave\n\n" + _vinetas(l.ideas))
            fuente_txt = f"{nombre_guia}."
            if l.paginas:
                fuente_txt += f"\n{FUENTE_LIBRO}, páginas {l.paginas}."
            secciones.append("## Fuente\n" + fuente_txt)
            contenido = f"# {l.nombre}\n\n" + "\n\n".join(secciones) + "\n"
            figuras += l.figuras
            lessons.append({
                "unidad_orden": mod.numero, "unidad_nombre": mod.titulo, "categoria": "General",
                "tema_regulatorio": l.nombre, "nombre": l.nombre, "posicion": pos, "tipo": "texto",
                "descripcion": f"{l.nombre} — dentro de {mod.titulo}.",
                "duracion_min": l.minutos or 15, "contenido": contenido, "transcripcion": "",
                # Un tramo de páginas del libro por fuente (publish_media los junta en un PDF).
                "fuentes": [{"fuente_nombre": FUENTE_LIBRO, "pagina_inicio": x, "pagina_fin": y,
                             "tema_regulatorio": l.nombre, "fragmento_resumen": l.paginas}
                            for x, y in tramos] or [{"fuente_nombre": nombre_guia, "pagina_inicio": 0,
                                                     "pagina_fin": 0, "tema_regulatorio": l.nombre,
                                                     "fragmento_resumen": ""}],
                "fuente_debil": False, "plan_id": lesson_id(contenido), "revision_visual": None,
                "anexo": False, "recursos": _recursos(l.figuras),
                "guia": {"modulo": mod.numero, "unidad": l.orden, "objetivo_guia": bool(l.objetivos),
                         "puntos_clave_guia": bool(l.ideas)},
            })
            for p in l.preguntas:
                r = mod.respuestas.get((l.orden, p["numero"]))
                motivo = None
                if r is None:
                    motivo = "sin respuesta en la guía"
                elif r["letra"] not in p["opciones"]:
                    motivo = f"la respuesta «{r['letra']}» no es una alternativa"
                elif r["texto"] and _norm(r["texto"]) not in _norm(p["opciones"][r["letra"]]) \
                        and _norm(p["opciones"][r["letra"]]) not in _norm(r["texto"]):
                    motivo = "el texto de la respuesta no coincide con la alternativa"
                ejercicios.append({
                    "numero": f"m{mod.numero}-u{l.orden}-{p['numero']}",
                    "pregunta": p["pregunta"], "opciones": p["opciones"],
                    "respuestas": [r["letra"]] if r and not motivo else [],
                    "multi": False, "explicacion": (r or {}).get("explicacion", ""),
                    "tiene_imagen": False, "diferir": bool(motivo), "motivo": motivo,
                    "unidad_orden": mod.numero, "grupo": f"{codigo.lower()}-m{mod.numero}-u{l.orden}-{p['numero']}",
                    "fuente": f"Guía {codigo} módulo {mod.numero}",
                })
        # Páginas del libro de todo el módulo (anexo y lecciones sin páginas propias).
        tramos_mod = tramos_paginas(" , ".join(l.paginas for l in mod.lecciones))
        fuentes_mod = [{"fuente_nombre": FUENTE_LIBRO, "pagina_inicio": x, "pagina_fin": y,
                        "tema_regulatorio": mod.titulo, "fragmento_resumen": ""} for x, y in tramos_mod]
        for x in lessons:
            if x["unidad_orden"] == mod.numero and not x["fuentes"][0]["pagina_inicio"]:
                x["fuentes"] = [dict(f, tema_regulatorio=x["nombre"]) for f in fuentes_mod]
        if mod.anexo:
            pos += 1
            contenido = (f"# Material de referencia\n\n## Desarrollo\n\n{_cuerpo(mod.anexo)}\n\n"
                         f"## Fuente\n{nombre_guia}.\n")
            figuras += mod.anexo_figuras
            lessons.append({
                "unidad_orden": mod.numero, "unidad_nombre": mod.titulo, "categoria": "General",
                "tema_regulatorio": "Material de referencia", "nombre": "Material de referencia",
                "posicion": pos, "tipo": "texto", "descripcion": f"Glosario y referencias de {mod.titulo}.",
                "duracion_min": 10, "contenido": contenido, "transcripcion": "",
                "fuentes": [dict(f, tema_regulatorio="Material de referencia") for f in fuentes_mod],
                "fuente_debil": False, "plan_id": lesson_id(contenido), "revision_visual": None,
                "anexo": True, "recursos": _recursos(mod.anexo_figuras),
            })
        # Quiz del módulo: preguntas embebidas (respaldo offline / sin banco).
        validas = [e for e in ejercicios if e["unidad_orden"] == mod.numero and not e["diferir"]]
        paso = max(1, len(validas) // 10)
        muestra = validas[::paso][:10]
        pos += 1
        ini = min((x["fuentes"][0]["pagina_inicio"] for x in lessons
                   if x["unidad_orden"] == mod.numero and x["fuentes"][0]["pagina_inicio"]), default=0)
        fin = max((x["fuentes"][0]["pagina_fin"] for x in lessons if x["unidad_orden"] == mod.numero), default=0)
        lessons.append({
            "unidad_orden": mod.numero, "unidad_nombre": mod.titulo, "categoria": "General",
            "tema_regulatorio": f"Evaluación módulo {mod.numero}", "nombre": f"Evaluación del módulo {mod.numero}",
            "posicion": pos, "tipo": "quiz", "descripcion": f"Evaluación de cierre de {mod.titulo}.",
            "duracion_min": 10,
            "contenido": {"questions": [{
                "question": e["pregunta"], "options": list(e["opciones"].values()),
                "correct_index": list(e["opciones"]).index(e["respuestas"][0]),
                "explanation": e["explicacion"],
            } for e in muestra]},
            "transcripcion": "",
            "fuentes": [{"fuente_nombre": FUENTE_LIBRO, "pagina_inicio": ini, "pagina_fin": fin,
                         "tema_regulatorio": f"Evaluación módulo {mod.numero}", "fragmento_resumen": ""}],
            "fuente_debil": False, "anexo": False,
        })
    curso = {
        "manifest": {
            "curso": {"nombre": nombre, "codigo": codigo, "is_profesional": profesional, "costo": precio,
                      "descripcion": f"{nombre}: " + "; ".join(m.titulo for m in modulos) + "."},
            "unidades": unidades,
        },
        "lessons": lessons,
        "auditoria": {},
        "revision_humana": [],
    }
    return curso, ejercicios, figuras


def ordenar_archivos(carpeta: str | Path) -> list[tuple[int, Path]]:
    """[(numero_modulo, archivo)] ordenados por el número del nombre (A4_3_… → 3)."""
    out = []
    for p in Path(carpeta).glob("*.docx"):
        if p.name.startswith("~$"):
            continue
        m = re.match(r"^[A-Za-z0-9]+_(\d+)_", p.name)
        if m:
            out.append((int(m.group(1)), p))
    return sorted(out)


# ---------------------------------------------------------------------------
# Secciones que la guía no trae: Resumen (siempre), Objetivo y Puntos clave (si faltan)
# ---------------------------------------------------------------------------

_SECCION_PROMPT = {
    "## Objetivo": "Escribe el OBJETIVO de la lección: 1 a 3 viñetas que empiecen con un verbo "
                   "(Explicar, Identificar, Aplicar, Reconocer…). Formato: líneas que empiezan con «- ».",
    "## Puntos clave": "Escribe los PUNTOS CLAVE: 4 a 6 viñetas breves con los datos y reglas más "
                       "importantes. Formato: líneas que empiezan con «- ».",
    "## Resumen": "Escribe el RESUMEN de la lección: un párrafo de 2 a 4 oraciones, sin viñetas.",
}
_SYSTEM_SECCION = (
    "Redactas una sección faltante de una lección de un curso profesional de conducción (Chile). "
    "Usa SOLO la información del texto de la lección que se entrega: no agregues datos, cifras, "
    "normas ni consejos que no estén en ese texto. Conserva las cifras y términos tal como aparecen. "
    "Responde solo con el contenido de la sección, sin título."
)
# Orden canónico de las secciones (el mismo de llm_lesson_writer._REQUIRED_SECTIONS + Fuente).
_ORDEN_SECCIONES = ("## Objetivo", "## Introducción", "## Desarrollo", "## Aplicación práctica",
                    "## Puntos clave", "## Ejemplo aplicado", "## Errores frecuentes", "## Actividad breve",
                    "## Resumen", "## Fuente")


def _texto_plano(contenido: str) -> str:
    from content_pipeline.processors.figure_markers import strip_markers
    return strip_markers(contenido)


def _insertar_seccion(contenido: str, seccion: str, cuerpo: str) -> str:
    """Inserta ``seccion`` respetando el orden canónico de las secciones."""
    idx = _ORDEN_SECCIONES.index(seccion)
    pos = len(contenido)
    for siguiente in _ORDEN_SECCIONES[idx + 1:]:
        p = contenido.find("\n" + siguiente)
        if p != -1:
            pos = p
            break
    return contenido[:pos].rstrip() + f"\n\n{seccion}\n\n{cuerpo.strip()}\n\n" + contenido[pos:].lstrip("\n")


def _extractivo(seccion: str, texto: str) -> str:
    oraciones = [o.strip() for o in re.split(r"(?<=[.!?])\s+", texto) if 40 <= len(o.strip()) <= 260]
    if seccion == "## Resumen":
        return " ".join(oraciones[:3])
    return "\n".join(f"- {o}" for o in oraciones[: 3 if seccion == "## Objetivo" else 5])


def completar_secciones(curso: dict[str, Any], client: Any, *, log=print) -> list[dict[str, Any]]:
    """Agrega a cada lección de texto (no anexo) las secciones núcleo que falten.

    Lo generado sale solo del texto de la lección; las cifras se auditan con
    ``faithfulness.unsupported_figures`` (un reintento con el error; si persiste,
    versión extractiva). Devuelve el registro ``[{leccion, seccion, modo, cifras}]``.
    """
    from content_pipeline.processors.faithfulness import unsupported_figures

    registro = []
    for l in curso["lessons"]:
        if l.get("tipo") != "texto" or l.get("anexo"):
            continue
        for seccion in ("## Objetivo", "## Puntos clave", "## Resumen"):
            if seccion in l["contenido"]:
                continue
            fuente = _texto_plano(l["contenido"].split("## Fuente")[0])
            user = f"{_SECCION_PROMPT[seccion]}\n\nLección: {l['nombre']}\n\nTexto de la lección:\n{fuente}"
            cuerpo, cifras, modo = "", [], "ia"
            for intento in range(2):
                cuerpo = client.complete(system=_SYSTEM_SECCION, user=user, max_tokens=700, temperature=0.2).strip()
                cuerpo = re.sub(r"^#+\s.*\n", "", cuerpo).strip()
                cifras = unsupported_figures(cuerpo, fuente)
                if not cifras:
                    break
                user += ("\n\nTu respuesta anterior incluía cifras que no están en el texto: "
                         + "; ".join(cifras) + ". Reescríbela usando solo cifras del texto.")
            if cifras:
                cuerpo, modo = _extractivo(seccion, fuente), "extractivo"
            l["contenido"] = _insertar_seccion(l["contenido"], seccion, cuerpo)
            registro.append({"leccion": f"U{l['unidad_orden']}.{l['posicion']}", "nombre": l["nombre"],
                             "seccion": seccion.lstrip("# "), "modo": modo, "cifras": cifras})
            log(f"  U{l['unidad_orden']}.{l['posicion']} {seccion.lstrip('# ')} ({modo})")
    return registro
