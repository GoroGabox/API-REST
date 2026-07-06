from django.db import models


# Create your models here.
class Escuela(models.Model):
    """Modelo de licencia mixto:

    - **Llaves** (`basic_key`, `professional_key`): acceso TEMPORAL. Cada
      activacion descuenta 1 llave y crea una AccessKey con expiracion.
    - **Suscripcion** (`basic_access`, `professional_access`): acceso
      ILIMITADO en tiempo, pero acotado por cupos (`basic_seats_max`,
      `professional_seats_max`). Cada activacion descuenta 1 seat; liberar
      un estudiante restituye el seat. No consume llaves.
    """
    id = models.AutoField(primary_key=True, auto_created=True)
    nombre = models.CharField(max_length=100)
    direccion = models.CharField(max_length=200)
    email = models.EmailField()
    telefono = models.CharField(max_length=20)

    # Llaves (unidades reservables con expiracion)
    basic_key = models.IntegerField(default=0)
    professional_key = models.IntegerField(default=0)

    # Suscripciones (acceso ilimitado en tiempo con tope de cupos)
    basic_access = models.BooleanField(default=False)
    professional_access = models.BooleanField(default=False)
    basic_seats_max = models.IntegerField(default=0)
    basic_seats_used = models.IntegerField(default=0)
    professional_seats_max = models.IntegerField(default=0)
    professional_seats_used = models.IntegerField(default=0)

    def __str__(self):
        return self.nombre

class Curso(models.Model):
    id = models.AutoField(primary_key=True, auto_created=True)
    nombre = models.CharField(max_length=100)
    codigo = models.CharField(max_length=10, null=True)
    descripcion = models.TextField()
    costo = models.IntegerField(null = True)
    url_image = models.URLField(null=True, default="http://placeholder.url")
    url_icon = models.URLField(null=True, default="http://placeholder.url")
    is_profesional = models.BooleanField(default=False)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return self.nombre

class Categoria(models.Model):
    id = models.AutoField(primary_key=True, auto_created=True)
    nombre = models.CharField(max_length=100)
    color_hex = models.CharField(max_length=10, default="#545050")
    def __str__(self):
        return self.nombre


class Unidad(models.Model):
    """Agrupacion de Lecciones dentro de un Curso (ej: 'Unidad 2 - Senales')."""
    curso = models.ForeignKey(Curso, on_delete=models.CASCADE, related_name='unidades')
    nombre = models.CharField(max_length=100)
    orden = models.IntegerField(default=0)
    descripcion = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        ordering = ['curso', 'orden', 'id']
        constraints = [
            models.UniqueConstraint(fields=['curso', 'orden'], name='unique_unidad_orden_por_curso'),
        ]

    def __str__(self):
        return f"{self.curso.nombre} / U{self.orden} - {self.nombre}"


class Leccion(models.Model):
    TIPO_CHOICES = [
        ('texto', 'Texto / Lectura'),
        ('video', 'Video'),
        ('audio', 'Audio'),
        ('quiz', 'Quiz'),
        ('drag', 'Drag & Drop'),
        ('identify', 'Identificar'),
    ]

    id = models.AutoField(primary_key=True, auto_created=True)
    curso = models.ForeignKey(Curso, on_delete=models.CASCADE)
    unidad = models.ForeignKey(Unidad, on_delete=models.SET_NULL, null=True, blank=True, related_name='lecciones')
    categoria = models.ForeignKey(Categoria, on_delete=models.SET_NULL, null=True)
    nombre = models.CharField(max_length=100)
    posicion = models.IntegerField()
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default='video')
    descripcion = models.CharField(max_length=255, null=True)
    contenido = models.TextField(blank=True, default='')          # markdown/HTML para detalle
    transcripcion = models.TextField(blank=True, default='')
    duracion_min = models.IntegerField(default=0)                  # minutos estimados
    url_video = models.URLField(default="http://placeholder.url")
    url_audio = models.URLField(default="http://placeholder.url")
    url_pdf = models.URLField(blank=True, default='')

    class Meta:
        ordering = ['curso', 'posicion', 'id']

    def __str__(self):
        return self.nombre


class LeccionFuente(models.Model):
    leccion = models.ForeignKey(Leccion, on_delete=models.CASCADE, related_name="fuentes")
    fuente_nombre = models.CharField(max_length=255)
    pagina_inicio = models.IntegerField()
    pagina_fin = models.IntegerField()
    tema_regulatorio = models.CharField(max_length=255, blank=True, default='')
    fragmento_resumen = models.TextField(blank=True, default='')
    hash_fragmento = models.CharField(max_length=64, blank=True, default='')

    class Meta:
        ordering = ['leccion', 'pagina_inicio']

    def __str__(self):
        return f"{self.leccion.nombre} / {self.fuente_nombre} pp. {self.pagina_inicio}-{self.pagina_fin}"

class Glosario(models.Model):
    id = models.AutoField(primary_key=True, auto_created=True)
    termino = models.CharField(max_length=100,null=True)
    significado = models.TextField(null=True)

    def __str__(self):
        return self.termino

class Recurso(models.Model):
    """Recursos descargables de la biblioteca (PDFs principalmente).

    `requires_owned_course=True` restringe la visibilidad al estudiante que
    posee acceso al curso vinculado.
    """
    TIPO_CHOICES = [
        ('pdf', 'PDF'),
        ('video', 'Video'),
        ('audio', 'Audio'),
        ('link', 'Enlace externo'),
    ]
    titulo = models.CharField(max_length=200)
    descripcion = models.CharField(max_length=500, blank=True, default='')
    categoria = models.ForeignKey(Categoria, on_delete=models.SET_NULL, null=True, blank=True)
    curso = models.ForeignKey(Curso, on_delete=models.SET_NULL, null=True, blank=True, related_name='recursos')
    leccion = models.ForeignKey('Leccion', on_delete=models.SET_NULL, null=True, blank=True, related_name='recursos')
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default='pdf')
    url = models.URLField()
    paginas = models.IntegerField(null=True, blank=True)
    size_bytes = models.BigIntegerField(null=True, blank=True)
    requires_owned_course = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def __str__(self):
        return self.titulo


class Ejercicio(models.Model):
    id = models.AutoField(primary_key=True, auto_created=True)
    categoria = models.ForeignKey(Categoria, on_delete=models.SET_NULL, null=True)
    curso = models.ForeignKey(Curso, on_delete=models.SET_NULL, null=True)
    leccion = models.ForeignKey(Leccion, on_delete=models.SET_NULL, null=True)
    pregunta = models.TextField(null=True, blank=True, default="Pregunta", max_length=510)
    imagen = models.URLField(default="http://placeholder.url")
    opcion_a = models.CharField(max_length=255, null=True, blank=True)
    opcion_b = models.CharField(max_length=255, null=True, blank=True)
    opcion_c = models.CharField(max_length=255, null=True, blank=True)
    opcion_d = models.CharField(max_length=255, null=True, blank=True)
    opcion_e = models.CharField(max_length=255, null=True, blank=True)
    opcion_f = models.CharField(max_length=255, null=True, blank=True)
    respuesta = models.CharField(max_length=255, null=True)
    explicacion = models.TextField(blank=True, default="")

    class Meta:
        ordering = ['id']

    def __str__(self):
        return self.pregunta


