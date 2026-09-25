"""Genera el AUDIO de un curso ya generado (`generate_course`) y lo enlaza.

Toma el JSON `{manifest, lessons}`, redacta un guion locutado por lección con
Claude, lo sintetiza con el proveedor TTS configurado y escribe `transcripcion` +
`url_audio` en cada lección. No toca la base de datos: el JSON resultante se sube
luego con `import_course`.

Flujo en dos fases (recomendado, el TTS es lo caro):
  1. `--solo-guion`: redacta y audita los guiones (cifras nuevas, truncado, largo)
     → `transcripcion` + `audio_meta`. Revisar/editar antes de seguir.
  2. `--solo-audio`: sintetiza SOLO los guiones existentes y los guarda en el
     storage (`--storage local|s3`).
Sin esos flags hace ambas fases en una pasada. El JSON se guarda tras cada
lección: si algo falla a mitad, re-correr retoma donde quedó.
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils.text import slugify

from content_pipeline.exporters.json_exporter import read_json, write_json
from content_pipeline.llm.client import LLMClient, draft_model
from content_pipeline.media.narration import (
    audit_script,
    build_narration_script_meta,
    has_findings,
    strip_markdown,
)
from content_pipeline.media.storage import StorageError, get_audio_storage
from content_pipeline.media.tts import TTSError, get_tts_provider


def _resumen_auditoria(lessons: list[dict]) -> dict:
    metas = [l["audio_meta"] for l in lessons if isinstance(l.get("audio_meta"), dict)]
    return {
        "guiones": len(metas),
        "con_cifras_nuevas": [
            {"leccion": l.get("nombre"), "cifras": l["audio_meta"].get("cifras_nuevas")}
            for l in lessons
            if isinstance(l.get("audio_meta"), dict) and l["audio_meta"].get("cifras_nuevas")
        ],
        "truncados": sum(1 for m in metas if m.get("truncado")),
        "cortos": sum(1 for m in metas if m.get("corto")),
        "con_audio": sum(1 for l in lessons if (l.get("url_audio") or "").startswith("http")
                         and "placeholder" not in l.get("url_audio", "")),
    }


class Command(BaseCommand):
    help = (
        "Genera el guion y el audio (TTS) de un curso ya generado y escribe "
        "transcripcion + url_audio en el JSON. No toca la BD; luego import_course."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="JSON del curso ({manifest, lessons}).")
        parser.add_argument("--out", default=None, help="JSON de salida (por defecto, sobrescribe --file).")
        fase = parser.add_mutually_exclusive_group()
        fase.add_argument("--solo-guion", action="store_true",
                          help="Solo redacta y audita guiones (sin TTS). Fase 1.")
        fase.add_argument("--solo-audio", action="store_true",
                          help="Solo sintetiza los guiones ya existentes (sin LLM). Fase 2.")
        parser.add_argument("--rehacer-guion", action="store_true",
                            help="Redacta el guion aunque la lección ya tenga transcripcion.")
        parser.add_argument("--estricto", action="store_true",
                            help="No sintetiza lecciones cuyo guion tiene hallazgos (cifras nuevas/truncado/corto).")
        parser.add_argument("--provider", default=None,
                            help="Proveedor TTS: elevenlabs | openai (default: TTS_PROVIDER).")
        parser.add_argument("--voice", default=None, help="Override del id de voz del proveedor.")
        parser.add_argument("--storage", default=None,
                            help="Destino de los audios: local | s3 (default: AUDIO_STORAGE o local).")
        parser.add_argument("--tipos", default="texto",
                            help="Tipos de lección a locutar, separados por coma (default: texto).")
        parser.add_argument("--media-dir", default=None,
                            help="[local] Carpeta destino (default: MEDIA_ROOT/course_audio).")
        parser.add_argument("--base-url", default=None,
                            help="[local] Prefijo URL de url_audio (default: http://127.0.0.1:8000/media/course_audio/).")
        parser.add_argument("--limit", type=int, default=None, help="Solo las primeras N lecciones (para probar).")
        parser.add_argument("--overwrite", action="store_true", help="Regenera el audio aunque ya exista.")

    def handle(self, *args, **opts):
        file_path = Path(opts["file"])
        if not file_path.exists():
            raise CommandError(f"No existe el archivo: {file_path}")
        data = read_json(file_path)
        if not isinstance(data, dict) or "lessons" not in data:
            raise CommandError("El JSON debe tener las claves 'manifest' y 'lessons'.")
        out_path = Path(opts["out"]) if opts["out"] else file_path

        hacer_guion = not opts["solo_audio"]
        hacer_audio = not opts["solo_guion"]

        provider = storage = None
        if hacer_audio:
            try:
                provider = get_tts_provider(opts["provider"])
                if opts["voice"]:
                    provider.voice_id = opts["voice"]  # type: ignore[attr-defined]
                storage_opts = {}
                if opts["media_dir"]:
                    storage_opts["media_dir"] = opts["media_dir"]
                if opts["base_url"]:
                    storage_opts["base_url"] = opts["base_url"]
                storage = get_audio_storage(opts["storage"], **storage_opts)
            except (TTSError, StorageError) as exc:
                raise CommandError(str(exc)) from exc

        narration_client = None
        if hacer_guion:
            if LLMClient.is_available():
                narration_client = LLMClient()
                self.stdout.write(f"Guion locutado con IA ({draft_model()}).")
            else:
                self.stdout.write("Sin IA: guion = Markdown limpiado (calidad menor).")

        tipos = {t.strip() for t in opts["tipos"].split(",") if t.strip()}
        codigo = ((data.get("manifest") or {}).get("curso") or {}).get("codigo", "curso")
        lessons = data["lessons"]
        objetivo = [(i, l) for i, l in enumerate(lessons, start=1) if l.get("tipo") in tipos]
        if opts["limit"]:
            objetivo = objetivo[: opts["limit"]]
        self.stdout.write(f"Lecciones objetivo: {len(objetivo)} (tipos: {sorted(tipos)}).")

        if hacer_audio:
            # Estimación de caracteres a sintetizar (lo que factura el TTS).
            est = sum(
                len((l.get("transcripcion") or "").strip()
                    or ("" if opts["solo_audio"] else strip_markdown(l.get("contenido") or "")))
                for _, l in objetivo
            )
            self.stdout.write(
                f"TTS: {provider.name} (voz {provider.voice_id}) -> {storage.name} ({storage.describe()}). "
                f"~{est:,} caracteres a sintetizar (tope, sin descontar audios ya existentes)."
            )

        def _guardar():
            data["auditoria_audio"] = _resumen_auditoria(lessons)
            write_json(out_path, data)

        guiones = audios = omitidas = 0
        for index, lesson in objetivo:
            nombre = lesson.get("nombre", f"leccion_{index}")
            meta = lesson.get("audio_meta") if isinstance(lesson.get("audio_meta"), dict) else {}

            # ---- Fase 1: guion ------------------------------------------------
            script = (lesson.get("transcripcion") or "").strip()
            if hacer_guion and (not script or opts["rehacer_guion"]):
                self.stdout.write(f"  [{index}] {nombre} — redactando guion…")
                res = build_narration_script_meta(
                    lesson.get("contenido", ""), nombre=nombre, client=narration_client,
                )
                script = res.script
                if not script:
                    self.stderr.write("      sin contenido locutable, omito.")
                    omitidas += 1
                    continue
                meta = {**meta, **audit_script(script, lesson.get("contenido", ""), truncado=res.truncado)}
                lesson["transcripcion"] = script
                lesson["audio_meta"] = meta
                guiones += 1
                if has_findings(meta):
                    self.stdout.write(self.style.WARNING(
                        f"      [!] revisar guion: cifras nuevas {meta['cifras_nuevas'] or '—'}, "
                        f"truncado={meta['truncado']}, ratio={meta['ratio_largo']}"
                    ))
                _guardar()

            if not hacer_audio:
                continue

            # ---- Fase 2: audio ------------------------------------------------
            if not script:
                self.stderr.write(f"  [{index}] {nombre} — sin guion (corre antes --solo-guion), omito.")
                omitidas += 1
                continue
            if opts["estricto"] and has_findings(meta):
                self.stderr.write(f"  [{index}] {nombre} — guion con hallazgos (--estricto), omito.")
                omitidas += 1
                continue

            slug = slugify(nombre)[:60] or f"leccion_{index}"
            fname = f"{slugify(codigo)}_{index:02d}_{slug}.{provider.extension()}"
            try:
                if not opts["overwrite"] and storage.exists(fname):
                    self.stdout.write(f"  [{index}] audio ya existe, omito: {fname}")
                    lesson["url_audio"] = storage.url(fname)
                    _guardar()
                    continue
                self.stdout.write(f"  [{index}] {nombre} — sintetizando ({len(script):,} chars)…")
                audio = provider.synthesize(script)
                url = storage.save(fname, audio)
            except (TTSError, StorageError) as exc:
                _guardar()
                raise CommandError(
                    f"Fallo en lección {index} ({nombre}): {exc}. "
                    f"Progreso guardado en {out_path}; re-corre para retomar."
                ) from exc

            lesson["url_audio"] = url
            lesson["audio_meta"] = {
                **meta, "provider": provider.name, "voz": provider.voice_id,
                "chars": len(script), "archivo": fname,
            }
            audios += 1
            self.stdout.write(self.style.SUCCESS(f"      -> {fname} ({len(audio)//1024} KB)"))
            _guardar()

        _guardar()
        resumen = data["auditoria_audio"]
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Guiones redactados: {guiones} · audios generados: {audios} · omitidas: {omitidas} -> {out_path}"
        ))
        self.stdout.write(
            f"Auditoría de guiones: {len(resumen['con_cifras_nuevas'])} con cifras nuevas · "
            f"{resumen['truncados']} truncados · {resumen['cortos']} cortos · "
            f"{resumen['con_audio']} lecciones con audio."
        )
        if opts["solo_guion"]:
            self.stdout.write("Siguiente: revisa los guiones marcados y corre con --solo-audio.")
