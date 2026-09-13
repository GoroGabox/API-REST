"""Genera el AUDIO de un curso ya generado (`generate_course`) y lo enlaza.

Toma el JSON `{manifest, lessons}`, redacta un guion locutado por lección con
Claude, lo sintetiza con el proveedor TTS configurado (ElevenLabs por defecto),
guarda los .mp3 y escribe `transcripcion` + `url_audio` en cada lección. No toca
la base de datos: el JSON resultante se sube luego con `import_course`.

Fase 1 (local): los archivos se guardan bajo MEDIA_ROOT y las URLs apuntan al
server local (`--base-url`). Para producción, apunta `--media-dir`/`--base-url`
a tu bucket/CDN (o sube los archivos aparte y usa la URL pública).
"""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils.text import slugify

from content_pipeline.exporters.json_exporter import read_json, write_json
from content_pipeline.llm.client import LLMClient, draft_model
from content_pipeline.media.narration import build_narration_script
from content_pipeline.media.tts import TTSError, get_tts_provider


class Command(BaseCommand):
    help = (
        "Genera el audio (TTS) de un curso ya generado y escribe url_audio + "
        "transcripcion en el JSON. No toca la BD; luego se sube con import_course."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="JSON del curso ({manifest, lessons}).")
        parser.add_argument("--out", default=None, help="JSON de salida (por defecto, sobrescribe --file).")
        parser.add_argument("--provider", default=None, help="Proveedor TTS (default: TTS_PROVIDER).")
        parser.add_argument("--voice", default=None, help="Override del id de voz del proveedor.")
        parser.add_argument("--tipos", default="texto",
                            help="Tipos de lección a locutar, separados por coma (default: texto).")
        parser.add_argument("--media-dir", default=None,
                            help="Carpeta destino de los audios (default: MEDIA_ROOT/course_audio).")
        parser.add_argument("--base-url", default=None,
                            help="Prefijo URL para url_audio (default: http://127.0.0.1:8000/media/course_audio/).")
        parser.add_argument("--limit", type=int, default=None, help="Solo las primeras N lecciones (para probar).")
        parser.add_argument("--overwrite", action="store_true", help="Regenera aunque el .mp3 ya exista.")

    def handle(self, *args, **opts):
        file_path = Path(opts["file"])
        if not file_path.exists():
            raise CommandError(f"No existe el archivo: {file_path}")
        data = read_json(file_path)
        if not isinstance(data, dict) or "lessons" not in data:
            raise CommandError("El JSON debe tener las claves 'manifest' y 'lessons'.")

        try:
            provider = get_tts_provider(opts["provider"])
            if opts["voice"]:
                provider.voice_id = opts["voice"]  # type: ignore[attr-defined]
        except TTSError as exc:
            raise CommandError(str(exc)) from exc

        media_dir = Path(opts["media_dir"]) if opts["media_dir"] else Path(settings.MEDIA_ROOT) / "course_audio"
        media_dir.mkdir(parents=True, exist_ok=True)
        base_url = opts["base_url"] or f"http://127.0.0.1:8000{settings.MEDIA_URL}course_audio/"
        if not base_url.endswith("/"):
            base_url += "/"
        ext = provider.extension()

        tipos = {t.strip() for t in opts["tipos"].split(",") if t.strip()}
        codigo = ((data.get("manifest") or {}).get("curso") or {}).get("codigo", "curso")

        use_llm = LLMClient.is_available()
        narration_client = LLMClient() if use_llm else None
        if use_llm:
            self.stdout.write(f"Guion locutado con IA ({draft_model()}); voz vía TTS.")
        else:
            self.stdout.write("Sin IA: guion = Markdown limpiado (calidad menor).")

        lessons = data["lessons"]
        objetivo = [l for l in lessons if l.get("tipo") in tipos]
        if opts["limit"]:
            objetivo = objetivo[: opts["limit"]]
        self.stdout.write(f"Lecciones a locutar: {len(objetivo)} (tipos: {sorted(tipos)}).")

        hechas = 0
        for index, lesson in enumerate(lessons, start=1):
            if lesson not in objetivo:
                continue
            nombre = lesson.get("nombre", f"leccion_{index}")
            slug = slugify(nombre)[:60] or f"leccion_{index}"
            fname = f"{slugify(codigo)}_{index:02d}_{slug}.{ext}"
            out_audio = media_dir / fname

            if out_audio.exists() and not opts["overwrite"]:
                self.stdout.write(f"  [{index}] ya existe, omito: {fname}")
                lesson["url_audio"] = base_url + fname
                continue

            self.stdout.write(f"  [{index}] {nombre} — redactando guion…")
            script = (lesson.get("transcripcion") or "").strip() or build_narration_script(
                lesson.get("contenido", ""), nombre=nombre, client=narration_client,
            )
            if not script:
                self.stderr.write(f"      sin contenido locutable, omito.")
                continue

            self.stdout.write(f"      sintetizando audio ({len(script)} chars)…")
            try:
                audio = provider.synthesize(script)
            except TTSError as exc:
                raise CommandError(f"Fallo TTS en lección {index}: {exc}") from exc
            out_audio.write_bytes(audio)

            lesson["transcripcion"] = script
            lesson["url_audio"] = base_url + fname
            hechas += 1
            self.stdout.write(self.style.SUCCESS(f"      → {fname} ({len(audio)//1024} KB)"))

        out_path = Path(opts["out"]) if opts["out"] else file_path
        write_json(out_path, data)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Audio generado para {hechas} lecciones → {out_path}"))
        self.stdout.write(f"Archivos en: {media_dir}")
        self.stdout.write("")
        self.stdout.write("Prueba local: corre el server y abre la lección en el dashboard del estudiante.")
        self.stdout.write("  python manage.py runserver")
