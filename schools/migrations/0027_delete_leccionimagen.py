from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('schools', '0026_migrar_medios_a_recursos'),
    ]

    operations = [
        migrations.DeleteModel(
            name='LeccionImagen',
        ),
    ]
