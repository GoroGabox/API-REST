"""Agrega (o actualiza) la presentación inicial «Requisitos y exámenes» de un curso.

Crea la unidad 0 «Antes de empezar» con una lección de lectura armada desde
``content_pipeline/presentaciones/`` (parte de la licencia + declaración jurada +
salud y preguntas frecuentes). Sin IA e idempotente; no toca las demás lecciones.
A los alumnos que ya habían completado el resto del curso les marca también esta
lección, para que no pierdan la elegibilidad del examen final.

    # Una vez, desde local (sube los 2 PDF y guarda sus URL en presentaciones/medios.json):
    python manage.py presentacion_curso --subir-medios --storage s3 --pdf-dir out/cd/zip
    # Por curso (local o prod), primero con --dry-run:
    python manage.py presentacion_curso --curso B --dry-run
    python manage.py presentacion_curso --curso B
    # Audio (desde local, con claves de TTS): guion + TTS por licencia; guarda la URL en
    # presentaciones/audio.json (commitear). Sin --audio se enlaza el de audio.json si el
    # texto no cambió (así en prod no hacen falta claves de TTS).
    python manage.py presentacion_curso --curso B --audio --provider elevenlabs --storage s3
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_pipeline.processors import presentacion as P


class Command(BaseCommand):
    help = "Agrega la unidad 0 «Antes de empezar» (requisitos y exámenes) a un curso."

    def add_arguments(self, parser):
        parser.add_argument("--curso", action="append", default=[], help="Código del curso (repetible).")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--subir-medios", action="store_true",
                            help="Sube los PDF de las declaraciones y escribe presentaciones/medios.json.")
        parser.add_argument("--storage", default="", help="local|s3 (con --subir-medios).")
        parser.add_argument("--pdf-dir", default="out/cd/zip", help="Carpeta con los PDF (con --subir-medios).")
        parser.add_argument("--mostrar", action="store_true", help="Imprime el Markdown armado.")
        parser.add_argument("--audio", action="store_true",
                            help="Genera (si falta o cambió el texto) guion + audio y lo guarda en presentaciones/audio.json.")
        parser.add_argument("--provider", default=None, help="Proveedor TTS con --audio: elevenlabs | openai | azure.")
        parser.add_argument("--voice", default=None, help="Override del id de voz (con --audio).")
        parser.add_argument("--rehacer-audio", action="store_true",
                            help="Con --audio, rehace guion y audio aunque estén vigentes.")

    def handle(self, *args, **o):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
        if o["subir_medios"]:
            self._subir(o)
        if not o["curso"]:
            if not o["subir_medios"]:
                raise CommandError("Indica --curso (o --subir-medios).")
            return

        from content_pipeline.exporters.django_importer import importar_presentacion
        from schools.models import Curso

        try:
            urls = P.medios()
        except P.PresentacionError as exc:
            raise CommandError(str(exc)) from exc
        for codigo in o["curso"]:
            curso = Curso.objects.filter(codigo=codigo).first()
            if curso is None:
                raise CommandError(f"No existe el curso con código «{codigo}».")
            try:
                spec = P.componer(codigo, urls)
            except P.PresentacionError as exc:
                raise CommandError(f"{codigo}: {exc}") from exc
            if o["mostrar"]:
                self.stdout.write(spec["contenido"])
            spec = self._audio(spec, o)
            summary = importar_presentacion(curso, spec, dry_run=o["dry_run"])
            self.stdout.write(f"{codigo} · {curso.nombre} (licencia {spec['licencia']}, "
                              f"{len(spec['contenido'])} caracteres):")
            for linea in summary.as_lines():
                self.stdout.write(f"  {linea}")

    def _subir(self, o):
        from content_pipeline.media.storage import get_storage

        st = get_storage(o["storage"] or None, kind="docs")
        datos = {}
        for clave, (archivo, _) in P.PDFS.items():
            path = Path(o["pdf_dir"]) / archivo
            if not path.exists():
                raise CommandError(f"No existe {path}.")
            nombre = f"presentacion/{archivo}"
            datos[clave] = st.save(nombre, path.read_bytes())
            self.stdout.write(f"  {archivo} → {datos[clave]}")
        (P.CARPETA / "medios.json").write_text(json.dumps(datos, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Escrito {P.CARPETA / 'medios.json'}."))

    # ------------------------------------------------------------------
    def _audio(self, spec, o):
        """Enlaza (y con ``--audio`` genera) la narración de la licencia de ``spec``."""
        entrada, vigente = P.audio_vigente(spec)
        licencia = spec["licencia"]
        if o["audio"] and (not vigente or o["rehacer_audio"] or self._otra_voz(entrada, o)):
            if o["dry_run"]:
                self.stdout.write(f"  [dry-run] generaría guion + audio de la licencia {licencia}.")
                return P.aplicar_audio(spec, entrada) if vigente else spec
            entrada = self._generar_audio(spec, entrada if vigente and not o["rehacer_audio"] else None, o)
            vigente = True
        if entrada and vigente:
            return P.aplicar_audio(spec, entrada)
        if entrada:
            self.stderr.write(self.style.WARNING(
                f"  Audio de {licencia} desactualizado (cambió el texto): no se enlaza el nuevo; "
                f"el publicado se conserva. Corre --audio para regenerarlo."))
        return spec

    def _proveedor(self, o):
        from content_pipeline.media.tts import TTSError, get_tts_provider

        try:
            provider = get_tts_provider(o["provider"])
        except TTSError as exc:
            raise CommandError(str(exc)) from exc
        if o["voice"]:
            provider.voice_id = o["voice"]
        return provider

    def _otra_voz(self, entrada, o):
        if not entrada or not (o["provider"] or o["voice"]):
            return False
        provider = self._proveedor(o)
        return (entrada.get("provider"), entrada.get("voz")) != (provider.name, provider.voice_id)

    def _generar_audio(self, spec, previa, o):
        from content_pipeline.llm.client import LLMClient
        from content_pipeline.media.narration import audit_script, build_narration_script_meta, has_findings
        from content_pipeline.media.storage import StorageError, get_audio_storage
        from content_pipeline.media.tts import TTSError, audio_filename, mp3_duracion_seg

        licencia = str(spec["licencia"])
        provider = self._proveedor(o)
        try:
            storage = get_audio_storage(o["storage"] or None)
        except StorageError as exc:
            raise CommandError(str(exc)) from exc

        if previa and previa.get("transcripcion"):
            script, auditoria = previa["transcripcion"], previa.get("auditoria") or {}
        else:
            if not LLMClient.is_available():
                raise CommandError("--audio necesita ANTHROPIC_API_KEY para redactar el guion.")
            self.stdout.write(f"  Redactando guion de la presentación ({licencia})…")
            res = build_narration_script_meta(spec["contenido"], nombre=P.LECCION_NOMBRE)
            script = res.script
            auditoria = audit_script(script, spec["contenido"], truncado=res.truncado)
            if has_findings(auditoria):
                self.stdout.write(self.style.WARNING(
                    f"  [!] revisar guion: cifras nuevas {auditoria['cifras_nuevas'] or '—'}, "
                    f"truncado={auditoria['truncado']}, ratio={auditoria['ratio_largo']}"))

        fname = audio_filename("presentacion", script, provider)
        duracion = previa.get("duracion_seg") if previa and previa.get("archivo") == fname else None
        try:
            if storage.exists(fname):
                url = storage.url(fname)
            else:
                self.stdout.write(f"  Sintetizando ({len(script):,} caracteres, {provider.name})…")
                audio = provider.synthesize(script)
                url = storage.save(fname, audio)
                duracion = mp3_duracion_seg(audio)
        except (TTSError, StorageError) as exc:
            raise CommandError(f"Audio de {licencia}: {exc}") from exc

        entrada = {
            "hash_texto": P.hash_texto(spec), "transcripcion": script, "auditoria": auditoria,
            "url": url, "archivo": fname, "provider": provider.name, "voz": provider.voice_id,
            "modelo": getattr(provider, "model", ""), "duracion_seg": duracion,
        }
        cache = P.audio_cache()
        cache[licencia] = entrada
        ruta = P.guardar_audio_cache(cache)
        self.stdout.write(self.style.SUCCESS(f"  Audio {licencia} → {url} (guardado en {ruta})."))
        return entrada
