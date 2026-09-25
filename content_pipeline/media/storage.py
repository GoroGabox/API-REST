"""Destino de los audios generados: disco local o bucket S3-compatible.

Railway tiene filesystem efímero, así que en producción los .mp3 van a un bucket
(Cloudflare R2 o AWS S3) con URL pública. En local basta MEDIA_ROOT + runserver.
Como TTS/email, se elige por configuración.

Config (settings o entorno):
    AUDIO_STORAGE               -> 'local' (default) | 's3'
    AUDIO_S3_BUCKET             -> nombre del bucket
    AUDIO_S3_ENDPOINT_URL       -> R2: https://<account>.r2.cloudflarestorage.com (vacío en AWS)
    AUDIO_S3_REGION             -> 'auto' en R2; región en AWS
    AUDIO_S3_ACCESS_KEY_ID / AUDIO_S3_SECRET_ACCESS_KEY
    AUDIO_S3_PUBLIC_BASE_URL    -> URL pública del bucket (R2: dominio r2.dev o propio)
    AUDIO_S3_PREFIX             -> 'course_audio/' (default)
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from content_pipeline.media.tts import _conf


class StorageError(RuntimeError):
    pass


class AudioStorage(Protocol):
    name: str

    def exists(self, filename: str) -> bool: ...
    def url(self, filename: str) -> str: ...
    def save(self, filename: str, data: bytes) -> str: ...


def _with_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


class LocalStorage:
    name = "local"

    def __init__(self, *, media_dir: str | Path | None = None, base_url: str | None = None):
        if media_dir is None:
            from django.conf import settings
            media_dir = Path(settings.MEDIA_ROOT) / "course_audio"
        if base_url is None:
            from django.conf import settings
            base_url = f"http://127.0.0.1:8000{settings.MEDIA_URL}course_audio/"
        self.media_dir = Path(media_dir)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.base_url = _with_slash(base_url)

    def exists(self, filename: str) -> bool:
        return (self.media_dir / filename).exists()

    def url(self, filename: str) -> str:
        return self.base_url + filename

    def save(self, filename: str, data: bytes) -> str:
        (self.media_dir / filename).write_bytes(data)
        return self.url(filename)

    def describe(self) -> str:
        return str(self.media_dir)


class S3Storage:
    name = "s3"

    def __init__(self, *, client=None, bucket: str | None = None, public_base_url: str | None = None,
                 prefix: str | None = None):
        self.bucket = bucket or _conf("AUDIO_S3_BUCKET")
        public = public_base_url or _conf("AUDIO_S3_PUBLIC_BASE_URL")
        prefix = prefix if prefix is not None else _conf("AUDIO_S3_PREFIX", "course_audio/")
        if not self.bucket:
            raise StorageError("Falta AUDIO_S3_BUCKET.")
        if not public:
            raise StorageError("Falta AUDIO_S3_PUBLIC_BASE_URL (URL pública del bucket).")
        self.public_base_url = _with_slash(public)
        self.prefix = _with_slash(prefix) if prefix else ""
        self.client = client or self._build_client()

    @staticmethod
    def _build_client():
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise StorageError("Falta boto3 para subir al bucket: pip install boto3") from exc
        key_id = _conf("AUDIO_S3_ACCESS_KEY_ID")
        secret = _conf("AUDIO_S3_SECRET_ACCESS_KEY")
        if not key_id or not secret:
            raise StorageError("Faltan AUDIO_S3_ACCESS_KEY_ID / AUDIO_S3_SECRET_ACCESS_KEY.")
        return boto3.client(
            "s3",
            endpoint_url=_conf("AUDIO_S3_ENDPOINT_URL") or None,
            region_name=_conf("AUDIO_S3_REGION") or None,
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
        )

    def _key(self, filename: str) -> str:
        return self.prefix + filename

    def exists(self, filename: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(filename))
            return True
        except Exception as exc:
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise StorageError(f"No se pudo consultar el bucket: {exc}") from exc

    def url(self, filename: str) -> str:
        return self.public_base_url + self._key(filename)

    def save(self, filename: str, data: bytes) -> str:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._key(filename),
                Body=data,
                ContentType="audio/mpeg",
                CacheControl="public, max-age=31536000",
            )
        except Exception as exc:
            raise StorageError(f"Fallo la subida de {filename}: {exc}") from exc
        return self.url(filename)

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"


def get_audio_storage(name: str | None = None, **opts) -> AudioStorage:
    """`opts` (media_dir/base_url) solo aplican al storage local."""
    name = (name or _conf("AUDIO_STORAGE", "local") or "local").lower()
    if name == "local":
        return LocalStorage(**opts)
    if name == "s3":
        return S3Storage()
    raise StorageError(f"Storage de audio desconocido: {name}. Disponibles: ['local', 's3'].")
