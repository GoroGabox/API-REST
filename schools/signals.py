"""Mantiene el espejo ``Leccion.url_*`` al editar recursos (admin, API, scripts)."""
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Leccion, LeccionRecurso
from .services import sync_media_mirror


@receiver(post_save, sender=LeccionRecurso)
def _recurso_guardado(sender, instance, raw=False, **kwargs):
    if raw:            # loaddata
        return
    sync_media_mirror(instance.leccion)


@receiver(post_delete, sender=LeccionRecurso)
def _recurso_borrado(sender, instance, **kwargs):
    leccion = Leccion.objects.filter(pk=instance.leccion_id).first()
    if leccion is not None:    # si se borra la lección entera (cascade) no hay nada que sincronizar
        sync_media_mirror(leccion, url_quitada=instance.url)
