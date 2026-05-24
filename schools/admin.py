from django.contrib import admin
from .models import Escuela, Curso, Leccion, Ejercicio, Glosario, Categoria

# Register your models here.
admin.site.register(Escuela)
admin.site.register(Curso)
admin.site.register(Leccion)
admin.site.register(Ejercicio)
admin.site.register(Glosario)
admin.site.register(Categoria)