from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("schools", "0029_curso_slug"),
    ]

    operations = [
        migrations.AlterField(
            model_name="curso",
            name="slug",
            field=models.SlugField(max_length=80, unique=True, blank=True),
        ),
    ]
