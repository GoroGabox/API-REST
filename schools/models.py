from django.db import models


# Create your models here.
class Escuela(models.Model):
    id = models.AutoField(primary_key=True, auto_created=True)
    nombre = models.CharField(max_length=100)
    direccion = models.CharField(max_length=200)
    email = models.EmailField()
    telefono = models.CharField(max_length=20)
    basic_key = models.IntegerField(default=0)
    professional_key = models.IntegerField(default=0)
    basic_access = models.BooleanField(default=False)
    professional_access = models.BooleanField(default=False)

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
    
class Leccion(models.Model):
    id = models.AutoField(primary_key=True, auto_created=True)
    curso = models.ForeignKey(Curso, on_delete=models.CASCADE)
    categoria = models.ForeignKey(Categoria, on_delete=models.SET_NULL, null=True)
    nombre = models.CharField(max_length=100)
    posicion = models.IntegerField()
    descripcion = models.CharField(max_length=255, null=True)
    url_video = models.URLField(default="http://placeholder.url")
    url_audio = models.URLField(default="http://placeholder.url")

    def __str__(self):
        return self.nombre

class Glosario(models.Model):
    id = models.AutoField(primary_key=True, auto_created=True)
    termino = models.CharField(max_length=100,null=True)
    significado = models.TextField(null=True)

    def __str__(self):
        return self.termino

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

    class Meta:
        ordering = ['id']

    def __str__(self):
        return self.pregunta

