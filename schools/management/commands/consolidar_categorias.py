"""Consolida las categorías en la taxonomía canónica única.

Mueve todo lo que apunta a una categoría heredada («Mecanica basica»,
«Legislación», «Transporte de pasajeros»…) a su categoría canónica de
``content_pipeline.taxonomy`` y borra la heredada. Así lecciones y ejercicios
comparten una sola lista para filtrar. Idempotente.

    python manage.py consolidar_categorias --dry-run
    python manage.py consolidar_categorias
"""
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from content_pipeline import taxonomy
from schools.models import Categoria


def _relaciones():
    """(modelo, campo) de todas las FK que apuntan a Categoria."""
    return [
        (rel.related_model, rel.field.name)
        for rel in Categoria._meta.related_objects
        if rel.one_to_many or rel.one_to_one
    ]


def plan_consolidacion():
    """{categoria_id_origen: nombre_canonico} para las que deben moverse."""
    canonicas = defaultdict(list)
    plan = {}
    for c in Categoria.objects.order_by('id'):
        destino = taxonomy.resolve(c.nombre)
        if c.nombre == destino:
            canonicas[destino].append(c.id)
        else:
            plan[c.id] = destino
    # Duplicados exactos de una canónica: se conserva la de menor id.
    for nombre, ids in canonicas.items():
        for extra in ids[1:]:
            plan[extra] = nombre
    return plan


class Command(BaseCommand):
    help = "Mueve lecciones, ejercicios y pruebas de categorías heredadas a la taxonomía canónica."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Solo reporta.")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        plan = plan_consolidacion()
        if not plan:
            self.stdout.write(self.style.SUCCESS("Nada que consolidar: todas las categorías son canónicas."))
            return
        relaciones = _relaciones()
        nombres = dict(Categoria.objects.values_list('id', 'nombre'))
        movidos = defaultdict(int)
        with transaction.atomic():
            for origen_id, destino_nombre in sorted(plan.items()):
                destino = (Categoria.objects.filter(nombre=destino_nombre).exclude(id=origen_id)
                           .order_by('id').first())
                if destino is None and not dry:
                    destino = Categoria.objects.create(
                        nombre=destino_nombre, color_hex=taxonomy.color_for(destino_nombre))
                linea = []
                for modelo, campo in relaciones:
                    qs = modelo.objects.filter(**{f"{campo}_id": origen_id})
                    n = qs.count()
                    if n:
                        linea.append(f"{modelo.__name__}={n}")
                        movidos[modelo.__name__] += n
                        if not dry:
                            qs.update(**{f"{campo}_id": destino.id})
                self.stdout.write(f"  «{nombres[origen_id]}» → «{destino_nombre}» ({', '.join(linea) or 'vacía'})")
                if not dry:
                    Categoria.objects.filter(id=origen_id).delete()
            # Color oficial en las canónicas.
            if not dry:
                for c in Categoria.objects.all():
                    color = taxonomy.color_for(c.nombre)
                    if c.nombre in taxonomy.CATEGORY_NAMES + [taxonomy.FALLBACK] and c.color_hex != color:
                        c.color_hex = color
                        c.save(update_fields=['color_hex'])
            if dry:
                transaction.set_rollback(True)
        resumen = ", ".join(f"{k}: {v}" for k, v in sorted(movidos.items())) or "nada"
        estado = "DRY-RUN" if dry else "Listo"
        self.stdout.write(self.style.SUCCESS(
            f"{estado}: {len(plan)} categorías heredadas consolidadas · filas movidas → {resumen}"))
