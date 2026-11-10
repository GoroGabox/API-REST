"""Historial de precios (`CambioPrecio`) de `PlanCurso` y `Producto`.

pre_save guarda los valores de precio anteriores en la instancia; post_save
crea el registro solo si es nuevo o si cambió algún campo de precio. El
usuario sale de `autotestAPI.auditoria.usuario_actual()` (la vista envuelve el
guardado con `como_usuario`).
"""
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from autotestAPI.auditoria import usuario_actual
from schools.models import PlanCurso

from .models import CambioPrecio, Producto

CAMPOS_PLAN = ("precio", "precio_referencia", "activo")
CAMPOS_PRODUCTO = ("valor_neto", "descuento", "activo")


def _entero(v):
    return None if v is None else int(round(float(v)))


def _previos(model, instance, campos):
    if not instance.pk:
        return None
    return model.objects.filter(pk=instance.pk).values(*campos).first()


@receiver(pre_save, sender=PlanCurso)
def _plan_pre(sender, instance, **kwargs):
    instance._precio_previo = _previos(PlanCurso, instance, CAMPOS_PLAN)


@receiver(post_save, sender=PlanCurso)
def _plan_post(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    prev = getattr(instance, "_precio_previo", None)
    if not created and prev and all(prev[c] == getattr(instance, c) for c in CAMPOS_PLAN):
        return
    prev = prev or {}
    CambioPrecio.objects.create(
        tipo="plan_curso",
        objeto_id=instance.pk,
        nombre=f"{instance.curso.nombre} · {instance.dias} días",
        curso=instance.curso,
        dias=instance.dias,
        precio_anterior=_entero(prev.get("precio")),
        precio_nuevo=_entero(instance.precio),
        referencia_anterior=_entero(prev.get("precio_referencia")),
        referencia_nueva=_entero(instance.precio_referencia),
        activo=instance.activo,
        usuario=usuario_actual(),
    )


@receiver(pre_save, sender=Producto)
def _producto_pre(sender, instance, **kwargs):
    instance._precio_previo = _previos(Producto, instance, CAMPOS_PRODUCTO)


@receiver(post_save, sender=Producto)
def _producto_post(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    prev = getattr(instance, "_precio_previo", None)
    # Decimal vs str/float según cómo llegó el valor: se compara normalizado.
    if not created and prev and all(_norm(prev[c]) == _norm(getattr(instance, c)) for c in CAMPOS_PRODUCTO):
        return
    prev = prev or {}
    CambioPrecio.objects.create(
        tipo="producto",
        objeto_id=instance.pk,
        nombre=instance.nombre,
        precio_anterior=_entero(prev.get("valor_neto")),
        precio_nuevo=_entero(instance.valor_neto),
        descuento_anterior=prev.get("descuento"),
        descuento_nuevo=instance.descuento,
        activo=instance.activo,
        usuario=usuario_actual(),
    )


def _norm(v):
    if v is None or isinstance(v, bool):
        return v
    return round(float(v), 2)
