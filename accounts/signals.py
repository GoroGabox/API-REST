"""Signals de la app accounts.

- Emisión automática de certificados cuando una Prueba evaluación-final-curso
  se marca aprobada.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Prueba
from .services import emitir_certificado_si_corresponde


@receiver(post_save, sender=Prueba)
def _emit_certificado_on_prueba_aprobada(sender, instance: Prueba, created, **kwargs):
    # En el flujo normal la Prueba se crea con aprobado=False y se actualiza
    # tras submit_prueba; pero también queremos cubrir creación directa con
    # aprobado=True (fixtures admin, scripts de seeding).
    emitir_certificado_si_corresponde(instance)
