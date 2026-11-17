"""Presentación inicial «Requisitos y exámenes» de cada curso (unidad 0 «Antes de empezar»).

Fuente versionada en ``content_pipeline/presentaciones/``:

- ``<LICENCIA>.md``: parte propia de la licencia (qué habilita, requisitos, camino,
  exámenes, qué llevar). Front matter YAML con ``licencia``, ``renovacion`` (respuesta
  a «¿Cada cuánto se renueva?») y ``fuentes`` (lista para «## Fuente»).
- ``comun_declaracion.md``: la declaración jurada (en ``{{declaracion}}``; si el archivo
  de la licencia no trae el marcador, va antes de «### El camino a la licencia»).
- ``comun_salud_faq.md``: celular, salud y preguntas frecuentes (en ``{{salud_faq}}``;
  si falta el marcador, al final).
- ``medios.json``: URLs públicas de los PDF (``pdf_salud``, ``pdf_drogas``), que sube
  ``presentacion_curso --subir-medios``.

La licencia sale del código del curso (``A4`` → ``A4.md``; ``C1`` → ``C.md`` si no hay
``C1.md``).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

CARPETA = Path(__file__).resolve().parent.parent / "presentaciones"
UNIDAD_NOMBRE = "Antes de empezar"
LECCION_NOMBRE = "Requisitos y exámenes"
PDFS = {"pdf_salud": ("Declaracion-Estado-de-Salud-Anexo1.pdf", "Declaración de estado de salud (Anexo 1)"),
        "pdf_drogas": ("Declaracion-Jurada-No-Consumo-de-Drogas.pdf", "Modelo de declaración jurada de no consumo de drogas")}


class PresentacionError(ValueError):
    pass


def archivo_licencia(codigo: str, carpeta: Path = CARPETA) -> Path:
    codigo = (codigo or "").strip().upper()
    for cand in (codigo, re.sub(r"\d+$", "", codigo)):
        if cand and (carpeta / f"{cand}.md").exists():
            return carpeta / f"{cand}.md"
    raise PresentacionError(f"No hay presentación para «{codigo}» en {carpeta} ({codigo}.md).")


def leer(path: Path) -> tuple[dict[str, Any], str]:
    texto = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", texto, re.S)
    if not m:
        raise PresentacionError(f"{path.name}: falta el front matter (licencia, renovacion, fuentes).")
    meta = yaml.safe_load(m.group(1)) or {}
    for campo in ("renovacion", "fuentes"):
        if not meta.get(campo):
            raise PresentacionError(f"{path.name}: falta «{campo}» en el front matter.")
    return meta, texto[m.end():].strip()


def medios(carpeta: Path = CARPETA) -> dict[str, str]:
    p = carpeta / "medios.json"
    datos = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    faltan = [k for k in PDFS if not str(datos.get(k, "")).startswith("http")]
    if faltan:
        raise PresentacionError(f"Faltan las URL de {', '.join(faltan)} en {p}: corre --subir-medios.")
    return datos


def componer(codigo: str, urls: dict[str, str], carpeta: Path = CARPETA) -> dict[str, Any]:
    """Lección lista para importar: ``{nombre, contenido, recursos, fuentes, licencia}``."""
    meta, cuerpo = leer(archivo_licencia(codigo, carpeta))
    decl = (carpeta / "comun_declaracion.md").read_text(encoding="utf-8").strip()
    faq = (carpeta / "comun_salud_faq.md").read_text(encoding="utf-8").strip()
    faq = faq.replace("{{renovacion}}", str(meta["renovacion"]).strip())
    if "{{declaracion}}" in cuerpo:
        cuerpo = cuerpo.replace("{{declaracion}}", decl)
    elif "\n### El camino a la licencia" in cuerpo:
        cuerpo = cuerpo.replace("\n### El camino a la licencia", f"\n{decl}\n\n### El camino a la licencia", 1)
    else:
        cuerpo += "\n\n" + decl
    cuerpo = cuerpo.replace("{{salud_faq}}", faq) if "{{salud_faq}}" in cuerpo else cuerpo + "\n\n" + faq
    for clave, url in urls.items():
        cuerpo = cuerpo.replace("{{" + clave + "}}", url)
    sueltos = re.findall(r"\{\{(?!figura:)[^}]+\}\}", cuerpo)
    if sueltos:
        raise PresentacionError(f"Marcadores sin reemplazar: {', '.join(sorted(set(sueltos)))}")
    fuentes = "\n".join(f"- {f}" for f in meta["fuentes"])
    contenido = f"# {LECCION_NOMBRE}\n\n## Desarrollo\n\n{cuerpo.strip()}\n\n## Fuente\n{fuentes}\n"

    from content_pipeline.processors.content_blocks import validate_components
    errores, _ = validate_components(contenido)
    if errores:
        raise PresentacionError("Componentes inválidos: " + "; ".join(errores))
    # Los PDF van solo como enlaces: un recurso pdf/descarga se copiaría a ``url_pdf``
    # (espejo de compatibilidad) y los clientes lo mostrarían como el PDF de la lección.
    recursos: list[dict[str, Any]] = []
    return {"nombre": LECCION_NOMBRE, "contenido": contenido, "recursos": recursos,
            "fuentes": list(meta["fuentes"]), "licencia": meta.get("licencia") or codigo}



# ---------------------------------------------------------------------------
# Audio de la presentación (``presentacion_curso --audio``)
# ---------------------------------------------------------------------------
# ``audio.json`` (versionado, como ``medios.json``) guarda por licencia el guion y la
# URL del audio, más la huella del texto con que se generó. Así prod enlaza el audio
# sin claves de TTS, y un cambio en los .md lo marca como desactualizado.

def audio_cache(carpeta: Path | None = None) -> dict[str, dict[str, Any]]:
    p = (carpeta or CARPETA) / "audio.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def guardar_audio_cache(datos: dict[str, dict[str, Any]], carpeta: Path | None = None) -> Path:
    p = (carpeta or CARPETA) / "audio.json"
    p.write_text(json.dumps(datos, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


def hash_texto(spec: dict[str, Any]) -> str:
    from content_pipeline.media.narration import content_hash

    return content_hash(spec["contenido"])


def audio_vigente(spec: dict[str, Any], carpeta: Path | None = None) -> tuple[dict[str, Any] | None, bool]:
    """``(entrada, vigente)`` del audio de la licencia de ``spec`` en ``audio.json``."""
    entrada = audio_cache(carpeta).get(str(spec["licencia"]))
    if not entrada:
        return None, False
    return entrada, entrada.get("hash_texto") == hash_texto(spec)


def aplicar_audio(spec: dict[str, Any], entrada: dict[str, Any]) -> dict[str, Any]:
    """Agrega a ``spec`` la transcripción y el recurso de narración de ``entrada``."""
    meta = {"archivo": entrada.get("archivo", "")}
    if entrada.get("duracion_seg"):
        meta["duracion_seg"] = entrada["duracion_seg"]
    spec = {**spec, "transcripcion": entrada.get("transcripcion", "")}
    spec["recursos"] = [r for r in spec.get("recursos") or [] if r.get("tipo") != "audio"] + [{
        "tipo": "audio", "rol": "narracion", "clave": "audio", "url": entrada["url"],
        "orden": 1000, "titulo": "Narración de la lección", "meta": meta,
    }]
    return spec
