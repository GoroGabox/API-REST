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

- El audio se nombra por contenido (``{codigo}/{sha1(guion|proveedor|voz|modelo)}.mp3``):
  re-correr no re-sintetiza y otro guion o voz genera otro archivo.
- ``audio_meta.hash_contenido`` guarda la huella del texto de la lección: si el
  texto cambió (corrección posterior), el guion queda obsoleto y se rehace.
- Las lecciones ``anexo`` (solo figuras) y las de texto muy corto no se locutan.
- ``--solo-estimar`` imprime lo pendiente (guiones, caracteres y USD aprox.) sin escribir.
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_pipeline.exporters.json_exporter import read_json, write_json
from content_pipeline.llm.client import LLMClient, draft_model
from content_pipeline.media.narration import (
    audit_script,
    build_narration_script_meta,
    content_hash,
    has_findings,
    strip_markdown,
)
from content_pipeline.media.recursos import upsert_recurso
from content_pipeline.media.storage import StorageError, get_audio_storage
from content_pipeline.media.tts import (
    TTSError,
    audio_filename,
    audio_hash,
    get_tts_provider,
    mp3_duracion_seg,
    usd_por_1k,
)

# Texto locutable mínimo: por debajo (portadas, anexos de figuras) no se narra.
MIN_CHARS_LOCUTABLE = 200


def _audio_actual(lesson: dict) -> dict | None:
    """Recurso de narración ya enlazado en la lección (o ``None``)."""
    for r in lesson.get("recursos") or []:
        if isinstance(r, dict) and r.get("tipo") == "audio" and (r.get("url") or "").startswith("http"):
            return r
    return None


def _motivo_omitir(lesson: dict) -> str | None:
    if lesson.get("anexo"):
        return "anexo"
    contenido = lesson.get("contenido")
    if not isinstance(contenido, str) or len(strip_markdown(contenido)) < MIN_CHARS_LOCUTABLE:
        return "texto_corto"
    return None


def _guion_obsoleto(lesson: dict) -> bool:
    meta = lesson.get("audio_meta") if isinstance(lesson.get("audio_meta"), dict) else {}
    previo = meta.get("hash_contenido")
    return bool((lesson.get("transcripcion") or "").strip() and previo
                and previo != content_hash(lesson.get("contenido")))


def _resumen_auditoria(lessons: list[dict], omitidas: list[dict]) -> dict:
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
        "obsoletas": [l.get("nombre") for l in lessons if _guion_obsoleto(l)],
        "omitidas": omitidas,
        "con_audio": sum(1 for l in lessons if _audio_actual(l)),
        "duracion_total_seg": sum(int(m.get("duracion_seg") or 0) for m in metas),
    }


def _set_audio(lesson: dict, url: str, fname: str, duracion: int | None) -> None:
    """Narración como recurso de la lección (clave ``audio``) + espejo ``url_audio``."""
    lesson["url_audio"] = url        # espejo de compatibilidad (JSON/clientes previos)
    meta: dict = {"archivo": fname}
    if duracion:
        meta["duracion_seg"] = duracion
    upsert_recurso(lesson, {"tipo": "audio", "rol": "narracion", "clave": "audio", "url": url,
                            "orden": 1000, "titulo": "Narración de la lección", "meta": meta})


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
        fase.add_argument("--solo-estimar", action="store_true",
                          help="Solo informa lo pendiente (guiones, caracteres TTS, USD aprox.). No escribe.")
        parser.add_argument("--rehacer-guion", action="store_true",
                            help="Redacta el guion aunque la lección ya tenga transcripcion vigente.")
        parser.add_argument("--estricto", action="store_true",
                            help="No sintetiza lecciones cuyo guion tiene hallazgos (cifras nuevas/truncado/corto).")
        parser.add_argument("--provider", default=None,
                            help="Proveedor TTS: elevenlabs | openai (default: TTS_PROVIDER).")
        parser.add_argument("--voice", default=None, help="Override del id de voz del proveedor.")
        parser.add_argument("--storage", default=None,
                            help="Destino de los audios: local | s3 (default: MEDIA_STORAGE o local).")
        parser.add_argument("--tipos", default="texto",
                            help="Tipos de lección a locutar, separados por coma (default: texto).")
        parser.add_argument("--media-dir", default=None,
                            help="[local] Carpeta destino (default: MEDIA_ROOT/course_audio).")
        parser.add_argument("--base-url", default=None,
                            help="[local] Prefijo URL de url_audio (default: http://127.0.0.1:8000/media/course_audio/).")
        parser.add_argument("--limit", type=int, default=None, help="Solo las primeras N lecciones (para probar).")
        parser.add_argument("--overwrite", action="store_true", help="Regenera el audio aunque ya exista.")

    # ------------------------------------------------------------------
    def _proveedor(self, opts, *, requerido: bool):
        try:
            provider = get_tts_provider(opts["provider"])
        except TTSError as exc:
            if requerido:
                raise CommandError(str(exc)) from exc
            return None
        if opts["voice"]:
            provider.voice_id = opts["voice"]  # type: ignore[attr-defined]
        return provider

    def _estimar(self, objetivo, provider, opts) -> None:
        """Imprime guiones y audios pendientes, caracteres de TTS y USD aproximados."""
        guiones = chars = audios = 0
        for _, lesson in objetivo:
            script = (lesson.get("transcripcion") or "").strip()
            rehacer = not script or opts["rehacer_guion"] or _guion_obsoleto(lesson)
            if rehacer:
                if opts["solo_audio"]:
                    continue                   # --solo-audio no redacta: no se sintetiza
                guiones += 1
                script = strip_markdown(lesson.get("contenido") or "")
            elif not opts["overwrite"]:
                actual = _audio_actual(lesson)
                if actual and provider is None:
                    continue
                if actual and (actual.get("meta") or {}).get("archivo") == audio_filename(
                        self._codigo, script, provider):
                    continue
            audios += 1
            chars += len(script)
        linea = f"Pendiente: {guiones} guiones · {audios} audios · ~{chars:,} caracteres de TTS"
        nombre = provider.name if provider is not None else (opts["provider"] or "")
        precio = usd_por_1k(nombre) if nombre else None
        if precio is not None:
            linea += (f" · ~US${chars / 1000 * precio:,.2f} con {nombre} "
                      f"(US${precio}/1k car.; ajustable con TTS_USD_1K_{nombre.upper()})")
        self.stdout.write(linea)

    # ------------------------------------------------------------------
    def handle(self, *args, **opts):
        file_path = Path(opts["file"])
        if not file_path.exists():
            raise CommandError(f"No existe el archivo: {file_path}")
        data = read_json(file_path)
        if not isinstance(data, dict) or "lessons" not in data:
            raise CommandError("El JSON debe tener las claves 'manifest' y 'lessons'.")
        out_path = Path(opts["out"]) if opts["out"] else file_path

        tipos = {t.strip() for t in opts["tipos"].split(",") if t.strip()}
        self._codigo = ((data.get("manifest") or {}).get("curso") or {}).get("codigo", "curso")
        lessons = data["lessons"]
        omitidas_info: list[dict] = []
        objetivo = []
        for i, lesson in enumerate(lessons, start=1):
            if lesson.get("tipo") not in tipos:
                continue
            motivo = _motivo_omitir(lesson)
            if motivo:
                omitidas_info.append({"leccion": lesson.get("nombre"), "motivo": motivo})
                continue
            objetivo.append((i, lesson))
        if opts["limit"]:
            objetivo = objetivo[: opts["limit"]]
        self.stdout.write(
            f"Lecciones objetivo: {len(objetivo)} (tipos: {sorted(tipos)}; "
            f"sin locutar por anexo/texto corto: {len(omitidas_info)})."
        )

        if opts["solo_estimar"]:
            self._estimar(objetivo, self._proveedor(opts, requerido=False), opts)
            return

        hacer_guion = not opts["solo_audio"]
        hacer_audio = not opts["solo_guion"]

        provider = storage = None
        if hacer_audio:
            provider = self._proveedor(opts, requerido=True)
            storage_opts = {}
            if opts["media_dir"]:
                storage_opts["media_dir"] = opts["media_dir"]
            if opts["base_url"]:
                storage_opts["base_url"] = opts["base_url"]
            try:
                storage = get_audio_storage(opts["storage"], **storage_opts)
            except StorageError as exc:
                raise CommandError(str(exc)) from exc

        narration_client = None
        if hacer_guion:
            if LLMClient.is_available():
                narration_client = LLMClient()
                self.stdout.write(f"Guion locutado con IA ({draft_model()}).")
            else:
                self.stdout.write("Sin IA: guion = Markdown limpiado (calidad menor).")

        if hacer_audio:
            self.stdout.write(
                f"TTS: {provider.name} (voz {provider.voice_id}) -> {storage.name} ({storage.describe()})."
            )
            self._estimar(objetivo, provider, opts)

        def _guardar():
            data["auditoria_audio"] = _resumen_auditoria(lessons, omitidas_info)
            write_json(out_path, data)

        guiones = audios = enlazados = omitidas = 0
        for index, lesson in objetivo:
            nombre = lesson.get("nombre", f"leccion_{index}")
            meta = lesson.get("audio_meta") if isinstance(lesson.get("audio_meta"), dict) else {}
            hash_c = content_hash(lesson.get("contenido"))

            # ---- Fase 1: guion ------------------------------------------------
            script = (lesson.get("transcripcion") or "").strip()
            obsoleto = _guion_obsoleto(lesson)
            if hacer_guion and (not script or opts["rehacer_guion"] or obsoleto):
                motivo = " (cambió el texto de la lección)" if obsoleto else ""
                self.stdout.write(f"  [{index}] {nombre} — redactando guion{motivo}…")
                res = build_narration_script_meta(
                    lesson.get("contenido", ""), nombre=nombre, client=narration_client,
                )
                script = res.script
                if not script:
                    self.stderr.write("      sin contenido locutable, omito.")
                    omitidas += 1
                    continue
                # Guion nuevo: la meta de audio previa ya no aplica.
                meta = {**audit_script(script, lesson.get("contenido", ""), truncado=res.truncado),
                        "hash_contenido": hash_c}
                lesson["transcripcion"] = script
                lesson["audio_meta"] = meta
                obsoleto = False
                guiones += 1
                if has_findings(meta):
                    self.stdout.write(self.style.WARNING(
                        f"      [!] revisar guion: cifras nuevas {meta['cifras_nuevas'] or '—'}, "
                        f"truncado={meta['truncado']}, ratio={meta['ratio_largo']}"
                    ))
                _guardar()
            elif script and not meta.get("hash_contenido"):
                # Guion previo a la huella (o escrito a mano): se asume vigente.
                meta = {**meta, "hash_contenido": hash_c}
                lesson["audio_meta"] = meta

            if not hacer_audio:
                continue

            # ---- Fase 2: audio ------------------------------------------------
            if not script:
                self.stderr.write(f"  [{index}] {nombre} — sin guion (corre antes --solo-guion), omito.")
                omitidas += 1
                continue
            if obsoleto:
                self.stderr.write(
                    f"  [{index}] {nombre} — guion obsoleto (cambió el texto; corre --solo-guion), omito.")
                omitidas += 1
                continue
            if opts["estricto"] and has_findings(meta):
                self.stderr.write(f"  [{index}] {nombre} — guion con hallazgos (--estricto), omito.")
                omitidas += 1
                continue

            fname = audio_filename(self._codigo, script, provider)
            actual = _audio_actual(lesson)
            if not opts["overwrite"] and actual and (actual.get("meta") or {}).get("archivo") == fname:
                continue                                   # ya enlazado: nada que hacer
            duracion = meta.get("duracion_seg") if meta.get("archivo") == fname else None
            try:
                if not opts["overwrite"] and storage.exists(fname):
                    self.stdout.write(f"  [{index}] audio ya existe, lo enlazo: {fname}")
                    url = storage.url(fname)
                    enlazados += 1
                else:
                    self.stdout.write(f"  [{index}] {nombre} — sintetizando ({len(script):,} chars)…")
                    audio = provider.synthesize(script)
                    url = storage.save(fname, audio)
                    duracion = mp3_duracion_seg(audio) if fname.endswith(".mp3") else None
                    audios += 1
                    self.stdout.write(self.style.SUCCESS(f"      -> {fname} ({len(audio)//1024} KB)"))
            except (TTSError, StorageError) as exc:
                _guardar()
                raise CommandError(
                    f"Fallo en lección {index} ({nombre}): {exc}. "
                    f"Progreso guardado en {out_path}; re-corre para retomar."
                ) from exc

            _set_audio(lesson, url, fname, duracion)
            lesson["audio_meta"] = {
                **meta, "provider": provider.name, "voz": provider.voice_id,
                "modelo": getattr(provider, "model", ""), "chars": len(script),
                "archivo": fname, "hash_audio": audio_hash(script, provider),
                "duracion_seg": duracion,
            }
            _guardar()

        _guardar()
        resumen = data["auditoria_audio"]
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Guiones redactados: {guiones} · audios generados: {audios} · re-enlazados: {enlazados} · "
            f"omitidas: {omitidas} -> {out_path}"
        ))
        self.stdout.write(
            f"Auditoría de guiones: {len(resumen['con_cifras_nuevas'])} con cifras nuevas · "
            f"{resumen['truncados']} truncados · {resumen['cortos']} cortos · "
            f"{len(resumen['obsoletas'])} obsoletos · {resumen['con_audio']} lecciones con audio."
        )
        if opts["solo_guion"]:
            self.stdout.write("Siguiente: revisa los guiones marcados y corre con --solo-audio.")
