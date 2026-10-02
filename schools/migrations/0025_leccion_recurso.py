from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('schools', '0024_leccion_imagen'),
    ]

    operations = [
        # La biblioteca libera el nombre inverso 'recursos' de Leccion (no cambia la BD).
        migrations.AlterField(
            model_name='recurso',
            name='leccion',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='recursos_biblioteca', to='schools.leccion'),
        ),
        migrations.CreateModel(
            name='LeccionRecurso',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('tipo', models.CharField(choices=[('imagen', 'Imagen'), ('audio', 'Audio'), ('video', 'Video'), ('pdf', 'PDF')], max_length=10)),
                ('rol', models.CharField(choices=[('figura', 'Figura del libro'), ('narracion', 'Narración (audio de la lectura)'), ('principal', 'Medio principal'), ('paginas_libro', 'Páginas del libro'), ('descarga', 'Descarga')], default='figura', max_length=20)),
                ('clave', models.CharField(max_length=64)),
                ('url', models.URLField(max_length=500)),
                ('orden', models.IntegerField(default=0)),
                ('titulo', models.CharField(blank=True, default='', max_length=255)),
                ('meta', models.JSONField(blank=True, default=dict)),
                ('leccion', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='recursos', to='schools.leccion')),
            ],
            options={
                'ordering': ['orden', 'id'],
                'constraints': [models.UniqueConstraint(fields=('leccion', 'clave'), name='uniq_recurso_leccion_clave')],
            },
        ),
    ]
