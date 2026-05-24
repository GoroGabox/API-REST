from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('schools', '0009_alter_ejercicio_pregunta_alter_ejercicio_respuesta'),
    ]

    operations = [
        migrations.RenameField(
            model_name='curso',
            old_name='is_proffesional',
            new_name='is_profesional',
        ),
    ]
