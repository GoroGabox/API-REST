"""Rellena los datos comerciales de lo que ya existía.

- `Venta.item_type`/`dias`/`buy_order` desde su `TransbankTransaction` (buy_order
  autoritativo) y, si no hay transacción, desde `curso`/`producto`.
- `CambioPrecio` inicial: una foto del precio actual de cada `PlanCurso` y
  `Producto`, para que la evolución de precios tenga un punto de partida.
"""
from django.db import migrations
from django.utils import timezone


def _dias_de(buy_order):
    partes = (buy_order or "").split("_")
    if len(partes) != 4:
        return None
    try:
        return int(partes[3])
    except (TypeError, ValueError):
        return None


def rellenar_ventas(apps, schema_editor):
    Venta = apps.get_model("sales", "Venta")
    Transaccion = apps.get_model("sales", "TransbankTransaction")
    buy_orders = dict(Transaccion.objects.values_list("sale_id", "buy_order"))
    for venta in Venta.objects.filter(item_type__isnull=True).iterator():
        bo = buy_orders.get(venta.id)
        venta.item_type = "curso" if venta.curso_id else "producto"
        venta.buy_order = bo or None
        if venta.item_type == "curso":
            # Compras de curso anteriores al plan por duración eran de 7 días.
            venta.dias = _dias_de(bo) or 7
        venta.save(update_fields=["item_type", "buy_order", "dias"])


def precios_iniciales(apps, schema_editor):
    CambioPrecio = apps.get_model("sales", "CambioPrecio")
    PlanCurso = apps.get_model("schools", "PlanCurso")
    Producto = apps.get_model("sales", "Producto")
    ahora = timezone.now()
    for plan in PlanCurso.objects.select_related("curso").all():
        CambioPrecio.objects.create(
            tipo="plan_curso", objeto_id=plan.id,
            nombre=f"{plan.curso.nombre} · {plan.dias} días",
            curso_id=plan.curso_id, dias=plan.dias,
            precio_nuevo=plan.precio, referencia_nueva=plan.precio_referencia,
            activo=plan.activo, fecha=ahora,
        )
    for p in Producto.objects.all():
        CambioPrecio.objects.create(
            tipo="producto", objeto_id=p.id, nombre=p.nombre,
            precio_nuevo=None if p.valor_neto is None else int(round(float(p.valor_neto))),
            descuento_nuevo=p.descuento, activo=p.activo, fecha=ahora,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0026_comercial_intentos_historial"),
        ("schools", "0030_curso_slug_unico"),
    ]

    operations = [
        migrations.RunPython(rellenar_ventas, migrations.RunPython.noop),
        migrations.RunPython(precios_iniciales, migrations.RunPython.noop),
    ]
