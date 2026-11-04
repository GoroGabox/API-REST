"""Descarga de cursos para estudiar sin conexión (app móvil).

Un estudiante registra un ``DispositivoOffline`` (UUID que genera la app) y pide una
``LicenciaOffline`` por curso. La licencia trae la llave de contenido (CEK) con la que
la app cifra lo descargado y una fecha ``offline_hasta`` firmada (Ed25519): el
contenido solo se abre hasta min(fin del acceso, última conexión + 7 días). Revocar
la licencia (o perder el acceso) hace que la app borre la CEK → contenido ilegible.

La CEK no se guarda: se deriva de ``OFFLINE_MASTER_KEY`` + ``sal`` de la licencia
(ver ``offline.crypto``). Una licencia nueva (re-descarga) usa sal nueva.
"""
import secrets
import uuid

from django.conf import settings
from django.db import models

from schools.models import Curso


def _sal():
    return secrets.token_hex(16)


class DispositivoOffline(models.Model):
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='dispositivos_offline')
    device_id = models.UUIDField()
    plataforma = models.CharField(max_length=20, blank=True, default='')
    nombre = models.CharField(max_length=100, blank=True, default='')
    creado = models.DateTimeField(auto_now_add=True)
    ultimo_uso = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['usuario', 'device_id'], name='uniq_dispositivo_offline_usuario'),
        ]
        ordering = ['-ultimo_uso']

    def __str__(self):
        return f"{self.usuario_id} · {self.nombre or self.device_id}"


class LicenciaOffline(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dispositivo = models.ForeignKey(DispositivoOffline, on_delete=models.CASCADE, related_name='licencias')
    curso = models.ForeignKey(Curso, on_delete=models.CASCADE, related_name='licencias_offline')
    sal = models.CharField(max_length=32, default=_sal)
    emitida_en = models.DateTimeField(auto_now_add=True)
    renovada_en = models.DateTimeField()
    offline_hasta = models.DateTimeField()
    revocada = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['dispositivo', 'curso'], name='uniq_licencia_offline_dispositivo_curso'),
        ]
        ordering = ['-renovada_en']

    def __str__(self):
        return f"Licencia {self.id} · curso {self.curso_id}"
