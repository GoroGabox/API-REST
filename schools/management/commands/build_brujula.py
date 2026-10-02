"""Empaqueta la Brújula del Libro como ZIP para auditores humanos (uso offline).

El ZIP trae ``brujula.html`` autocontenido (plan y/o curso embebidos, abre con
doble clic sin servidor) y ``LEEME.txt``. El auditor revisa, deja observaciones y
exporta ``review_<codigo>_<autor>.json``, que devuelve al dueño del curso.

    python manage.py build_brujula --plan out/plan_b.json --out out/brujula_b.zip
    python manage.py build_brujula --plan out/plan_b.json --course out/b.json --out out/brujula_b.zip
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_pipeline.exporters.json_exporter import read_json
from content_pipeline.review.ids import ensure_plan_ids

TEMPLATE = Path(__file__).resolve().parents[3] / "content_pipeline" / "review" / "brujula.html"

SKELETON_HEAD = (
    '<!doctype html><html lang="es"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
    "<style>body{margin:0}[hidden]{display:none!important}img{max-width:100%}</style></head><body>\n"
)

LEEME = """BRÚJULA DEL LIBRO — revisión del curso {codigo}
=============================================

1. Descomprimí esta carpeta y abrí «brujula.html» con doble clic (Chrome, Edge o Firefox).
   No necesita internet salvo para las tipografías.
2. Arriba a la derecha escribí tu nombre (queda como autor de tus observaciones).
3. Fase «Estructura»: revisá unidades y lecciones. Las herramientas (subir, dividir, unir,
   mover, quitar, renombrar, categoría, tope) NO cambian el plan: registran una propuesta.
4. Fase «Contenido»: abrí cada lección; seleccioná un texto y usá «Corregir selección»
   para proponer una corrección, o «Observar» para dejar un comentario con cita del libro.
5. Pestaña «Revisión»: elegí tu veredicto por fase y pulsá «Exportar mi revisión».
   Se descarga review_{codigo_l}_<tu-nombre>.json: envíaselo al responsable del curso.

Tu trabajo se guarda en este navegador mientras no borres los datos del sitio, pero
solo llega al responsable cuando exportás y enviás el archivo.
"""


def _collect_figures(bundle: dict, opts: dict) -> dict[str, Path]:
    """Archivos de figuras a incluir en el ZIP ({ruta en el zip: archivo local}).

    Las rutas del plan (``figuras[].archivo``) y del curso (``recursos[].archivo_local``)
    son relativas a su JSON; en el ZIP quedan en ``figuras/<nombre>`` y la ruta del
    bundle se reescribe a esa ubicación (la Brújula las abre junto a ``brujula.html``).
    """
    files: dict[str, Path] = {}

    def add(base: Path, ref: str) -> str:
        path = Path(ref) if Path(ref).is_absolute() else base / ref
        if not path.exists():
            return ref
        arc = f"figuras/{path.name}"
        files[arc] = path
        return arc

    if bundle.get("plan") and opts.get("plan"):
        base = Path(opts["plan"]).parent
        for u in bundle["plan"].get("unidades") or []:
            for lec in u.get("lecciones") or []:
                for f in lec.get("figuras") or []:
                    if f.get("archivo"):
                        f["archivo"] = add(base, f["archivo"])
    if bundle.get("course") and opts.get("course"):
        base = Path(opts["course"]).parent
        for lec in bundle["course"].get("lessons") or []:
            for r in lec.get("recursos") or []:
                if r.get("archivo_local") and not r.get("url"):
                    r["archivo_local"] = add(base, r["archivo_local"])
    return files


class Command(BaseCommand):
    help = "Genera un ZIP offline de la Brújula del Libro con el plan y/o curso embebidos, para auditores."

    def add_arguments(self, parser):
        parser.add_argument("--plan", help="plan.json (salida de plan_course).")
        parser.add_argument("--course", help="JSON de generate_course (opcional).")
        parser.add_argument("--out", required=True, help="Ruta del .zip a generar.")
        parser.add_argument("--modo", choices=["auditor", "editor"], default="auditor",
                            help="Rol con el que abre la Brújula (def. auditor).")

    def handle(self, *args, **opts):
        if not opts.get("plan") and not opts.get("course"):
            raise CommandError("Indicá --plan y/o --course.")
        if not TEMPLATE.exists():
            raise CommandError(f"No encuentro la plantilla: {TEMPLATE}")

        bundle: dict = {"modo": opts["modo"]}
        codigo = ""
        if opts.get("plan"):
            plan = read_json(Path(opts["plan"]))
            if not isinstance(plan.get("unidades"), list):
                raise CommandError("--plan no parece un plan.json.")
            bundle["plan"] = ensure_plan_ids(plan)
            codigo = (plan.get("curso") or {}).get("codigo", "")
        if opts.get("course"):
            course = read_json(Path(opts["course"]))
            if not isinstance(course.get("lessons"), list):
                raise CommandError("--course no parece un JSON de generate_course.")
            bundle["course"] = course
            codigo = codigo or ((course.get("manifest") or {}).get("curso") or {}).get("codigo", "")
        codigo = (codigo or "curso").upper()
        figuras = _collect_figures(bundle, opts)

        payload = json.dumps(bundle, ensure_ascii=False).replace("</", "<\\/")
        page = TEMPLATE.read_text(encoding="utf-8")
        tag = f'<script type="application/json" id="bundle">{payload}</script>\n'
        idx = page.find("<script>")
        page = page[:idx] + tag + page[idx:] if idx >= 0 else page + tag
        html = SKELETON_HEAD + page + "\n</body></html>\n"

        out = Path(opts["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("brujula.html", html)
            zf.writestr("LEEME.txt", LEEME.format(codigo=codigo, codigo_l=codigo.lower()))
            for arcname, path in figuras.items():
                zf.write(path, arcname)
        n_l = sum(len(u.get("lecciones", [])) for u in (bundle.get("plan") or {}).get("unidades", []))
        n_c = len((bundle.get("course") or {}).get("lessons", []))
        self.stdout.write(self.style.SUCCESS(
            f"Brujula {codigo} ({opts['modo']}) -> {out} · plan: {n_l} lecciones · curso: {n_c} lecciones"
            f" · {len(figuras)} figuras"))
