from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import Usuario, DirectorProfile, EstudianteProfile, Certificado, Prueba, PruebaEjercicio

@admin.register(Usuario)
class UsuarioAdminConfig(UserAdmin):
    model = Usuario

    list_display = ("email", "nombre", "apellido", "escuela", "is_active", "is_staff", "is_director", "is_estudiante")
    search_fields = ("email", "nombre", "apellido", "escuela")
    ordering = ("email",)

    # 🔹 Agregamos también last_login y date_joined como read-only
    readonly_fields = ("password", "last_login", "date_joined")

    fieldsets = (
        (None, {
            "fields": ("email", "password")
        }),
        ("Información personal", {
            "fields": ("nombre", "apellido", "escuela")
        }),
        ("Permisos", {
            "fields": (
                "is_active",
                "is_staff",
                "is_superuser",
                "is_director",
                "is_estudiante",
                "groups",
                "user_permissions",
            )
        }),
        ("Fechas importantes", {
            "fields": ("last_login", "date_joined"),
        }),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "email",
                    "nombre",
                    "apellido",
                    "escuela",
                    "password1",
                    "password2",
                    "is_active",
                    "is_staff",
                    "is_director",
                    "is_estudiante",
                ),
            },
        ),
    )
        # 👇👇👇 ACCIÓN PARA RESETEAR PASSWORD
    actions = ["set_test_password"]

    def set_test_password(self, request, queryset):
        """
        Pone una contraseña de prueba a los usuarios seleccionados.
        """
        new_password = "Test1234!"  # cámbiala por lo que quieras

        for user in queryset:
            user.set_password(new_password)
            user.save()

        self.message_user(
            request,
            f"Se actualizó la contraseña de {queryset.count()} usuario(s) a '{new_password}'.",
        )

    set_test_password.short_description = "Poner contraseña de prueba a usuarios seleccionados"


admin.site.register(DirectorProfile)
admin.site.register(EstudianteProfile)
admin.site.register(Certificado)
admin.site.register(Prueba)
admin.site.register(PruebaEjercicio)
