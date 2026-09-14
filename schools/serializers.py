from rest_framework import serializers
from .models import Escuela, Curso, Leccion, Ejercicio, Glosario, Categoria, Unidad, Recurso, PlanCurso

class EscuelaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Escuela
        fields = '__all__'


class PlanCursoSerializer(serializers.ModelSerializer):
    """Plan de precio de un curso (CRUD admin + anidado en el curso)."""
    class Meta:
        model = PlanCurso
        fields = ['id', 'curso', 'dias', 'precio', 'precio_referencia', 'etiqueta', 'activo', 'orden']


class CursoSerializer(serializers.ModelSerializer):
    # Los cursos no pueden ser gratis: costo obligatorio y > 0 al crear/editar.
    # (min_value=1 rechaza 0 y negativos; required en create/PUT, validado si
    # viene en un PATCH parcial.)
    costo = serializers.IntegerField(min_value=1, required=True, allow_null=False)
    # Precio unitario (plan de 7 días activo) para el público en /explore.
    precio_unitario = serializers.SerializerMethodField()
    # Lista de planes activos (para el detalle /explore/[id]).
    planes = serializers.SerializerMethodField()

    class Meta:
        model = Curso
        fields = '__all__'

    def _planes_activos(self, obj):
        return sorted(
            [p for p in obj.planes.all() if p.activo],
            key=lambda p: p.dias,
        )

    def get_precio_unitario(self, obj):
        activos = self._planes_activos(obj)
        if activos:
            return int(activos[0].precio)  # menor duración = unitario (7 días)
        return int(obj.costo or 0)

    def get_planes(self, obj):
        return PlanCursoSerializer(self._planes_activos(obj), many=True).data

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
            'multiple',
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