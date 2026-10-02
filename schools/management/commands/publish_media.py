"""Publica los medios del curso (figuras, PDFs, audio local) y rellena sus URLs.

Último paso antes de `import_course`: recorre `lessons[].recursos`, sube al storage
(`--storage local|s3`) cada recurso con `archivo_local` y sin `url`, y escribe la URL
pública. Idempotente: un archivo ya publicado se reutiliza (no se re-sube).

Con `--paginas-libro --contenido libro.pdf` además arma, por lección, un PDF con SUS
páginas del libro (según `fuentes[0].pagina_inicio/fin`) y lo publica como recurso
`pdf/paginas_libro` (clave `paginas`). No toca la BD.

Uso::

    python manage.py publish_media --file out/b.json --out out/b_pub.json --storage s3
    python manage.py publish_media --file out/b.json --out out/b_pub.json --paginas-libro --contenido libro.pdf
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_pipeline.media.recursos import upsert_recurso
from content_pipeline.media.storage import StorageError, get_storage

_KIND = {"imagen": "images", "pdf": "docs", "audio": "audio", "video": "docs"}


def _resolve(archivo: str, base: Path) -> Path | None:
    for cand in (Path(archivo), base / archivo):
        if cand.exists():
            return cand
    return None


def book_pages_pdf(src, a: int, b: int) -> bytes:
    """PDF con las páginas ``a..b`` (1-based, inclusivas) del documento ``src`` (fitz)."""
    import fitz

    out = fitz.open()
    out.insert_pdf(src, from_page=a - 1, to_page=b - 1)
    data = out.tobytes(garbage=3, deflate=True)
    out.close()
    return data


class Command(BaseCommand):
    help = "Sube figuras/PDF/audio locales del curso al storage y rellena sus URLs (no toca la BD)."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="JSON del curso.")
        parser.add_argument("--out", required=True, help="JSON de salida con las URLs.")
        parser.add_argument("--storage", default=None, help="local | s3 (def. AUDIO_STORAGE).")
        parser.add_argument("--media-dir", default=None, help="Storage local: carpeta raíz (se agrega el subdirectorio del tipo).")
        parser.add_argument("--base-url", default=None, help="Storage local: URL base (se agrega el subdirectorio del tipo).")
        parser.add_argument("--paginas-libro", action="store_true",
                            help="Publicar por lección el PDF con sus páginas del libro.")
        parser.add_argument("--contenido", default=None, help="Libro PDF (requerido con --paginas-libro).")
        parser.add_argument("--overwrite", action="store_true", help="Re-subir aunque ya exista.")

    def handle(self, *args, **opts):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
        src_path = Path(opts["file"])
        course = json.loads(src_path.read_text(encoding="utf-8"))
        codigo = str(((course.get("manifest") or {}).get("curso") or {}).get("codigo") or "curso").strip().upper()
        base = src_path.parent

        storages: dict[str, object] = {}

        def storage(kind: str):
            if kind not in storages:
                extra = {}
                if opts["media_dir"]:
                    extra["media_dir"] = Path(opts["media_dir"]) / {"images": "course_images", "docs": "course_docs",
                                                                     "audio": "course_audio"}[kind]
                if opts["base_url"]:
                    extra["base_url"] = opts["base_url"].rstrip("/") + "/" + {
                        "images": "course_images", "docs": "course_docs", "audio": "course_audio"}[kind] + "/"
                try:
                    storages[kind] = get_storage(opts["storage"], kind=kind, **extra)
                except StorageError as exc:
                    raise CommandError(str(exc)) from exc
            return storages[kind]

        def publish(kind: str, fname: str, data: bytes | None = None, path: Path | None = None) -> tuple[str, bool]:
            st = storage(kind)
            if not opts["overwrite"] and st.exists(fname):
                return st.url(fname), False
            return st.save(fname, data if data is not None else path.read_bytes()), True

        subidos = reutilizados = faltantes = 0
        try:
            for lesson in course.get("lessons") or []:
                for rec in lesson.get("recursos") or []:
                    local = rec.get("archivo_local")
                    if not local or (rec.get("url") and not opts["overwrite"]):
                        continue
                    path = _resolve(local, base)
                    if path is None:
                        faltantes += 1
                        self.stderr.write(self.style.WARNING(f"  falta el archivo {local} ({lesson.get('nombre')})"))
                        continue
                    h = (rec.get("meta") or {}).get("hash") or hashlib.sha1(path.read_bytes()).hexdigest()
                    fname = f"{codigo}/{h[:16]}{path.suffix.lower()}"
                    rec["url"], nuevo = publish(_KIND.get(rec.get("tipo"), "docs"), fname, path=path)
                    subidos += nuevo
                    reutilizados += not nuevo

            paginas = 0
            if opts["paginas_libro"]:
                if not opts["contenido"] or Path(opts["contenido"]).suffix.lower() != ".pdf":
                    raise CommandError("--paginas-libro requiere --contenido con el libro en PDF.")
                import fitz
                with fitz.open(opts["contenido"]) as doc:
                    for lesson in course.get("lessons") or []:
                        if lesson.get("tipo") == "quiz":
                            continue
                        f0 = (lesson.get("fuentes") or [{}])[0]
                        a, b = int(f0.get("pagina_inicio") or 0), int(f0.get("pagina_fin") or 0)
                        if not (1 <= a <= b <= len(doc)):
                            continue
                        fname = f"{codigo}/paginas_{a:03d}-{b:03d}.pdf"
                        st = storage("docs")
                        if not opts["overwrite"] and st.exists(fname):
                            url, nuevo = st.url(fname), False
                        else:
                            url, nuevo = st.save(fname, book_pages_pdf(doc, a, b)), True
                        subidos += nuevo
                        reutilizados += not nuevo
                        etiqueta = f"{a}" if a == b else f"{a}-{b}"
                        upsert_recurso(lesson, {
                            "tipo": "pdf", "rol": "paginas_libro", "clave": "paginas", "url": url,
                            "orden": 1001, "titulo": f"Páginas del libro ({etiqueta})",
                            "meta": {"paginas": [a, b], "fuente": f0.get("fuente_nombre") or ""},
                        })
                        paginas += 1
                self.stdout.write(f"Páginas del libro: {paginas} lecciones.")
        except StorageError as exc:
            raise CommandError(str(exc)) from exc

        course["medios_meta"] = {"subidos": subidos, "reutilizados": reutilizados, "faltantes": faltantes,
                                 "storage": next(iter(storages.values())).name if storages else None}
        out = Path(opts["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(course, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(
            f"Medios: {subidos} subidos · {reutilizados} reutilizados · {faltantes} faltantes → {out}"))
        if faltantes:
            self.stderr.write(self.style.WARNING("Hay archivos faltantes: esas figuras no se importarán."))
        self.stdout.write("Luego: python manage.py import_course --file " + str(out))
