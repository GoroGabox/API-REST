"""Curso ya redactado como HTML interactivo (Cursos-Licencias-C-y-D) → JSON de curso + ejercicios.

Conversión FIEL, como ``guia_docx``: el texto no se reescribe. El HTML guarda todo
en su ``<script>``: ``MODULES`` (``{id, group: intro|core|extra, title, html}``),
``SVG`` (láminas, insertadas con ``fig(k, pie)``), ``SIGNS`` (señales en webp) y el
banco ``Q`` (propias + oficiales CONASET). ``curso_html_extract.js`` (node, contexto
vm vacío) evalúa solo esa parte de datos y la entrega en JSON.

- Cada módulo ``core`` es una unidad; los ``extra`` se juntan en una unidad final.
  El módulo ``intro`` (requisitos y exámenes) no se convierte aquí: lo cubre la
  presentación de ``content_pipeline/presentaciones`` (comando ``presentacion_curso``).
- Lecciones = secciones ``h3`` consecutivas hasta ~``LARGO_LECCION`` caracteres
  (módulo corto = una lección). ``## Desarrollo`` con ``### sección``.
- ``fig(k, pie)`` → PNG rasterizado con PyMuPDF + marcador ``{{figura:F-…}}``;
  las señales del banco (webp) también van como figuras en la lección de señales.
- ``div.key`` → recuadro; ``table`` → tabla GFM; ``h4`` → negrita.
- Objetivo, Puntos clave y Resumen no vienen: los agrega
  ``guia_docx.completar_secciones`` (IA auditada por cifras).
- Banco → ejercicios para ``import_ejercicios --curso`` (unidad por ``mod``).
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from content_pipeline.processors.guia_docx import _recursos
from content_pipeline.review.ids import lesson_id

LARGO_LECCION = 4000          # caracteres de Desarrollo por lección (objetivo)
MODULO_CORTO = 5000           # un módulo bajo este largo queda en una sola lección
PREGUNTAS_QUIZ = 10
_EXTRACTOR = Path(__file__).with_name("curso_html_extract.js")

# Colores del tema claro del HTML (las láminas usan var(--…)).
_VARS = {"--surface": "#ffffff", "--fg": "#1d1f22", "--muted": "#5d6168", "--line": "#d9d7cf",
         "--accent": "#e8a400", "--accent-ink": "#1d1f22", "--steel": "#3b4450", "--tint": "#fbf3dc",
         "--bg": "#f3f2ee", "--ok": "#1f7a3f", "--ok-bg": "#e3f2e8", "--bad": "#b3261e", "--bad-bg": "#fbe5e3"}


def extraer(html_path: str | Path) -> dict[str, Any]:
    """Datos del ``<script>`` del curso: ``{modules, svg, signs, q}`` (vía node)."""
    r = subprocess.run(["node", str(_EXTRACTOR), str(html_path)], capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"No se pudo leer el script del HTML: {r.stderr.decode('utf-8', 'replace')[:500]}")
    return json.loads(r.stdout.decode("utf-8"))


def rasterizar_svg(svg: str, escala: float = 2.0) -> bytes:
    """SVG de una lámina → PNG (PyMuPDF), resolviendo las variables CSS del tema claro."""
    import pymupdf

    s = re.sub(r"var\((--[\w-]+)\)", lambda m: _VARS.get(m.group(1), "#1d1f22"), svg)
    if "xmlns=" not in s[:200]:
        s = s.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    doc = pymupdf.open(stream=s.encode("utf-8"), filetype="svg")
    pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(escala, escala), alpha=False)
    return pix.tobytes("png")


# ---------------------------------------------------------------------------
# HTML del módulo → bloques Markdown
# ---------------------------------------------------------------------------

@dataclass
class Bloque:
    tipo: str                 # h3 | md | fig
    texto: str = ""
    fig: dict[str, Any] | None = None


def _limpio(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()


class _Conversor(HTMLParser):
    """HTML acotado del curso (h3/h4/p/ul/ol/li/table/div.key/figure/b) → bloques."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bloques: list[Bloque] = []
        self.inline: list[str] | None = None     # buffer del elemento de texto abierto
        self.destino = ""                        # h3 | h4 | p | li | td | th | cap | key_t
        self.lista: list[str] = []               # ol | ul anidadas
        self.items: list[str] = []
        self.tabla: list[list[tuple[str, bool]]] | None = None
        self.key: dict[str, Any] | None = None   # recuadro abierto
        self.figura: dict[str, Any] | None = None
        self.ignorar = 0

    # -- utilidades ---------------------------------------------------------
    def _abrir(self, destino: str):
        self.inline, self.destino = [], destino

    def _texto(self) -> str:
        t = _limpio("".join(self.inline or []))
        t = re.sub(r"\*\*(\s*)\*\*", r"\1", t)          # negritas vacías o pegadas
        self.inline, self.destino = None, ""
        return t

    def _md(self, texto: str):
        if not texto:
            return
        if self.key is not None:
            self.key["partes"].append(texto)
        else:
            self.bloques.append(Bloque("md", texto))

    # -- parser -------------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.ignorar:
            if tag in ("div", "svg"):
                self.ignorar += 1
            return
        cls = a.get("class") or ""
        if tag in ("h3", "h4", "p") and self.inline is None:
            self._abrir(tag)
        elif tag in ("ul", "ol"):
            self.lista.append(tag)
            if len(self.lista) == 1:
                self.items = []
        elif tag == "li":
            self._abrir("li")
        elif tag == "table":
            self.tabla = []
        elif tag == "tr" and self.tabla is not None:
            self.tabla.append([])
        elif tag in ("td", "th") and self.tabla is not None:
            self._abrir(tag)
        elif tag == "div" and "key" in cls.split():
            self.key = {"titulo": "", "partes": []}
        elif tag == "strong" and self.key is not None and not self.key["titulo"] and self.inline is None:
            self._abrir("key_t")
        elif tag == "figure":
            self.figura = {"svg": a.get("data-svg"), "src": None, "pie": ""}
        elif tag == "img" and self.figura is not None:
            self.figura["src"] = a.get("src")
            self.figura["alt"] = a.get("alt") or ""
        elif tag == "figcaption":
            self._abrir("cap")
        elif tag in ("b", "strong") and self.inline is not None:
            self.inline.append("**")
        elif tag == "br" and self.inline is not None:
            self.inline.append(" ")
        elif tag == "svg":
            self.ignorar = 1

    def handle_endtag(self, tag):
        if self.ignorar:
            if tag in ("div", "svg"):
                self.ignorar -= 1
            return
        if tag in ("b", "strong") and self.inline is not None and self.destino != "key_t":
            self.inline.append("**")
        elif tag == "strong" and self.destino == "key_t":
            self.key["titulo"] = self._texto()
        elif tag in ("h3", "h4", "p") and self.destino == tag:
            t = self._texto()
            if tag == "h3":
                self.bloques.append(Bloque("h3", t.replace("**", "")))
            elif tag == "h4":
                self._md(f"**{t.replace('**', '')}**")
            else:
                self._md(t)
        elif tag == "li" and self.destino == "li":
            t = self._texto()
            marca = "1." if self.lista and self.lista[-1] == "ol" else "-"
            sangria = "  " * (len(self.lista) - 1)
            self.items.append(f"{sangria}{marca} {t}")
        elif tag in ("ul", "ol") and self.lista:
            self.lista.pop()
            if not self.lista:
                self._md("\n".join(self.items))
                self.items = []
        elif tag in ("td", "th") and self.destino == tag:
            self.tabla[-1].append((self._texto().replace("|", "/"), tag == "th"))
        elif tag == "table" and self.tabla is not None:
            filas = [f for f in self.tabla if f]
            self.tabla = None
            if filas:
                n = max(len(f) for f in filas)
                filas = [f + [("", False)] * (n - len(f)) for f in filas]
                cab, cuerpo = filas[0], filas[1:]
                if not cuerpo:          # tabla de una fila (pasos o datos sueltos) → lista
                    self._md("\n".join(f"- {c}" for c, _ in cab if c))
                    return
                lineas = ["| " + " | ".join(c for c, _ in cab) + " |", "|" + " --- |" * n]
                lineas += ["| " + " | ".join(c for c, _ in f) + " |" for f in cuerpo]
                self._md("\n".join(lineas))
        elif tag == "div" and self.key is not None and self.inline is None and not self.lista \
                and self.tabla is None and self.figura is None:
            k, self.key = self.key, None
            titulo = k["titulo"]
            tipo = "CONSEJO" if titulo.lower().startswith("consejo") else "DEBES_SABER"
            cuerpo = "\n>\n".join("> " + p.replace("\n", "\n> ") for p in k["partes"])
            self.bloques.append(Bloque("md", f"> [!{tipo}] {titulo}".rstrip() + ("\n" + cuerpo if cuerpo else "")))
        elif tag == "figcaption" and self.destino == "cap":
            self.figura["pie"] = self._texto().replace("**", "")
        elif tag == "figure" and self.figura is not None:
            f, self.figura = self.figura, None
            self.bloques.append(Bloque("fig", fig=f))

    def handle_data(self, data):
        if self.ignorar:
            return
        if self.inline is not None:
            self.inline.append(data)


def html_a_bloques(html: str) -> list[Bloque]:
    c = _Conversor()
    c.feed(html)
    c.close()
    return c.bloques


# ---------------------------------------------------------------------------
# Estructura del curso
# ---------------------------------------------------------------------------

@dataclass
class Seccion:
    titulo: str
    bloques: list[Bloque] = field(default_factory=list)

    def largo(self) -> int:
        return sum(len(b.texto) for b in self.bloques)


def secciones(bloques: list[Bloque]) -> list[Seccion]:
    out = [Seccion("")]
    for b in bloques:
        if b.tipo == "h3":
            out.append(Seccion(b.texto))
        else:
            out[-1].bloques.append(b)
    if not out[0].bloques:
        out.pop(0)
    elif len(out) > 1:
        # Texto introductorio del módulo: va al inicio de la primera sección.
        out[1].bloques = out[0].bloques + out[1].bloques
        out[1].intro = True
        out.pop(0)
    return out


def agrupar(secs: list[Seccion], titulo_modulo: str) -> list[tuple[str, list[Seccion]]]:
    """Secciones consecutivas → lecciones de ~LARGO_LECCION caracteres."""
    total = sum(s.largo() for s in secs)
    if total < MODULO_CORTO or len(secs) <= 1:
        return [(titulo_modulo, secs)]
    grupos: list[list[Seccion]] = [[]]
    for s in secs:
        actual = sum(x.largo() for x in grupos[-1])
        if grupos[-1] and actual + s.largo() > LARGO_LECCION and actual >= LARGO_LECCION * 0.4:
            grupos.append([])
        grupos[-1].append(s)
    # Un último grupo muy corto se une al anterior.
    if len(grupos) > 1 and sum(x.largo() for x in grupos[-1]) < LARGO_LECCION * 0.3:
        grupos[-2] += grupos.pop()
    out = []
    for k, g in enumerate(grupos, start=1):
        titulos = [_nombre_corto(s.titulo, "") for s in g if s.titulo]
        if len(titulos) == 1:
            nombre = titulos[0]
        else:
            nombre = ", ".join(titulos[:-1]) + " y " + titulos[-1] if titulos else ""
            if not nombre or len(nombre) > 90:
                nombre = f"{titulo_modulo} (parte {k})"
        out.append((_nombre_corto(nombre, titulo_modulo), g))
    return out


def _nombre_corto(nombre: str, respaldo: str) -> str:
    nombre = re.sub(r"^\d+(\.\d+)*\s+", "", nombre).strip() or respaldo
    if len(nombre) > 95:
        nombre = nombre[:92].rsplit(" ", 1)[0] + "…"
    return nombre


@dataclass
class Unidad:
    orden: int
    titulo: str
    modulos: list[dict[str, Any]]
    paginas: tuple[int, int] | None = None


def armar_curso(datos: dict[str, Any], *, nombre: str, codigo: str, precio: int, profesional: bool,
                fuente: str, libro: str = "", paginas_unidad: dict[int, tuple[int, int]] | None = None,
                titulo_extras: str = "Otras maquinarias", fuente_banco: str = "",
                fuente_oficial: str = "Cuestionario oficial CONASET (clases A1, A2, D y E)",
                ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """(curso_json, ejercicios, figuras, mod→unidad) desde los datos de ``extraer``."""
    paginas_unidad = paginas_unidad or {}
    core = [m for m in datos["modules"] if m["group"] == "core"]
    extra = [m for m in datos["modules"] if m["group"] == "extra"]
    unidades = [Unidad(i, m["title"], [m]) for i, m in enumerate(core, start=1)]
    if extra:
        unidades.append(Unidad(len(core) + 1, titulo_extras, extra))
    mod_unidad = {m["id"]: u.orden for u in unidades for m in u.modulos}

    lessons, figuras, manifest_u = [], [], []
    por_svg: dict[str, dict[str, Any]] = {}

    def figura_de(b: Bloque) -> dict[str, Any] | None:
        f = b.fig or {}
        if f.get("svg"):
            svg = datos["svg"].get(f["svg"])
            if not svg:
                return None
            if f["svg"] not in por_svg:
                data, ext = rasterizar_svg(svg), "png"
                h = hashlib.sha1(data).hexdigest()
                por_svg[f["svg"]] = {"hash": h, "ext": ext, "data": data,
                                     "archivo": f"figuras_{codigo}/{h[:16]}.{ext}"}
            base = por_svg[f["svg"]]
        elif (f.get("src") or "").startswith("data:image/"):
            cab, b64 = f["src"].split(",", 1)
            ext = cab.split("/")[1].split(";")[0]
            data = base64.b64decode(b64)
            h = hashlib.sha1(data).hexdigest()
            base = {"hash": h, "ext": ext, "data": data, "archivo": f"figuras_{codigo}/{h[:16]}.{ext}"}
        else:
            return None
        pie = f.get("pie") or ""
        return {**base, "id": f"F-{base['hash'][:10]}", "pie": pie, "alt": f.get("alt") or pie}

    for u in unidades:
        pos = 0
        pags = paginas_unidad.get(u.orden)
        fuentes_u = ([{"fuente_nombre": libro, "pagina_inicio": pags[0], "pagina_fin": pags[1],
                       "tema_regulatorio": u.titulo, "fragmento_resumen": ""}] if pags and libro else
                     [{"fuente_nombre": fuente, "pagina_inicio": 1, "pagina_fin": 1,
                       "tema_regulatorio": u.titulo, "fragmento_resumen": ""}])
        temas = []
        for m in u.modulos:
            secs = secciones(html_a_bloques(m["html"]))
            grupos = agrupar(secs, m["title"]) if len(u.modulos) == 1 else [(m["title"], secs)]
            for nombre_l, grupo in grupos:
                pos += 1
                partes, figs = [], []
                for s in grupo:
                    if s.titulo and not (len(grupo) == 1 and _nombre_corto(s.titulo, "") == nombre_l
                                         and not getattr(s, "intro", False)):
                        partes.append(f"### {s.titulo}")
                    for b in s.bloques:
                        if b.tipo == "fig":
                            fg = figura_de(b)
                            if fg is None:
                                continue
                            if fg["id"] not in {x["id"] for x in figs}:
                                figs.append(fg)
                                partes.append(f"{{{{figura:{fg['id']}}}}}")
                        elif b.texto:
                            partes.append(b.texto)
                desarrollo = "\n\n".join(partes)
                fuente_txt = f"{fuente}, módulo «{m['title']}»."
                if pags and libro:
                    fuente_txt += f"\n{libro}, páginas {pags[0]}-{pags[1]}."
                contenido = f"# {nombre_l}\n\n## Desarrollo\n\n{desarrollo}\n\n## Fuente\n{fuente_txt}\n"
                figuras += [f for f in figs if f["archivo"] not in {x["archivo"] for x in figuras}]
                temas.append(nombre_l)
                palabras = len(re.sub(r"[#>*|{}\-]", " ", desarrollo).split())
                lessons.append({
                    "unidad_orden": u.orden, "unidad_nombre": u.titulo, "categoria": "General",
                    "tema_regulatorio": nombre_l, "nombre": nombre_l, "posicion": pos, "tipo": "texto",
                    "descripcion": f"{nombre_l} — dentro de {u.titulo}."[:255],
                    "duracion_min": max(5, round(palabras / 180)), "contenido": contenido,
                    "transcripcion": "",
                    "fuentes": [dict(f, tema_regulatorio=nombre_l) for f in fuentes_u],
                    "fuente_debil": False, "plan_id": lesson_id(contenido), "revision_visual": None,
                    "anexo": False, "recursos": _recursos_html(figs),
                    "html": {"modulo": m["id"]},
                })
        manifest_u.append({"orden": u.orden, "nombre": u.titulo, "categoria": "General",
                           "horas_elearning": round(sum(l["duracion_min"] for l in lessons
                                                        if l["unidad_orden"] == u.orden) / 60, 1),
                           "temas": temas})

    ejercicios = banco(datos, codigo=codigo, mod_unidad=mod_unidad, figuras=figuras,
                       fuente_banco=fuente_banco or f"Curso {codigo} AutoTest", fuente_oficial=fuente_oficial)

    # Quiz por unidad (preguntas embebidas: respaldo offline / unidad sin banco suficiente).
    for u in unidades:
        validas = [e for e in ejercicios if e["unidad_orden"] == u.orden and not e.get("imagen_archivo")]
        paso = max(1, len(validas) // PREGUNTAS_QUIZ)
        muestra = validas[::paso][:PREGUNTAS_QUIZ]
        pos = max((l["posicion"] for l in lessons if l["unidad_orden"] == u.orden), default=0) + 1
        pags = paginas_unidad.get(u.orden)
        lessons.append({
            "unidad_orden": u.orden, "unidad_nombre": u.titulo, "categoria": "General",
            "tema_regulatorio": f"Evaluación unidad {u.orden}", "nombre": f"Evaluación de la unidad {u.orden}",
            "posicion": pos, "tipo": "quiz", "descripcion": f"Evaluación de cierre de {u.titulo}."[:255],
            "duracion_min": 10,
            "contenido": {"questions": [{
                "question": e["pregunta"], "options": list(e["opciones"].values()),
                "correct_index": list(e["opciones"]).index(e["respuestas"][0]),
                "explanation": e["explicacion"],
            } for e in muestra]},
            "transcripcion": "",
            "fuentes": [{"fuente_nombre": libro if pags and libro else fuente,
                         "pagina_inicio": pags[0] if pags and libro else 1,
                         "pagina_fin": pags[1] if pags and libro else 1,
                         "tema_regulatorio": f"Evaluación unidad {u.orden}", "fragmento_resumen": ""}],
            "fuente_debil": False, "anexo": False,
        })

    lessons.sort(key=lambda l: (l["unidad_orden"], l["posicion"]))
    curso = {
        "manifest": {
            "curso": {"nombre": nombre, "codigo": codigo, "is_profesional": profesional, "costo": precio,
                      "descripcion": f"{nombre}: " + "; ".join(u.titulo for u in unidades) + "."},
            "unidades": manifest_u,
        },
        "lessons": lessons,
        "auditoria": {},
        "revision_humana": [],
    }
    return curso, ejercicios, figuras, mod_unidad


def _recursos_html(figs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = _recursos(figs)
    for r, f in zip(out, figs):
        r["meta"].update({"origen": "html", "alt": f.get("alt") or f.get("pie") or ""})
    return out


def banco(datos: dict[str, Any], *, codigo: str, mod_unidad: dict[str, int], figuras: list[dict[str, Any]],
          fuente_banco: str, fuente_oficial: str) -> list[dict[str, Any]]:
    """Preguntas del HTML → ítems de ``import_ejercicios`` (una por grupo, sin variantes)."""
    out = []
    for q in datos["q"]:
        letras = "abcdef"[: len(q["o"])]
        opciones = {letras[i]: str(o).strip() for i, o in enumerate(q["o"])}
        oficial = bool(q.get("off"))
        ident = f"o{q['n']}" if oficial else f"p{q['id'] if not str(q['id']).startswith('p') else q['id'][1:]}"
        unidad = mod_unidad.get(q["mod"])
        item = {
            "numero": ident, "pregunta": q["q"].strip(), "opciones": opciones,
            "respuestas": [letras[int(q["a"])]], "multi": False, "explicacion": (q.get("e") or "").strip(),
            "tiene_imagen": bool(q.get("img")), "diferir": unidad is None,
            "motivo": None if unidad is not None else f"módulo «{q['mod']}» sin unidad",
            "unidad_orden": unidad, "grupo": f"{codigo.lower()}-{ident}",
            "fuente": fuente_oficial if oficial else fuente_banco,
        }
        if q.get("img"):
            src = datos["signs"].get(str(q["img"]))
            if not src:
                item.update(diferir=True, motivo=f"imagen {q['img']} no está en SIGNS")
            else:
                cab, b64 = src.split(",", 1)
                ext = cab.split("/")[1].split(";")[0]
                data = base64.b64decode(b64)
                h = hashlib.sha1(data).hexdigest()
                archivo = f"figuras_{codigo}/{h[:16]}.{ext}"
                if archivo not in {f["archivo"] for f in figuras}:
                    figuras.append({"hash": h, "ext": ext, "data": data, "archivo": archivo,
                                    "id": f"F-{h[:10]}", "pie": "", "alt": ""})
                item["imagen_archivo"] = archivo
        out.append(item)
    return out
