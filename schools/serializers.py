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
    # Precio unitario (plan de 7 días). WRITE: al crear/editar crea o actualiza
    # el PlanCurso de 7 días (fuente única de precio; los cursos no pueden ser
    # gratis → min_value=1, obligatorio al crear). READ: se inyecta en
    # to_representation desde el plan de 7 días.
    precio_unitario = serializers.IntegerField(min_value=1, required=False, write_only=True)
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

    def get_planes(self, obj):
        return PlanCursoSerializer(self._planes_activos(obj), many=True).data

    def to_representation(self, instance):
        data = super().to_representation(instance)
        activos = self._planes_activos(instance)
        data["precio_unitario"] = int(activos[0].precio) if activos else 0
        return data

    def create(self, validated_data):
        precio = validated_data.pop("precio_unitario", None)
        if not precio or int(precio) < 1:
            raise serializers.ValidationError(
                {"precio_unitario": "El precio unitario es obligatorio y debe ser mayor a 0."}
            )
        curso = super().create(validated_data)
        PlanCurso.objects.create(curso=curso, dias=7, precio=int(precio), activo=True, orden=7)
        return curso

    def update(self, instance, validated_data):
        precio = validated_data.pop("precio_unitario", None)
        curso = super().update(instance, validated_data)
        if precio is not None:
            PlanCurso.objects.update_or_create(
                curso=curso, dias=7,
                defaults={"precio": int(precio), "activo": True, "orden": 7},
            )
        return curso

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