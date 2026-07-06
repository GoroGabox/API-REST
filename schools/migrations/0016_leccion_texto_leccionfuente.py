# Generated manually for the A2 content pipeline.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('schools', '0015_escuela_basic_seats_max_escuela_basic_seats_used_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='leccion',
            name='tipo',
            field=models.CharField(
                choices=[
                    ('texto', 'Texto / Lectura'),
                    ('video', 'Video'),
                    ('audio', 'Audio'),
                    ('quiz', 'Quiz'),
                    ('drag', 'Drag & Drop'),
                    ('identify', 'Identificar'),
                ],
                default='video',
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name='LeccionFuente',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('fuente_nombre', models.CharField(max_length=255)),
                ('pagina_inicio', models.IntegerField()),
                ('pagina_fin', models.IntegerField()),
                ('tema_regulatorio', models.CharField(blank=True, default='', max_length=255)),
                ('fragmento_resumen', models.TextField(blank=True, default='')),
                ('hash_fragmento', models.CharField(blank=True, default='', max_length=64)),
                ('leccion', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='fuentes', to='schools.leccion')),
            ],
            options={
                'ordering': ['leccion', 'pagina_inicio'],
            },
        ),
    ]
