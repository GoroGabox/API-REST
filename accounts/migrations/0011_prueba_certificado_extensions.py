import uuid

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0010_alter_usuario_options_and_more'),
        ('schools', '0011_alter_curso_options_alter_ejercicio_options'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='prueba',
            options={'ordering': ['-fecha', '-id']},
        ),
        migrations.AddField(
            model_name='prueba',
            name='curso',
            field=models.ForeignKey(
                on_delete=models.deletion.SET_NULL, null=True, blank=True,
                to='schools.curso', related_name='pruebas',
            ),
        ),
        migrations.AddField(
            model_name='prueba',
            name='tipo',
            field=models.CharField(
                max_length=20, default='rapida',
                choices=[('completa', 'Completa'), ('rapida', 'Rápida'), ('categoria', 'Por categoría')],
            ),
        ),
        migrations.AddField(
            model_name='prueba',
            name='modalidad',
            field=models.CharField(
                max_length=20, default='practica',
                choices=[('practica', 'Práctica'), ('evaluacion', 'Evaluación')],
            ),
        ),
        migrations.AddField(
            model_name='prueba',
            name='completada_en',
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name='prueba',
            name='total_correctas',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='prueba',
            name='score',
            field=models.DecimalField(max_digits=5, decimal_places=2, default=0),
        ),
        migrations.AlterField(
            model_name='pruebaejercicio',
            name='prueba',
            field=models.ForeignKey(
                on_delete=models.deletion.CASCADE,
                to='accounts.prueba', related_name='items',
            ),
        ),
        migrations.AlterField(
            model_name='pruebaejercicio',
            name='respuesta_estudiante',
            field=models.CharField(max_length=200, blank=True, default=''),
        ),
        migrations.AddField(
            model_name='pruebaejercicio',
            name='correcta',
            field=models.BooleanField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name='certificado',
            name='prueba',
            field=models.ForeignKey(
                on_delete=models.deletion.SET_NULL, null=True, blank=True,
                to='accounts.prueba', related_name='certificados',
            ),
        ),
        migrations.AddField(
            model_name='certificado',
            name='codigo',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AddConstraint(
            model_name='certificado',
            constraint=models.UniqueConstraint(
                fields=['estudiante', 'curso'],
                name='unique_certificado_por_estudiante_curso',
            ),
        ),
    ]
