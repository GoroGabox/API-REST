"""Extrae las figuras del libro (PDF/Word) y las asigna a las lecciones del curso.

Etapa posterior a `generate_course --from-plan` (como `generate_media`): NO toca la BD
ni regenera nada. Ubica cada figura en la lección del PLAN cuyo texto fuente contiene
el texto que la rodea en el libro (respaldo: página en PDF / unidad en carpeta de
Word) y la enlaza a la lección del curso por `plan_id`. Las sube al storage
(`--storage local|s3`, carpeta `course_images/`) y escribe `imagenes` por lección +
`imagenes_meta` en el JSON. Luego `import_course` crea las `LeccionImagen`.

Uso::

    # Revisar primero sin subir (archivos en out/img_<codigo>/)
    python manage.py extract_images --plan out/plan_b.json --course out/b.json --contenido libro.pdf --out out/b_img.json --sin-subir
    # Con pie/alt por IA (visión, Haiku) y subida a R2
    python manage.py extract_images --plan out/plan_b.json --course out/b.json --contenido libro.pdf --out out/b_img.json --describir --storage s3
    # Carpeta de capítulos en Word
    python manage.py extract_images --plan out/plan_b.json --course out/b.json --dir "../libro B" --out out/b_img.json
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_pipeline.media.images import describe_figures_llm, extract_figures, map_figures_to_plan
from content_pipeline.media.storage import StorageError, get_storage
from content_pipeline.processors.outline import chapter_files


def _course_lessons_by_plan(course: dict) -> tuple[dict[str, dict], dict[tuple[int, str], dict]]:
    by_id, by_name = {}, {}
    for lec in course.get("lessons") or []:
        if lec.get("plan_id"):
            by_id[lec["plan_id"]] = lec
        by_name[(int(lec.get("unidad_orden") or 0), str(lec.get("nombre") or "").strip().lower())] = lec
    return by_id, by_name


class Command(BaseCommand):
    help = "Extrae figuras del libro, las mapea a las lecciones y las sube al storage (no toca la BD)."

    def add_arguments(self, parser):
        parser.add_argument("--plan", required=True, help="plan.json usado para generar el curso.")
        parser.add_argument("--course", required=True, help="JSON del curso (salida de generate_course).")
        parser.add_argument("--contenido", default=None, help="Libro único (PDF/DOCX).")
        parser.add_argument("--dir", default=None, help="Carpeta con un archivo por capítulo (docx/pdf).")
        parser.add_argument("--out", required=True, help="JSON de salida (curso + imagenes).")
        parser.add_argument("--storage", default=None, help="local | s3 (def. AUDIO_STORAGE).")
        parser.add_argument("--media-dir", default=None, help="Storage local: carpeta destino.")
        parser.add_argument("--base-url", default=None, help="Storage local: URL base pública.")
        parser.add_argument("--sin-subir", action="store_true",
                            help="Solo extraer y mapear: archivos en out/img_<codigo>/, sin URL.")
        parser.add_argument("--describir", action="store_true",
                            help="IA con visión: pie + alt y descarte de figuras decorativas.")
        parser.add_argument("--describe-model", default=None, help="Modelo de visión (def. Haiku).")
        parser.add_argument("--limit", type=int, default=0, help="Procesar solo las primeras N figuras.")

    def handle(self, *args, **opts):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
        if bool(opts["contenido"]) == bool(opts["dir"]):
            raise CommandError("Pasá exactamente uno: --contenido (libro único) o --dir (carpeta de capítulos).")
        plan = json.loads(Path(opts["plan"]).read_text(encoding="utf-8"))
        course = json.loads(Path(opts["course"]).read_text(encoding="utf-8"))
        codigo = str(((course.get("manifest") or {}).get("curso") or {}).get("codigo")
                     or (plan.get("curso") or {}).get("codigo") or "curso").strip().upper()

        if opts["dir"]:
            source = chapter_files(opts["dir"])
            if not source:
                raise CommandError(f"Sin archivos de capítulo en {opts['dir']}")
        else:
            source = Path(opts["contenido"])
            if not source.exists():
                raise CommandError(f"No existe: {source}")

        self.stdout.write(f"Extrayendo figuras de: {opts['dir'] or opts['contenido']}")
        figs, descartes = extract_figures(source)
        if opts["limit"]:
            figs = figs[: opts["limit"]]
        self.stdout.write(f"  {len(figs)} figuras · descartadas: {dict(descartes) or 0}")

        asignadas, sin_leccion = map_figures_to_plan(figs, plan, por_archivo=bool(opts["dir"]))
        plan_lessons = {lec.get("id"): lec for u in plan.get("unidades") or [] for lec in u.get("lecciones") or []}

        # IA de visión (opcional): pie/alt + descarte de decorativas.
        descripciones, ia = {}, None
        if opts["describir"]:
            from content_pipeline.llm.client import LLMClient, draft_model
            if not LLMClient.is_available():
                self.stderr.write(self.style.WARNING("--describir requiere ANTHROPIC_API_KEY; se omite."))
            else:
                model = opts["describe_model"] or draft_model()
                client = LLMClient(model=model)
                items = [(fig, plan_lessons.get(lid, {}).get("nombre", ""))
                         for lid, lst in asignadas.items() for fig, _m in lst]
                self.stdout.write(f"Describiendo {len(items)} figuras con IA ({model})…")
                descripciones = describe_figures_llm(items, client=client, model=model)
                ia = client.meter.as_dict()
                self.stdout.write(f"  ~US${round(client.meter.cost_usd, 4)} · {client.meter.calls} llamadas")

        storage, local_dir = None, None
        if opts["sin_subir"]:
            local_dir = Path(opts["out"]).parent / f"img_{codigo}"
            local_dir.mkdir(parents=True, exist_ok=True)
        else:
            storage_opts = {k: opts[k] for k in ("media_dir", "base_url") if opts[k]}
            try:
                storage = get_storage(opts["storage"], kind="images", **storage_opts)
            except StorageError as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(f"Storage: {storage.name} ({storage.describe()})")

        by_id, by_name = _course_lessons_by_plan(course)
        motivos: Counter = Counter(descartes)
        por_leccion: dict[str, int] = {}
        sin_curso = 0
        subidas = reutilizadas = 0
        for lid, lst in asignadas.items():
            plan_lec = plan_lessons.get(lid, {})
            unidad_orden = next((int(u.get("orden") or 0) for u in plan.get("unidades") or []
                                 if plan_lec in (u.get("lecciones") or [])), 0)
            lesson = by_id.get(lid) or by_name.get((unidad_orden, str(plan_lec.get("nombre", "")).strip().lower()))
            if lesson is None:
                sin_curso += len(lst)
                continue
            imagenes = []
            for fig, mapeo in lst:
                desc = descripciones.get(fig.hash) or {}
                if desc and not desc.get("relevante", True):
                    motivos["decorativa_ia"] += 1
                    continue
                fname = f"{codigo}/{fig.hash[:16]}.{fig.ext}"
                url, archivo_local = "", ""
                if local_dir is not None:
                    target = local_dir / Path(fname).name
                    target.write_bytes(fig.data)
                    archivo_local = str(target)
                else:
                    try:
                        if storage.exists(fname):
                            url, reutilizadas = storage.url(fname), reutilizadas + 1
                        else:
                            url, subidas = storage.save(fname, fig.data), subidas + 1
                    except StorageError as exc:
                        raise CommandError(str(exc)) from exc
                img = {
                    "url": url,
                    "orden": len(imagenes) + 1,
                    "pagina": fig.pagina,
                    "pie": desc.get("pie") or fig.pie_libro,
                    "alt": desc.get("alt") or desc.get("pie") or fig.pie_libro,
                    "ancho": fig.ancho,
                    "alto": fig.alto,
                    "origen": fig.origen,
                    "mapeo": mapeo,
                    "hash": fig.hash,
                }
                if archivo_local:
                    img["archivo_local"] = archivo_local
                imagenes.append(img)
            lesson["imagenes"] = imagenes
            por_leccion[lesson.get("nombre", lid)] = len(imagenes)

        # Lecciones gráficas (revisión visual alta) que quedaron sin figuras.
        visuales_sin = [lec.get("nombre", "") for lec in plan_lessons.values()
                        if (lec.get("visual") or {}).get("nivel") == "alta" and lec.get("id") not in asignadas]
        course["imagenes_meta"] = {
            "fuente": str(opts["dir"] or opts["contenido"]),
            "extraidas": len(figs),
            "asignadas": sum(por_leccion.values()),
            "descartadas": dict(motivos),
            "sin_leccion": [{"pagina": f.pagina, "ancla": (f.ancla_antes or f.ancla_despues)[:120]}
                            for f in sin_leccion],
            "sin_leccion_en_curso": sin_curso,
            "por_leccion": por_leccion,
            "visuales_sin_figuras": visuales_sin,
            "mapeo": dict(Counter(m for lst in asignadas.values() for _f, m in lst)),
            "subidas": subidas, "reutilizadas": reutilizadas,
            "ia": ia,
        }

        out = Path(opts["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(course, ensure_ascii=False, indent=2), encoding="utf-8")

        meta = course["imagenes_meta"]
        self.stdout.write(self.style.SUCCESS(
            f"Figuras: {meta['asignadas']} asignadas en {len(por_leccion)} lecciones · "
            f"{len(sin_leccion)} sin lección · mapeo {meta['mapeo']}"))
        for nombre, n in por_leccion.items():
            self.stdout.write(f"  {n:>3} · {nombre[:70]}")
        if sin_curso:
            self.stderr.write(self.style.WARNING(
                f"{sin_curso} figura(s) de lecciones del plan que no están en el curso (¿plan distinto?)."))
        if visuales_sin:
            self.stderr.write(self.style.WARNING(
                "Lecciones gráficas sin figuras (revisar a mano): " + "; ".join(visuales_sin[:10])))
        if local_dir is not None:
            self.stdout.write(f"Archivos en {local_dir} (sin subir; volvé a correr sin --sin-subir para publicar).")
        self.stdout.write(self.style.SUCCESS(f"Escrito: {out}"))
        self.stdout.write("Luego: python manage.py import_course --file " + str(out))
