import uuid

from django.db import models
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import Group
from schools.models import Escuela, Curso, Ejercicio, Leccion


class UserManager(BaseUserManager):
    def create_user(self, email, nombre, apellido, password=None, is_director=False, is_estudiante=False, **other_fields):
        if not email:
            raise ValueError('El email es requerido')
        if not password:
            raise ValueError('La constraseña es requerida')

        email = self.normalize_email(email)
        user = self.model(
            email=email,
            nombre=nombre,
            apellido=apellido,
            is_director=is_director,  # Asignar explícitamente
            is_estudiante=is_estudiante,  # Asignar explícitamente
            **other_fields
        )
        user.set_password(password)
        user.save()

        if is_director:
            director_group, _ = Group.objects.get_or_create(name='Directores')
            user.groups.add(director_group)

        if is_estudiante:
            estudiante_group, _ = Group.objects.get_or_create(name='Estudiantes')
            user.groups.add(estudiante_group)

        return user

    def create_superuser(self, email, nombre , apellido, password=None, **other_fields):
        other_fields.setdefault('is_staff', True)
        other_fields.setdefault('is_active', True)
        other_fields.setdefault('is_superuser', True)

        if other_fields.get('is_staff') is not True:
            raise ValueError('Superuser debe tener asignado is_staff=True')
        if other_fields.get('is_active') is not True:
            raise ValueError('Superuser debe tener asignado is_active=True')
        
        user = self.create_user(email, nombre , apellido, password, **other_fields)
        user.save()
        return user

class Usuario(AbstractBaseUser, PermissionsMixin):
    # id = models.AutoField(primary_key=True)
    password = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    nombre = models.CharField(max_length=100)
    apellido = models.CharField(max_length=100)

    escuela = models.ForeignKey(Escuela, on_delete=models.SET_NULL, null=True, blank=True)

    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)

    is_director = models.BooleanField(default=False)
    is_estudiante = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True, null=True)

    activation_token = models.UUIDField(default=uuid.uuid4, editable=False, null=True, blank=True, db_index=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['nombre' ,'apellido']

    objects = UserManager()

    class Meta:
        ordering = ['id']

    def __str__(self):
        return self.nombre + ' ' + self.apellido
    
class DirectorProfile(models.Model):
    id = models.AutoField(primary_key=True, auto_created=True)
    usuario = models.OneToOneField(Usuario, on_delete = models.CASCADE)

class EstudianteProfile(models.Model):
    id = models.AutoField(primary_key=True, auto_created=True)
    usuario = models.OneToOneField(Usuario, on_delete = models.CASCADE)

class Certificado(models.Model):
    id = models.AutoField(primary_key=True, auto_created=True)
    estudiante = models.ForeignKey(Usuario, on_delete=models.CASCADE)
    curso = models.ForeignKey(Curso, on_delete=models.SET_NULL, null=True)
    prueba = models.ForeignKey('Prueba', on_delete=models.SET_NULL, null=True, blank=True, related_name='certificados')
    codigo = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    fecha_emision = models.DateField(auto_now_add=True)

    class Meta:
        # Un solo certificado por (estudiante, curso). Re-aprobar no duplica.
        constraints = [
            models.UniqueConstraint(
                fields=['estudiante', 'curso'],
                name='unique_certificado_por_estudiante_curso',
            ),
        ]

class EstudianteLeccion(models.Model):
    estudiante = models.ForeignKey(Usuario, on_delete=models.CASCADE)
    leccion = models.ForeignKey(Leccion, on_delete=models.CASCADE)
    curso = models.ForeignKey(
        Curso,
        on_delete=models.CASCADE,
        related_name="progresos_lecciones",
        null=True,
        blank=True,
    )

    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.estudiante} - {self.leccion}"

class Prueba(models.Model):
    TIPO_CHOICES = [
        ('completa', 'Completa'),
        ('rapida', 'Rápida'),
        ('categoria', 'Por categoría'),
    ]
    MODALIDAD_CHOICES = [
        # práctica: para entrenar; consume "energía". No otorga certificado.
        ('practica', 'Práctica'),
        # evaluación: gating de unidad o examen final del curso; consume
        # "corazones" al fallar. Si tipo='completa' y aprobado → certificado.
        ('evaluacion', 'Evaluación'),
    ]

    id = models.AutoField(primary_key=True, auto_created=True)
    estudiante = models.ForeignKey(Usuario, on_delete=models.CASCADE)
    curso = models.ForeignKey(Curso, on_delete=models.SET_NULL, null=True, blank=True, related_name='pruebas')

    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default='rapida')
    modalidad = models.CharField(max_length=20, choices=MODALIDAD_CHOICES, default='practica')

    fecha = models.DateField(auto_now_add=True)
    completada_en = models.DateTimeField(null=True, blank=True)

    total_correctas = models.IntegerField(default=0)
    score = models.DecimalField(max_digits=5, decimal_places=2, default=0)  # 0..100
    aprobado = models.BooleanField(default=False)

    class Meta:
        ordering = ['-fecha', '-id']

    def __str__(self):
        return f'#{self.id} {self.estudiante}'


class PruebaEjercicio(models.Model):
    id = models.AutoField(primary_key=True, auto_created=True)
    prueba = models.ForeignKey(Prueba, on_delete=models.CASCADE, related_name='items')
    ejercicio = models.ForeignKey(Ejercicio, on_delete=models.CASCADE)
    respuesta_estudiante = models.CharField(max_length=200, blank=True, default='')
    correcta = models.BooleanField(null=True, blank=True)  # null = aún sin responder

    def __str__(self):
        return f'#{self.prueba_id}${self.ejercicio_id}'