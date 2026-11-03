"""Deja la BD LOCAL en un estado limpio para probar compras y accesos.

Borra ventas, llaves/códigos, inscripciones y todos los usuarios salvo el
superusuario `administrador@email.com`; deja solo la Escuela Demo (10 llaves,
sin suscripción) y solo el Curso Clase B (planes 7/14/35 días). Ajusta los
productos de suscripción para que sean comprables con cupos reales. El banco
de ejercicios se conserva (`Ejercicio.curso` es SET_NULL).

Cuentas creadas (contraseña `PASSWORD_PRUEBAS`):
  - director@email.com      director de la Escuela Demo
  - est.escuela1@email.com  estudiante de la Escuela Demo, sin acceso
  - est.escuela2@email.com  estudiante de la Escuela Demo, sin acceso
  - est.libre1@email.com    estudiante sin escuela (compra B2C / canje)
  - est.libre2@email.com    estudiante sin escuela (compra B2C / canje)

Uso:
    python manage.py seed_pruebas

Idempotente. Se niega a correr con `DATABASE_URL` (solo SQLite local).
"""
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from schools.models import Curso, Escuela, PlanCurso

PASSWORD_PRUEBAS = "Prueba1234!"
ADMIN_EMAIL = "administrador@email.com"
ESCUELA_NOMBRE = "Escuela Demo"
CURSO_CODIGO = "B"
PLANES_B = [(7, 6990), (14, 13980), (35, 34950)]

DIRECTOR = ("director@email.com", "Diego", "Director")
ESTUDIANTES_ESCUELA = [
    ("est.escuela1@email.com", "Ana", "Escuela"),
    ("est.escuela2@email.com", "Bruno", "Escuela"),
]
ESTUDIANTES_LIBRES = [
    ("est.libre1@email.com", "Carla", "Libre"),
    ("est.libre2@email.com", "Daniel", "Libre"),
]


class Command(BaseCommand):
    help = "Limpia usuarios/ventas/cursos de la BD local y crea cuentas de prueba."

    def handle(self, *args, **opts):
        if os.getenv("DATABASE_URL"):
            raise CommandError("DATABASE_URL está definida: este comando es solo para la BD local.")

        # Imports locales: schools es la app de más bajo nivel.
        from accounts.models import Usuario
        from sales.models import AccessKey, Producto, SolicitudAcceso, Venta

        with transaction.atomic():
            curso = Curso.objects.filter(codigo=CURSO_CODIGO).first()
            if curso is None:
                raise CommandError(f"No existe el curso con código {CURSO_CODIGO!r}.")
            if not Usuario.objects.filter(email=ADMIN_EMAIL, is_superuser=True).exists():
                raise CommandError(f"No existe el superusuario {ADMIN_EMAIL}.")

            # 1. Limpiar
            Venta.objects.all().delete()
            AccessKey.objects.all().delete()
            SolicitudAcceso.objects.all().delete()
            Usuario.objects.exclude(email=ADMIN_EMAIL).delete()
            Curso.objects.exclude(pk=curso.pk).delete()

            escuela = Escuela.objects.filter(nombre=ESCUELA_NOMBRE).first() or Escuela(nombre=ESCUELA_NOMBRE)
            escuela.basic_key = 10
            escuela.basic_access = False
            escuela.basic_seats_max = 0
            escuela.basic_seats_used = 0
            escuela.basic_access_until = None
            escuela.save()
            Escuela.objects.exclude(pk=escuela.pk).delete()

            # 2. Planes del curso
            for dias, precio in PLANES_B:
                PlanCurso.objects.update_or_create(
                    curso=curso, dias=dias, defaults={"precio": precio, "activo": True},
                )

            # 3. Productos
            Producto.objects.filter(nombre="Plan Plus").update(
                cant_seats=20, duracion_dias=30, basic_access=True, activo=True,
            )
            Producto.objects.filter(nombre="Plan Pro").update(activo=False)

            # 4. Cuentas
            email, nombre, apellido = DIRECTOR
            Usuario.objects.create_user(
                email, nombre, apellido, PASSWORD_PRUEBAS,
                is_director=True, escuela=escuela, is_active=True,
            )
            for email, nombre, apellido in ESTUDIANTES_ESCUELA:
                Usuario.objects.create_user(
                    email, nombre, apellido, PASSWORD_PRUEBAS,
                    is_estudiante=True, escuela=escuela, is_active=True,
                )
            for email, nombre, apellido in ESTUDIANTES_LIBRES:
                Usuario.objects.create_user(
                    email, nombre, apellido, PASSWORD_PRUEBAS,
                    is_estudiante=True, is_active=True,
                )

        self._resumen(Usuario, Producto, curso, escuela)

    def _resumen(self, Usuario, Producto, curso, escuela):
        self.stdout.write(self.style.SUCCESS("BD local lista para pruebas."))
        for u in Usuario.objects.select_related("escuela").order_by("id"):
            rol = "admin" if u.is_superuser else "director" if u.is_director else "estudiante"
            esc = u.escuela.nombre if u.escuela else "sin escuela"
            self.stdout.write(f"  {u.email:<26} {rol:<10} {esc}")
        escuela.refresh_from_db()
        self.stdout.write(
            f"Escuela: {escuela.nombre} (código {escuela.codigo}) · llaves {escuela.basic_key} · "
            f"suscripción {'sí' if escuela.basic_access else 'no'}"
        )
        planes = ", ".join(f"{p.dias}d ${p.precio}" for p in curso.planes.filter(activo=True))
        self.stdout.write(f"Curso: {curso.nombre} [{curso.codigo}] · {planes}")
        for p in Producto.objects.filter(activo=True).order_by("id"):
            self.stdout.write(
                f"Producto: {p.nombre} ({p.tipo}) · llaves {p.cant_basic_key} · cupos {p.cant_seats} · "
                f"días {p.duracion_dias or '-'}"
            )
