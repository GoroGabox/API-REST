from rest_framework import serializers
from .models import Escuela, Curso, Leccion, Ejercicio, Glosario, Categoria, Unidad, Recurso

class EscuelaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Escuela
        fields = '__all__'

class CursoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Curso
        fields = '__all__'

class LeccionSerializer(serializers.ModelSerializer):
    """Listado: solo metadatos. Para detalle completo usar LeccionDetalleSerializer."""
    unidad_orden = serializers.IntegerField(source='unidad.orden', read_only=True)
    unidad_nombre = serializers.CharField(source='unidad.nombre', read_only=True)

    class Meta:
        model = Leccion
        fields = [
            'id', 'curso', 'unidad', 'unidad_orden', 'unidad_nombre',
            'categoria', 'nombre', 'posicion', 'tipo', 'descripcion', 'duracion_min',
            'url_video', 'url_audio', 'url_pdf',
        ]


class LeccionDetalleSerializer(serializers.ModelSerializer):
    """Detalle completo: incluye contenido + transcripción."""
    unidad_orden = serializers.IntegerField(source='unidad.orden', read_only=True)
    unidad_nombre = serializers.CharField(source='unidad.nombre', read_only=True)

    class Meta:
        model = Leccion
        fields = [
            'id', 'curso', 'unidad', 'unidad_orden', 'unidad_nombre',
            'categoria', 'nombre', 'posicion', 'tipo', 'descripcion', 'contenido', 'transcripcion',
            'duracion_min', 'url_video', 'url_audio', 'url_pdf',
        ]


class UnidadSerializer(serializers.ModelSerializer):
    lecciones = LeccionSerializer(many=True, read_only=True)

    class Meta:
        model = Unidad
        fields = ['id', 'curso', 'orden', 'nombre', 'descripcion', 'lecciones']


class RecursoSerializer(serializers.ModelSerializer):
    categoria_nombre = serializers.CharField(source='categoria.nombre', read_only=True)
    curso_nombre = serializers.CharField(source='curso.nombre', read_only=True)

    class Meta:
        model = Recurso
        fields = [
            'id', 'titulo', 'descripcion', 'categoria', 'categoria_nombre',
            'curso', 'curso_nombre', 'leccion', 'tipo', 'url',
            'paginas', 'size_bytes', 'requires_owned_course', 'created_at',
        ]

class EjercicioSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ejercicio
        fields = [
            'id', 'categoria', 'curso', 'leccion', 'pregunta', 'imagen',
            'opcion_a', 'opcion_b', 'opcion_c', 'opcion_d', 'opcion_e', 'opcion_f',
        ]


class EjercicioConRespuestaSerializer(serializers.ModelSerializer):
    """Solo para grading o vistas administrativas — incluye la respuesta correcta."""
    class Meta:
        model = Ejercicio
        fields = '__all__'

class GlosarioSerializer(serializers.ModelSerializer):
    class Meta:
        model = Glosario
        fields = '__all__'

class CategoriaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Categoria
        fields = '__all__'