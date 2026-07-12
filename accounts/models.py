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

    # Perfil enriquecido (consumido por GET /accounts/me/)
    rut = models.CharField(max_length=20, blank=True, default='')
    direccion = models.CharField(max_length=255, blank=True, default='')
    telefono = models.CharField(max_length=30, blank=True, default='')
    avatar_url = models.URLField(blank=True, default='')

    escuela = models.ForeignKey(Escuela, on_delete=models.SET_NULL, null=True, blank=True)

    # Preferencias de cuenta
    email_notifications = models.BooleanField(default=True)

    # Autenticación en dos pasos (TOTP). `totp_secret` guarda la clave base32
    # (nunca se expone por la API); `is_2fa_enabled` solo pasa a True tras que el
    # usuario verifica un código válido con su app autenticadora.
    is_2fa_enabled = models.BooleanField(default=False)
    totp_secret = models.CharField(max_length=64, blank=True, default='')
    # Códigos de recuperación 2FA: lista de hashes (SHA-256) de códigos de un
    # solo uso. Permiten iniciar sesión si se pierde la app autenticadora.
    # Nunca se exponen; el texto plano se muestra una única vez al generarlos.
    totp_recovery_codes = models.JSONField(default=list, blank=True)

    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)

    is_director = models.BooleanField(default=False)
    is_estudiante = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True, null=True)

    activation_token = models.UUIDField(default=uuid.uuid4, editable=False, null=True, blank=True, db_index=True)

    # Gamificación — recursos consumibles
    hearts = models.IntegerField(default=5)             # vidas; -1 por error en EVALUACIÓN
    next_heart_regen_at = models.DateTimeField(null=True, blank=True)

    # Gamificación — progresión
    xp = models.IntegerField(default=0)                 # acumulado total
    streak_current = models.IntegerField(default=0)     # días seguidos con actividad
    streak_longest = models.IntegerField(default=0)
    last_active_date = models.DateField(null=True, blank=True)
    streak_frozen_until = models.DateField(null=True, blank=True)

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

class PushToken(models.Model):
    """Token Expo/FCM/APNS registrado por un cliente para recibir push."""
    PLATFORM_CHOICES = [('ios', 'iOS'), ('android', 'Android'), ('web', 'Web'), ('expo', 'Expo')]
    usuario = models.ForeignKey(Usuario, on_delete=models.CASCADE, related_name='push_tokens')
    token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES, default='expo')
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_seen_at']

    def __str__(self):
        return f'{self.usuario.email}:{self.platform}'


class Notificacion(models.Model):
    TIPO_CHOICES = [
        ('streak_risk', 'Racha en riesgo'),
        ('new_lesson', 'Nueva lección'),
        ('certificate_issued', 'Certificado emitido'),
        ('test_passed', 'Prueba aprobada'),
        ('hearts_refilled', 'Vidas recuperadas'),
        ('promo', 'Promoción'),
        ('info', 'Informativa'),
    ]
    usuario = models.ForeignKey(Usuario, on_delete=models.CASCADE, related_name='notificaciones')
    tipo = models.CharField(max_length=30, choices=TIPO_CHOICES, default='info')
    titulo = models.CharField(max_length=200)
    mensaje = models.TextField(blank=True, default='')
    data = models.JSONField(default=dict, blank=True)  # payload arbitrario (ids, urls)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['usuario', '-created_at'])]

    def __str__(self):
        return f'[{self.tipo}] {self.titulo} -> {self.usuario.email}'


class Logro(models.Model):
    """Catálogo de logros (admin-curado). UsuarioLogro registra ganados."""
    slug = models.SlugField(unique=True)
    nombre = models.CharField(max_length=100)
    descripcion = models.CharField(max_length=255, blank=True, default='')
    icono = models.CharField(max_length=50, blank=True, default='')  # emoji o key

    class Meta:
        ordering = ['id']

    def __str__(self):
        return self.nombre


class UsuarioLogro(models.Model):
    usuario = models.ForeignKey(Usuario, on_delete=models.CASCADE, related_name='logros_obtenidos')
    logro = models.ForeignKey(Logro, on_delete=models.CASCADE)
    obtenido_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['usuario', 'logro'], name='unique_logro_usuario'),
        ]
        ordering = ['-obtenido_en']


class EstudianteLeccion(models.Model):
    """Registro de "lección vista" por un estudiante.

    La sola existencia del registro indica que el estudiante vio la lección.
    Es idempotente: solo el primer POST crea fila; reposts de la misma
    (estudiante, leccion) se rechazan a nivel DB por UniqueConstraint y
    se manejan idempotentemente en el viewset.
    """
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

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["estudiante", "leccion"],
                name="unique_estudianteleccion_estudiante_leccion",
            ),
        ]
        ordering = ["-updated_at", "-id"]
        indexes = [
            models.Index(fields=["estudiante", "curso"]),
        ]

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