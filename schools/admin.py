from django.contrib import admin
from .models import Escuela, Curso, Leccion, LeccionFuente, LeccionRecurso, Ejercicio, Glosario, Categoria

# Register your models here.
admin.site.register(Escuela)
admin.site.register(Curso)


class LeccionRecursoInline(admin.TabularInline):
    model = LeccionRecurso
    extra = 0
    fields = ("orden", "tipo", "rol", "clave", "url", "titulo", "meta")


@admin.register(Leccion)
class LeccionAdmin(admin.ModelAdmin):
    list_display = ("nombre", "curso", "unidad", "posicion", "tipo")
    list_filter = ("curso", "tipo")
    search_fields = ("nombre",)
    inlines = [LeccionRecursoInline]

admin.site.register(LeccionFuente)
admin.site.register(LeccionRecurso)
admin.site.register(Ejercicio)
admin.site.register(Glosario)
admin.site.register(Categoria)
