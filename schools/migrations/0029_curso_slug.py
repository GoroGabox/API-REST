from django.db import migrations, models


def poblar_slugs(apps, schema_editor):
    """Slug único para cada curso existente, desde su nombre (orden por id)."""
    from django.utils.text import slugify

    Curso = apps.get_model("schools", "Curso")
    reservados = {"generate", "plan", "import"}
    usados = set()
    for curso in Curso.objects.order_by("id"):
        base = slugify(curso.nombre or "")[:70] or "curso"
        if base in reservados or base.isdigit():
            base = f"curso-{base}"
        slug, n = base, 2
        while slug in usados:
            slug, n = f"{base}-{n}", n + 1
        usados.add(slug)
        curso.slug = slug
        curso.save(update_fields=["slug"])


class Migration(migrations.Migration):

    dependencies = [
        ("schools", "0028_escuela_basic_access_until"),
    ]

    operations = [
        # 1) columna sin restricción, 2) poblar. La restricción única va en
        # 0030: en Postgres, alterar la tabla en la misma transacción que la
        # actualiza falla con "pending trigger events".
        migrations.AddField(
            model_name="curso",
            name="slug",
            field=models.SlugField(max_length=80, null=True, blank=True),
        ),
        migrations.RunPython(poblar_slugs, migrations.RunPython.noop),
    ]
