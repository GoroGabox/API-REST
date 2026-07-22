"""Seed mínimo para desarrollo: admin + escuela + director + estudiantes.

Uso:
    python manage.py seed_demo
    python manage.py seed_demo --reset   # borra usuarios existentes antes
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Usuario
from schools.models import Escuela


DEFAULT_PASSWORD = "Demo1234!"

USERS = [
    # (email, nombre, apellido, role, escuela_nombre|None)
    ("administrador@email.com", "Admin", "AutoTest", "admin", None),
    ("director@email.com",      "Diego", "Director", "director", "Escuela Demo"),
    ("juana@email.com",         "Juana", "Estudiante", "estudiante", "Escuela Demo"),
    ("pedro@email.com",         "Pedro", "Estudiante", "estudiante", "Escuela Demo"),
]


class Command(BaseCommand):
    help = "Crea usuarios demo (admin/director/estudiantes) y una escuela base."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Borra los usuarios demo antes de recrearlos.")

    @transaction.atomic
    def handle(self, *args, **opts):
        if opts.get("reset"):
            emails = [u[0] for u in USERS]
            n = Usuario.objects.filter(email__in=emails).delete()[0]
            self.stdout.write(self.style.WARNING(f"Borrados {n} usuarios previos."))

        # Escuela
        escuela, created = Escuela.objects.get_or_create(
            nombre="Escuela Demo",
            defaults={
                "direccion": "Av. Principal 123",
                "email": "contacto@escuelademo.cl",
                "telefono": "+56912345678",
                "basic_key": 10,
                "basic_access": True,
            },
        )
        self.stdout.write(self.style.SUCCESS(f'Escuela: {"creada" if created else "ya existia"} -> {escuela.nombre} (id={escuela.id})'))

        # Usuarios
        for email, nombre, apellido, role, escuela_nombre in USERS:
            if Usuario.objects.filter(email=email).exists():
                self.stdout.write(f"  - {email}: ya existe, saltando")
                continue

            kwargs = {
                "email": email,
                "nombre": nombre,
                "apellido": apellido,
                "password": DEFAULT_PASSWORD,
                "is_director": role == "director",
                "is_estudiante": role == "estudiante",
            }
            user = Usuario.objects.create_user(**kwargs)

            # Admin → flags
            if role == "admin":
                user.is_staff = True
                user.is_superuser = True

            # Activación inmediata (saltamos email)
            user.is_active = True
            user.activation_token = None

            # Asignar escuela
            if escuela_nombre:
                user.escuela = escuela

            user.save()
            self.stdout.write(self.style.SUCCESS(f"  OK {email} ({role}) creado con password '{DEFAULT_PASSWORD}'"))

        self.stdout.write(self.style.SUCCESS("\nSeed completo. Credenciales:"))
        for email, *_ in USERS:
            self.stdout.write(f"  {email} / {DEFAULT_PASSWORD}")
