import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0008_estudianteleccion_curso_estudianteleccion_updated_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='usuario',
            name='activation_token',
            field=models.UUIDField(
                default=uuid.uuid4,
                editable=False,
                null=True,
                blank=True,
                db_index=True,
            ),
        ),
    ]
