import secrets
import string

from django.db import models


# Alfabeto sin caracteres ambiguos (0/O, 1/I) para códigos legibles/dictables.
_CODIGO_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generar_codigo_escuela(length: int = 6) -> str:
    """Genera un código de escuela único (mayúsculas, sin ambigüedad).

    El estudiante lo ingresa para pedir acceso, así que prioriza que sea corto
    y fácil de dictar. Reintenta ante colisiones (rarísimas con 32^6).
    """
    while True:
        codigo = "".join(secrets.choice(_CODIGO_ALPHABET) for _ in range(length))
        if not Escuela.objects.filter(codigo=codigo).exists():
            return codigo


# Create your models here.
class Escuela(models.Model):
    """Modelo de licencia mixto (un solo tipo de llave y de suscripcion):

    - **Llaves** (`basic_key`): acceso TEMPORAL. Cada activacion descuenta 1
      llave y crea una AccessKey con expiracion (7 dias por defecto).
    - **Suscripcion** (`basic_access`): acceso acotado por cupos
      (`basic_seats_max`) y por la vigencia de la suscripcion
      (`basic_access_until`, null = sin vencimiento). Cada activacion descuenta
      1 seat; liberar un estudiante restituye el seat. No consume llaves.
    """
    id = models.AutoField(primary_key=True, auto_created=True)
    nombre = models.CharField(max_length=100)
    direccion = models.CharField(max_length=200)
    email = models.EmailField()
    telefono = models.CharField(max_length=20)

    # Código único que el estudiante ingresa para pedir acceso a esta escuela.
    codigo = models.CharField(max_length=12, unique=True, null=True, blank=True, db_index=True)

    # Llaves (unidades reservables con expiracion)
    basic_key = models.IntegerField(default=0)

    # Suscripcion (acceso ilimitado en tiempo con tope de cupos)
    basic_access = models.BooleanField(default=False)
    basic_seats_max = models.IntegerField(default=0)
    basic_seats_used = models.IntegerField(default=0)
    # Fin de la suscripción (null = sin vencimiento). Los cupos asignados vencen
    # con ella; renovar la extiende.
    basic_access_until = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        # Toda escuela nace con un código; se genera una sola vez y no cambia.
        if not self.codigo:
            self.codigo = generar_codigo_escuela()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.nombre

class Curso(models.Model):
    id = models.AutoField(primary_key=True, auto_created=True)
    nombre = models.CharField(max_length=100)
    codigo = models.CharField(max_length=10, null=True)
    descripcion = models.TextField()
    # El precio vive en PlanCurso (fuente única): el plan de 7 días es el valor
    # unitario. `Curso` ya no guarda `costo`.
    url_image = models.URLField(null=True, default="http://placeholder.url")
    url_icon = models.URLField(null=True, default="http://placeholder.url")
    is_profesional = models.BooleanField(default=False)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return self.nombre


class PlanCurso(models.Model):
    """Precio B2C de un curso por plan/duración (listado de valores por curso).

    Cada llave habilita 7 días; los planes son múltiplos de 7 (7/14/35). El
    precio es INDEPENDIENTE por plan (no un múltiplo fijo del unitario), lo que
    permite descuentos por duración. `precio_referencia` es el precio "antes"
    (tachado) para mostrar el ahorro. El plan de 7 días es el valor unitario que
    se muestra en /explore (fuente única de precio del curso).
    """
    DIAS_CHOICES = [(7, "7 días"), (14, "14 días"), (35, "35 días (1 mes + 5 de regalo)")]

    curso = models.ForeignKey(Curso, on_delete=models.CASCADE, related_name="planes")
    dias = models.IntegerField(choices=DIAS_CHOICES)
    precio = models.IntegerField()  # CLP, precio final del plan
    precio_referencia = models.IntegerField(null=True, blank=True)  # "antes" (tachado)
    etiqueta = models.CharField(max_length=60, blank=True, default="")
    activo = models.BooleanField(default=True)
    orden = models.IntegerField(default=0)

    class Meta:
        unique_together = ("curso", "dias")
        ordering = ["curso", "dias"]

    def __str__(self):
        return f"{self.curso.nombre} · {self.dias}d · ${self.precio}"


# Preguntas que usa una Sesión Temática del Gimnasio. Una categoría solo se
# ofrece para esa sesión si tiene al menos esta cantidad de ejercicios.
# Fuente única: `accounts.services.SIZES['categoria']` la reutiliza.
PREGUNTAS_SESION_TEMATICA = 15


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

class LeccionRecurso(models.Model):
    """Todo lo que acompaña al texto de una lección: figuras, audio, video, PDFs.

    El texto vive en ``Leccion.contenido`` (Markdown). Las figuras se insertan en él
    con marcadores ``{{figura:<clave>}}`` (los pone el redactor del pipeline); el
    cliente reemplaza cada marcador por el recurso con esa ``clave``. Los recursos no
    referenciados se muestran aparte (galería, reproductor, descargas) según ``rol``.

    ``meta`` (JSON) guarda lo específico del tipo: ``pie``, ``alt``, ``pagina``,
    ``paginas``, ``ancho``, ``alto``, ``duracion_seg``, ``origen``, ``mapeo``, ``hash``…
    Los campos ``Leccion.url_audio/url_video/url_pdf`` son un ESPEJO de compatibilidad
    que se rellena desde aquí (``schools.services.sync_media_mirror``).
    """
    TIPO_CHOICES = [
        ('imagen', 'Imagen'),
        ('audio', 'Audio'),
        ('video', 'Video'),
        ('pdf', 'PDF'),
    ]
    ROL_CHOICES = [
        ('figura', 'Figura del libro'),
        ('narracion', 'Narración (audio de la lectura)'),
        ('principal', 'Medio principal'),
        ('paginas_libro', 'Páginas del libro'),
        ('descarga', 'Descarga'),
    ]

    leccion = models.ForeignKey(Leccion, on_delete=models.CASCADE, related_name="recursos")
    tipo = models.CharField(max_length=10, choices=TIPO_CHOICES)
    rol = models.CharField(max_length=20, choices=ROL_CHOICES, default='figura')
    clave = models.CharField(max_length=64)          # id del marcador ({{figura:<clave>}}) o 'audio', 'paginas'…
    url = models.URLField(max_length=500)
    orden = models.IntegerField(default=0)
    titulo = models.CharField(max_length=255, blank=True, default='')
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["orden", "id"]
        constraints = [
            models.UniqueConstraint(fields=["leccion", "clave"], name="uniq_recurso_leccion_clave"),
        ]

    def __str__(self):
        return f"{self.leccion_id} · {self.tipo}/{self.rol} · {self.clave}"


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
    leccion = models.ForeignKey('Leccion', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='recursos_biblioteca')  # 'recursos' = LeccionRecurso
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
    # Selección múltiple: `multiple=True` indica que hay varias opciones
    # correctas (el cliente renderiza checkboxes). `respuestas_correctas` es la
    # fuente de verdad para esos casos — lista de keys, p.ej. ["c", "e"]. Para
    # preguntas de respuesta única se sigue usando `respuesta` (texto de la
    # opción correcta); así no se rompe el catálogo ni el importador existentes.
    multiple = models.BooleanField(default=False)
    respuestas_correctas = models.JSONField(default=list, blank=True)
    explicacion = models.TextField(blank=True, default="")

    class Meta:
        ordering = ['id']

    def __str__(self):
        return self.pregunta


