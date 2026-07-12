"""Signals de la app accounts.

- Emisión automática de certificados cuando una Prueba evaluación-final-curso
  se marca aprobada.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Prueba, Certificado
from .services import emitir_certificado_si_corresponde


@receiver(post_save, sender=Prueba)
def _emit_certificado_on_prueba_aprobada(sender, instance: Prueba, created, **kwargs):
    # En el flujo normal la Prueba se crea con aprobado=False y se actualiza
    # tras submit_prueba; pero también queremos cubrir creación directa con
    # aprobado=True (fixtures admin, scripts de seeding).
    emitir_certificado_si_corresponde(instance)

    # Notificación in-app + push al aprobar evaluación (no spam en prácticas).
    if not created and instance.aprobado and instance.modalidad == 'evaluacion':
        try:
            from .notifications import notificar
            notificar(
                instance.estudiante,
                tipo='test_passed',
                titulo='¡Aprobaste!',
                mensaje=f'Aprobaste la prueba con {instance.score}%.',
                data={'prueba_id': instance.id, 'score': float(instance.score)},
            )
        except Exception:
            pass


@receiver(post_save, sender=Certificado)
def _notificar_certificado(sender, instance: Certificado, created, **kwargs):
    if not created:
        return
    curso_nombre = instance.curso.nombre if instance.curso else ""
    try:
        from .notifications import notificar

        # Estudiante: hito relevante → también por email (respeta preferencia).
        notificar(
            instance.estudiante,
            tipo='certificate_issued',
            titulo='Certificado emitido',
            mensaje=f'Obtuviste tu certificado del curso {curso_nombre}.',
            data={'certificado_id': instance.id, 'codigo': str(instance.codigo)},
            email=True,
        )

        # Director(es) de la escuela del estudiante: aviso de que uno de sus
        # alumnos completó un curso. También por email (respeta preferencia).
        escuela_id = getattr(instance.estudiante, 'escuela_id', None)
        if escuela_id:
            from .models import Usuario
            est = instance.estudiante
            nombre_est = f'{est.nombre or ""} {est.apellido or ""}'.strip() or est.email
            directores = Usuario.objects.filter(
                escuela_id=escuela_id, is_director=True
            ).exclude(pk=est.pk)
            for director in directores:
                notificar(
                    director,
                    tipo='certificate_issued',
                    titulo='Nuevo certificado en tu escuela',
                    mensaje=f'{nombre_est} obtuvo el certificado del curso {curso_nombre}.',
                    data={
                        'certificado_id': instance.id,
                        'estudiante_id': est.id,
                        'curso_id': instance.curso_id,
                    },
                    email=True,
                )
    except Exception:
        pass
