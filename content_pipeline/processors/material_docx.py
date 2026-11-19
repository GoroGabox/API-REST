"""«Material de estudio» de los cursos profesionales (un .docx con 7 módulos) → curso + banco.

Formato (cursos A2, A3, A4 y A5, versión 2026-11):

- H1 «Contenido» (índice, se omite) y H1 «Introducción al curso» (licencia, requisitos,
  municipalidad, organización) → presentación de la unidad 0
  (``content_pipeline/presentaciones/<COD>.md``, sin «Cómo está organizado el curso»).
- Párrafo «MÓDULO N» + H1 título → unidad N. Antes de las unidades: tabla 1×1 «Horas de
  este módulo» (→ descripción), presentación y «Objetivos del módulo» (→ ``## Introducción``
  de la primera lección).
- H2 «Unidad N · Título» → lección (``Tiempo estimado de estudio`` → minutos). H3 → ``### ``,
  H4 → negrita, listas (con nivel), tablas 1×1 «Recuerde/Importante/Atención…» → recuadros,
  «Preguntas para repasar» → ``## Actividad breve``, tablas de datos → GFM, tablas de
  señales [imagen | texto] → figura + su texto.
- H2 «Ideas clave del módulo N» → lección ``anexo`` (sin IA) antes del quiz del módulo.

Conversión fiel: Objetivo, Puntos clave y Resumen (que no vienen por unidad) los agrega
``guia_docx.completar_secciones``. El banco sale de la «Práctica de test … CON_RESPUESTAS»
(``parsear_banco``): 50 tests de 20 preguntas por módulo, deduplicados en el banco único que
declara cada módulo. Las páginas del libro profesional se alinean por afinidad de
vocabulario (``paginas_libro``), sin IA.
"""
from __future__ import annotations

import hashlib
import math
import re
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from content_pipeline.processors import guia_docx as G
from content_pipeline.review.ids import lesson_id

_W = G._W
_A = G._A
_R = G._R
_REL = G._REL

FUENTE_LIBRO = "Libro del Nuevo Conductor Profesional (CONASET)"
_MODULO_RE = re.compile(r"^M[ÓO]DULO\s+(\d+)$", re.IGNORECASE)
_UNIDAD_RE = re.compile(r"^Unidad\s+(\d+)\s*·\s*(.+)$")
_IDEAS_RE = re.compile(r"^Ideas clave del m[óo]dulo\s+(\d+)", re.IGNORECASE)
_TIEMPO_RE = re.compile(r"^Tiempo estimado de estudio:\s*(.+)$", re.IGNORECASE)
_NUMERADO_RE = re.compile(r"^(\d+)\.\s+(.*)$")
_INTRO_OMITIR = ("cómo está organizado el curso",)


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------

@dataclass
class Bloque:
    tipo: str                         # p | tabla | img
    estilo: str = ""
    texto: str = ""
    md: str = ""
    lista: bool = False
    nivel: int = 0
    filas: list[list[str]] = field(default_factory=list)
    filas_md: list[list[str]] = field(default_factory=list)
    img: bytes = b""
    ext: str = ""
    pie: str = ""


def _rels(z: zipfile.ZipFile) -> dict[str, str]:
    rels = {}
    if "word/_rels/document.xml.rels" in z.namelist():
        for r in ET.fromstring(z.read("word/_rels/document.xml.rels")).iter(_REL + "Relationship"):
            rels[r.get("Id")] = r.get("Target")
    return rels


def _imagenes(z: zipfile.ZipFile, rels: dict[str, str], el) -> list[tuple[bytes, str]]:
    out = []
    for blip in el.iter(_A + "blip"):
        destino = rels.get(blip.get(_R + "embed"), "")
        nombre = "word/" + destino.lstrip("/") if not destino.startswith("word/") else destino
        ext = destino.rsplit(".", 1)[-1].lower()
        if nombre in z.namelist() and ext in ("png", "jpg", "jpeg", "gif"):
            out.append((z.read(nombre), "jpg" if ext == "jpeg" else ext))
    return out


def nombre_senal(texto: str) -> tuple[str, str]:
    """«PARE. Los conductores…» → («Pare», «Los conductores…»). Sin nombre en mayúsculas → ("", texto)."""
    m = re.match(r"^([^a-záéíóúñ]{3,}?[.:])\s*(.*)$", texto.strip(), re.S)
    if not m or not re.search(r"[A-ZÁÉÍÓÚÑ]{3}", m.group(1)):
        return "", texto.strip()
    nombre = m.group(1).rstrip(".:").strip()
    return nombre[:1] + nombre[1:].lower(), m.group(2).strip()


def leer_bloques(path: str | Path) -> list[Bloque]:
    z = zipfile.ZipFile(path)
    rels = _rels(z)
    body = ET.fromstring(z.read("word/document.xml")).find(_W + "body")
    bloques: list[Bloque] = []
    for el in body:
        if el.tag == _W + "tbl" and any(True for _ in el.iter(_A + "blip")):
            # Catálogo de señales: cada fila [imagen | texto] → figura + su texto.
            for tr in el.findall(_W + "tr"):
                imgs, textos = [], []
                for tc in tr.findall(_W + "tc"):
                    imgs += _imagenes(z, rels, tc)
                    ps = [G._md_parrafo(p) for p in tc.iter(_W + "p")]
                    textos += [x for x in ps if x[0]]
                plano = " ".join(x[0] for x in textos).strip()
                md = " ".join(x[1] for x in textos).strip()
                nombre, _ = nombre_senal(plano)
                for data, ext in imgs:
                    bloques.append(Bloque("img", img=data, ext=ext, pie=nombre or plano[:120]))
                if plano:
                    bloques.append(Bloque("p", estilo="senal", texto=plano, md=md))
            continue
        if el.tag == _W + "p":
            st = el.find(f"{_W}pPr/{_W}pStyle")
            estilo = st.get(_W + "val") if st is not None else ""
            for data, ext in _imagenes(z, rels, el):
                bloques.append(Bloque("img", img=data, ext=ext))
            plano, md = G._md_parrafo(el)
            if plano:
                num = el.find(f"{_W}pPr/{_W}numPr")
                ilvl = el.find(f"{_W}pPr/{_W}numPr/{_W}ilvl")
                lista = estilo.lower().startswith("list") or num is not None
                nivel = int(ilvl.get(_W + "val", "0")) if ilvl is not None else 0
                bloques.append(Bloque("p", estilo=estilo, texto=plano, md=md, lista=lista, nivel=nivel))
        elif el.tag == _W + "tbl":
            filas, filas_md = [], []
            for tr in el.findall(_W + "tr"):
                celdas, celdas_md = [], []
                for tc in tr.findall(_W + "tc"):
                    ps = [G._md_parrafo(p) for p in tc.iter(_W + "p")]
                    ps = [x for x in ps if x[0]]
                    celdas.append("\n".join(x[0] for x in ps))
                    celdas_md.append("\n".join(x[1] for x in ps))
                filas.append(celdas)
                filas_md.append(celdas_md)
            bloques.append(Bloque("tabla", filas=filas, filas_md=filas_md))
    return bloques


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def _es_1x1(b: Bloque) -> bool:
    return b.tipo == "tabla" and len(b.filas) == 1 and len(b.filas[0]) == 1


def _rotulo_1x1(b: Bloque) -> str:
    return (b.filas[0][0].split("\n") or [""])[0].strip()


def recuadro(b: Bloque) -> tuple[str, str]:
    """Tabla 1×1 → (markdown del recuadro, rótulo). «Atención, conductor…» → ATENCION."""
    rotulo = _rotulo_1x1(b)
    lineas = [l.strip() for l in b.filas_md[0][0].split("\n") if l.strip()]
    cuerpo = lineas[1:] if len(lineas) > 1 else lineas
    r = G._norm(rotulo)
    if r.startswith("atencion") or r.startswith("importante"):
        tipo = "ATENCION"
    elif r.startswith("consejo"):
        tipo = "CONSEJO"
    elif r.startswith("norma") or r.startswith("sancion"):
        tipo = "NORMA"
    elif r.startswith("dato"):
        tipo = "DATO"
    else:
        tipo = "DEBES_SABER"
    titulo = rotulo if len(lineas) > 1 and len(rotulo) <= 80 else ""
    # Varios párrafos en el recuadro → viñetas (líneas «> » seguidas se fundirían en un párrafo).
    vineta = "- " if len(cuerpo) > 1 else ""
    md = [f"> [!{tipo}] {titulo}".rstrip()] + [f"> {vineta}{re.sub(r'^[-•]\s*', '', l)}" for l in cuerpo]
    return "\n".join(md), rotulo


def _items_1x1(b: Bloque) -> list[str]:
    return [l.strip() for l in b.filas_md[0][0].split("\n")[1:] if l.strip()]


def _parrafo_md(b: Bloque) -> str:
    """Párrafo → línea markdown (lista con nivel, numerado «1.⇥…» → «1. …»)."""
    texto = b.md or b.texto
    m = _NUMERADO_RE.match(b.texto)
    if m and not b.lista:
        return f"{m.group(1)}. " + re.sub(r"^\d+\.\s+", "", texto)
    if b.lista:
        return "  " * b.nivel + f"- {texto}"
    return texto


def unir_md(lineas: list[str]) -> str:
    """Bloques markdown: items de lista consecutivos van juntos; el resto separado por línea vacía."""
    out: list[str] = []
    for l in lineas:
        es_item = bool(re.match(r"^\s*(?:-|\d+\.)\s", l))
        if out and es_item and re.match(r"^\s*(?:-|\d+\.)\s", out[-1].split("\n")[-1]):
            out[-1] += "\n" + l
        else:
            out.append(l)
    return "\n\n".join(x.strip("\n") for x in out if x.strip())


def minutos(texto: str) -> int:
    h = re.search(r"(\d+)\s*h", texto)
    m = re.search(r"(\d+)\s*min", texto)
    return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)


# ---------------------------------------------------------------------------
# Estructura
# ---------------------------------------------------------------------------

@dataclass
class Leccion:
    orden: int
    nombre: str
    minutos: int = 0
    desarrollo: list[str] = field(default_factory=list)
    repaso: list[str] = field(default_factory=list)
    figuras: list[dict[str, Any]] = field(default_factory=list)
    anexo: bool = False


@dataclass
class Modulo:
    numero: int
    titulo: str = ""
    horas: str = ""
    intro: list[str] = field(default_factory=list)
    lecciones: list[Leccion] = field(default_factory=list)
    rotulos: Counter = field(default_factory=Counter)


@dataclass
class Material:
    intro: list[Bloque] = field(default_factory=list)
    modulos: list[Modulo] = field(default_factory=list)


def _figura(b: Bloque, codigo: str) -> dict[str, Any]:
    h = hashlib.sha1(b.img).hexdigest()
    return {"id": f"F-{h[:10]}", "hash": h, "ext": b.ext, "data": b.img, "pie": b.pie,
            "archivo": f"figuras_{codigo}/{h[:16]}.{b.ext}"}


def parsear_material(path: str | Path, codigo: str) -> Material:
    mat = Material()
    modo = "inicio"                    # inicio | contenido | intro | intro_omitir | pre | leccion
    mod: Modulo | None = None
    lec: Leccion | None = None
    esperando_titulo = None
    for b in leer_bloques(path):
        if b.tipo == "p":
            m = _MODULO_RE.match(b.texto)
            if m:
                esperando_titulo = int(m.group(1))
                continue
            if b.estilo == "Heading1":
                if esperando_titulo is not None:
                    mod = Modulo(numero=esperando_titulo, titulo=b.texto.strip())
                    mat.modulos.append(mod)
                    modo, lec, esperando_titulo = "pre", None, None
                elif G._norm(b.texto) == "contenido":
                    modo = "contenido"
                elif G._norm(b.texto).startswith("introduccion al curso"):
                    modo = "intro"
                continue
            if modo in ("intro", "intro_omitir") and b.estilo == "Heading2":
                modo = "intro_omitir" if G._norm(b.texto) in {G._norm(x) for x in _INTRO_OMITIR} else "intro"
        if modo in ("inicio", "contenido", "intro_omitir"):
            continue
        if modo == "intro":
            mat.intro.append(b)
            continue
        if mod is None:
            continue
        # --- módulo ---
        if b.tipo == "p" and b.estilo == "Heading2":
            mu, mi = _UNIDAD_RE.match(b.texto), _IDEAS_RE.match(b.texto)
            if mu:
                lec = Leccion(orden=int(mu.group(1)), nombre=mu.group(2).strip())
            else:
                lec = Leccion(orden=len(mod.lecciones) + 1, nombre=b.texto.strip(), anexo=bool(mi))
            mod.lecciones.append(lec)
            modo = "leccion"
            continue
        if b.tipo == "p" and _TIEMPO_RE.match(b.texto):
            if lec is not None:
                lec.minutos = minutos(_TIEMPO_RE.match(b.texto).group(1))
            continue
        destino = mod.intro if modo == "pre" else lec.desarrollo
        if b.tipo == "img":
            fg = _figura(b, codigo)
            if modo == "pre":
                continue
            lec.figuras.append(fg)
            destino.append(f"{{{{figura:{fg['id']}}}}}")
        elif b.tipo == "tabla":
            if _es_1x1(b):
                rot = _rotulo_1x1(b)
                nr = G._norm(rot)
                if nr.startswith("horas de este modulo"):
                    mod.horas = " ".join(_items_1x1(b))
                elif nr.startswith("preguntas para repasar") and lec is not None:
                    lec.repaso += _items_1x1(b)
                    mod.rotulos[rot] += 1
                else:
                    md, rot = recuadro(b)
                    mod.rotulos[rot[:40]] += 1
                    destino.append(md)
            else:
                t = G.tabla_md(b)
                if t:
                    destino.append(t)
        elif b.tipo == "p":
            if b.estilo == "Heading3":
                destino.append(f"### {b.texto.strip()}" if modo != "pre" else f"**{b.texto.strip()}**")
            elif b.estilo == "Heading4":
                destino.append(f"**{b.texto.strip()}**")
            elif b.estilo == "senal":
                nombre, resto = nombre_senal(b.texto)
                destino.append(f"**{b.texto.split('.')[0].strip()}.** {resto}" if nombre else b.md)
            else:
                destino.append(_parrafo_md(b))
    return mat


# ---------------------------------------------------------------------------
# Presentación (unidad 0) desde la «Introducción al curso»
# ---------------------------------------------------------------------------

def presentacion_md(mat: Material, *, codigo: str, guia: str) -> str:
    lineas: list[str] = []
    renovacion = ""
    seccion = ""
    decl_puesta = False
    for b in mat.intro:
        if b.tipo == "p" and b.estilo == "Heading2":
            if not decl_puesta and seccion.startswith("requisitos"):
                lineas.append("{{declaracion}}")
                decl_puesta = True
            seccion = G._norm(b.texto)
            lineas.append(f"### {b.texto.strip()}")
        elif b.tipo == "p" and b.estilo in ("Heading3", "Heading4"):
            lineas.append(f"**{b.texto.strip()}**")
        elif b.tipo == "p":
            if "cada 4 anos" in G._norm(b.texto) and not renovacion:
                renovacion = b.texto.strip()
            lineas.append(_parrafo_md(b))
        elif b.tipo == "tabla":
            lineas.append(recuadro(b)[0] if _es_1x1(b) else G.tabla_md(b))
    if not decl_puesta:
        lineas.append("{{declaracion}}")
    lineas.append("{{salud_faq}}")
    if renovacion:
        renovacion = re.sub(r"^Sin embargo,\s*", "", renovacion)
        renovacion = "La licencia tiene duración indefinida, pero " + renovacion[:1].lower() + renovacion[1:]
    else:
        renovacion = ("Las licencias profesionales son de duración indefinida, pero se controlan "
                      "**cada 4 años**.")
    front = ("---\n"
             f"licencia: {codigo}\n"
             "tratamiento: usted\n"
             f"renovacion: {renovacion!r}\n"
             "fuentes:\n"
             f"  - {f'{guia} — Introducción al curso'!r}\n"
             "---\n")
    return front + unir_md(lineas) + "\n"


# ---------------------------------------------------------------------------
# Páginas del libro por afinidad (sin IA)
# ---------------------------------------------------------------------------

def paginas_libro(textos: list[str], pdf: str | Path, *, desde: int = 1, hasta: int | None = None,
                  max_paginas: int = 8) -> list[tuple[int, int, float]]:
    """Por lección: (pág. inicio, pág. fin, cobertura 0-1) del tramo más afín del libro.

    Afinidad = palabras de contenido de la lección presentes en la página, ponderadas por
    su rareza en el libro (idf). Se toma la mejor página y se extiende a las vecinas que
    conservan ≥ 55% de esa afinidad (hasta ``max_paginas``). La cobertura es la fracción
    del vocabulario ponderado de la lección que aparece en el tramo: baja = por confirmar.
    """
    import fitz

    from content_pipeline.services.plan_pages import _TF_TOPE, _tokens

    with fitz.open(str(pdf)) as doc:
        hasta = min(hasta or len(doc), len(doc))
        paginas = [doc[p - 1].get_text("text") for p in range(desde, hasta + 1)]
    pag_tok = [Counter(_tokens(t)) for t in paginas]
    df = Counter(w for c in pag_tok for w in c)
    idf = {w: math.log(1 + len(paginas) / n) for w, n in df.items()}
    out = []
    for texto in textos:
        tf = {w: min(c, _TF_TOPE) for w, c in Counter(_tokens(texto)).items() if w in idf}
        total = sum(idf[w] * c for w, c in tf.items()) or 1.0
        fila = [sum(idf[w] * c for w, c in tf.items() if w in pt) for pt in pag_tok]
        if not any(fila):
            out.append((0, 0, 0.0))
            continue
        mejor = max(range(len(fila)), key=fila.__getitem__)
        ini = fin = mejor
        while fin - ini + 1 < max_paginas:
            izq = fila[ini - 1] if ini > 0 else -1
            der = fila[fin + 1] if fin + 1 < len(fila) else -1
            if max(izq, der) < 0.55 * fila[mejor]:
                break
            if der >= izq:
                fin += 1
            else:
                ini -= 1
        vocab = set().union(*(pag_tok[i] for i in range(ini, fin + 1)))
        cobertura = sum(idf[w] * c for w, c in tf.items() if w in vocab) / total
        out.append((desde + ini, desde + fin, round(cobertura, 2)))
    return out


# ---------------------------------------------------------------------------
# Curso
# ---------------------------------------------------------------------------

def quiz_remite_lecciones(lecciones_modulo: list[dict[str, Any]], titulo_modulo: str) -> tuple[list[dict[str, Any]], str]:
    """(fuentes, descripción) del quiz de un módulo: remite a sus lecciones de lectura.

    No cita páginas del libro (las lecciones de un módulo saltan por todo el libro). Las
    páginas quedan en 1-1 porque el validador las exige > 0; los clientes no muestran las
    fuentes (las usan la Brújula y el admin).
    """
    lecs = [l for l in lecciones_modulo if l.get("tipo") == "texto" and not l.get("anexo")]
    fuentes = [{"fuente_nombre": f"Lección {l['posicion']} · {l['nombre']}"[:255], "pagina_inicio": 1,
                "pagina_fin": 1, "tema_regulatorio": l["nombre"][:255], "fragmento_resumen": ""} for l in lecs]
    descripcion = f"Evaluación de cierre de {titulo_modulo}. Repasa: " + ", ".join(l["nombre"] for l in lecs) + "."
    if len(descripcion) > 255:
        descripcion = descripcion[:252].rsplit(", ", 1)[0] + "…"
    return fuentes or [{"fuente_nombre": titulo_modulo, "pagina_inicio": 1, "pagina_fin": 1,
                        "tema_regulatorio": titulo_modulo, "fragmento_resumen": ""}], descripcion


def armar_curso(mat: Material, *, nombre: str, codigo: str, precio: int, profesional: bool, guia: str,
                ejercicios: list[dict[str, Any]], paginas: dict[tuple[int, int], tuple[int, int, float]] | None = None,
                ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(curso_json, figuras). ``paginas``: {(módulo, posición): (ini, fin, cobertura)}."""
    paginas = paginas or {}
    unidades, lessons, figuras = [], [], []
    for mod in mat.modulos:
        pos = 0
        temas = []
        for i, l in enumerate(mod.lecciones):
            pos += 1
            secciones = []
            if i == 0 and mod.intro:
                secciones.append("## Introducción\n\n" + unir_md(mod.intro))
            secciones.append("## Desarrollo\n\n" + unir_md(l.desarrollo))
            if l.repaso and not l.anexo:
                secciones.append("## Actividad breve\n\n" + "\n".join(f"- {q}" for q in l.repaso))
            ini, fin, cob = paginas.get((mod.numero, pos), (0, 0, 0.0))
            fuente_txt = f"{guia} · Módulo {mod.numero}: {mod.titulo}."
            if ini:
                fuente_txt += f"\n{FUENTE_LIBRO}, páginas {ini}-{fin}."
            secciones.append("## Fuente\n" + fuente_txt)
            contenido = f"# {l.nombre}\n\n" + "\n\n".join(secciones) + "\n"
            figuras += [f for f in l.figuras if f["archivo"] not in {x["archivo"] for x in figuras}]
            temas.append(l.nombre)
            fuentes = ([{"fuente_nombre": FUENTE_LIBRO, "pagina_inicio": ini, "pagina_fin": fin,
                         "tema_regulatorio": l.nombre, "fragmento_resumen": ""}] if ini else
                       [{"fuente_nombre": f"{guia} · Módulo {mod.numero}", "pagina_inicio": 1, "pagina_fin": 1,
                         "tema_regulatorio": l.nombre, "fragmento_resumen": ""}])
            lessons.append({
                "unidad_orden": mod.numero, "unidad_nombre": mod.titulo, "categoria": "General",
                "tema_regulatorio": l.nombre, "nombre": l.nombre[:100], "posicion": pos, "tipo": "texto",
                "descripcion": f"{l.nombre} — dentro de {mod.titulo}."[:255],
                "duracion_min": l.minutos or 15, "contenido": contenido, "transcripcion": "",
                "fuentes": fuentes, "fuente_debil": False, "plan_id": lesson_id(contenido),
                "revision_visual": None, "anexo": l.anexo, "recursos": G._recursos(l.figuras),
                "paginas_afinidad": cob if ini else None,
                "material": {"modulo": mod.numero, "unidad": l.orden},
            })
        # Quiz del módulo: preguntas embebidas (respaldo offline).
        validas = [e for e in ejercicios if e["unidad_orden"] == mod.numero and not e["diferir"]
                   and not e.get("imagen_archivo")]
        paso = max(1, len(validas) // 10)
        muestra = validas[::paso][:10]
        pos += 1
        fuentes_q, descripcion_q = quiz_remite_lecciones(
            [x for x in lessons if x["unidad_orden"] == mod.numero], mod.titulo)
        lessons.append({
            "unidad_orden": mod.numero, "unidad_nombre": mod.titulo, "categoria": "General",
            "tema_regulatorio": f"Evaluación módulo {mod.numero}", "nombre": f"Evaluación del módulo {mod.numero}",
            "posicion": pos, "tipo": "quiz", "descripcion": descripcion_q,
            "duracion_min": 10,
            "contenido": {"questions": [{
                "question": e["pregunta"], "options": list(e["opciones"].values()),
                "correct_index": list(e["opciones"]).index(e["respuestas"][0]),
                "explanation": e["explicacion"],
            } for e in muestra]},
            "transcripcion": "",
            "fuentes": fuentes_q,
            "fuente_debil": False, "anexo": False,
        })
        minutos_mod = sum(l.minutos for l in mod.lecciones)
        unidades.append({"orden": mod.numero, "nombre": mod.titulo, "categoria": "General",
                         "horas_elearning": round(minutos_mod / 60, 1), "temas": temas,
                         "descripcion": mod.horas[:255]})
    curso = {
        "manifest": {
            "curso": {"nombre": nombre, "codigo": codigo, "is_profesional": profesional, "costo": precio,
                      "descripcion": f"{nombre}: " + "; ".join(m.titulo for m in mat.modulos) + "."},
            "unidades": unidades,
        },
        "lessons": lessons,
        "auditoria": {},
        "revision_humana": [],
    }
    return curso, figuras


# ---------------------------------------------------------------------------
# Banco desde la «Práctica de test … CON_RESPUESTAS»
# ---------------------------------------------------------------------------

_CIRCULO = {chr(0x24D0 + i): "abcdefgh"[i] for i in range(8)}          # ⓐ…ⓗ
_PREG_RE = re.compile(r"^(\d+)\.\s+(.+)$")
_OPC_RE = re.compile(r"^([a-h])\)\s*(.+)$")
_CONASET_RE = re.compile(r"\s*·\s*Cuestionario CONASET\s*$", re.IGNORECASE)
_TEST_RE = re.compile(r"^M[óo]dulo\s+(\d+)\s*·\s*Test\s+\d+", re.IGNORECASE)
_MOD_TITLE_RE = re.compile(r"^M[óo]dulo\s+(\d+)\s*·", re.IGNORECASE)
_BANCO_RE = re.compile(r"Banco de\s+(\d+)\s+preguntas", re.IGNORECASE)


def parsear_banco(path: str | Path, *, codigo: str, fuente_guia: str,
                  fuente_oficial: str = "Cuestionario oficial CONASET") -> tuple[list[dict[str, Any]], dict[int, int], list[dict[str, Any]]]:
    """(ejercicios únicos, banco declarado por módulo, imágenes {hash, ext, data, archivo})."""
    from content_pipeline.exporters.django_importer import clave_ejercicio

    z = zipfile.ZipFile(path)
    rels = _rels(z)
    body = ET.fromstring(z.read("word/document.xml")).find(_W + "body")
    declarados: dict[int, int] = {}
    modulo = None
    en_test = False
    actual: dict[str, Any] | None = None
    preguntas: list[dict[str, Any]] = []

    def cerrar():
        nonlocal actual
        if actual and actual["opciones"]:
            preguntas.append(actual)
        actual = None

    for el in body:
        if el.tag != _W + "p":
            continue
        st = el.find(f"{_W}pPr/{_W}pStyle")
        estilo = st.get(_W + "val") if st is not None else ""
        texto = "".join(t.text or "" for t in el.iter(_W + "t")).strip()
        if estilo in ("Title", "Heading1", "Heading2"):
            cerrar()
            if "pauta" in G._norm(texto):
                break
            mt = _TEST_RE.match(texto)
            if mt:
                modulo, en_test = int(mt.group(1)), True
                continue
            mm = _MOD_TITLE_RE.match(texto)
            if mm:
                modulo, en_test = int(mm.group(1)), False
            mb = _BANCO_RE.search(texto)
            if mb and modulo is not None:
                declarados[modulo] = int(mb.group(1))
            continue
        if not en_test or modulo is None:
            continue
        imgs = _imagenes(z, rels, el)
        if imgs and actual is not None:
            actual["imagenes"] += imgs
        if not texto:
            continue
        mo = _OPC_RE.match(texto)
        if texto[:1] in _CIRCULO and actual is not None:
            letra = _CIRCULO[texto[0]]
            actual["opciones"].append((texto[1:].strip(), True))
            continue
        if mo and actual is not None:
            actual["opciones"].append((mo.group(2).strip(), False))
            continue
        mp = _PREG_RE.match(texto)
        if mp:
            cerrar()
            enunciado = mp.group(2).strip()
            oficial = bool(_CONASET_RE.search(enunciado))
            actual = {"modulo": modulo, "pregunta": _CONASET_RE.sub("", enunciado).strip(),
                      "oficial": oficial, "opciones": [], "imagenes": []}
    cerrar()

    # Banco único por módulo: misma pregunta = mismo enunciado + mismas alternativas (en cualquier orden).
    unicos: dict[tuple[int, str], dict[str, Any]] = {}
    imagenes: dict[str, dict[str, Any]] = {}
    for p in preguntas:
        opciones = {"abcdefgh"[i]: t for i, (t, _) in enumerate(p["opciones"])}
        correctas = [t for t, ok in p["opciones"] if ok]
        clave = clave_ejercicio(p["pregunta"], opciones)
        k = (p["modulo"], clave)
        if k not in unicos:
            unicos[k] = {"p": p, "opciones": opciones, "correctas": Counter(), "vistas": 0}
        unicos[k]["vistas"] += 1
        unicos[k]["correctas"][correctas[0] if len(correctas) == 1 else "|".join(correctas)] += 1
    ejercicios = []
    for (mod_n, clave), u in unicos.items():
        p, opciones = u["p"], u["opciones"]
        motivo = None
        if len(u["correctas"]) != 1:
            motivo = "la correcta cambia entre tests: " + "; ".join(u["correctas"])
        correcta = next(iter(u["correctas"]))
        letra = next((l for l, t in opciones.items() if t == correcta), None)
        if letra is None and not motivo:
            motivo = "sin alternativa marcada como correcta"
        h = hashlib.sha1(clave.encode("utf-8")).hexdigest()[:10]
        item = {
            "numero": f"m{mod_n}-{h}", "pregunta": p["pregunta"], "opciones": opciones,
            "respuestas": [letra] if letra and not motivo else [], "multi": False, "explicacion": "",
            "tiene_imagen": bool(p["imagenes"]), "diferir": bool(motivo), "motivo": motivo,
            "unidad_orden": mod_n, "grupo": f"{codigo.lower()}-m{mod_n}-{h}",
            "fuente": fuente_oficial if p["oficial"] else fuente_guia, "veces_en_tests": u["vistas"],
        }
        if p["imagenes"]:
            data, ext = p["imagenes"][0]
            hi = hashlib.sha1(data).hexdigest()
            archivo = f"figuras_{codigo}/{hi[:16]}.{ext}"
            imagenes.setdefault(archivo, {"hash": hi, "ext": ext, "data": data, "archivo": archivo})
            item["imagen_archivo"] = archivo
        ejercicios.append(item)
    return ejercicios, declarados, list(imagenes.values())
