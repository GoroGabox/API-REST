from rest_framework import serializers
from .models import (
    Escuela, Curso, Leccion, LeccionRecurso, Ejercicio, Glosario, Categoria, Unidad, Recurso, PlanCurso,
    PREGUNTAS_SESION_TEMATICA,
)

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
    # Nº de lecciones (anotado en CursoViewSet con Count('leccion')). Lo necesita
    # el panel del estudiante para el % de progreso; default 0 en usos anidados
    # sin anotación. read_only → no interfiere con create/update.
    cantidad_lecciones = serializers.IntegerField(read_only=True, default=0)

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


class LeccionRecursoSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeccionRecurso
        fields = ['id', 'tipo', 'rol', 'clave', 'url', 'orden', 'titulo', 'meta']


class LeccionDetalleSerializer(serializers.ModelSerializer):
    """Detalle completo: contenido + transcripción + recursos.

    ``recursos``: figuras, audio, video y PDFs (``LeccionRecurso``); las figuras se
    insertan en ``contenido`` con ``{{figura:<clave>}}``. ``imagenes`` y ``url_*`` se
    mantienen por compatibilidad con clientes previos (derivados de ``recursos``).
    """
    unidad_orden = serializers.IntegerField(source='unidad.orden', read_only=True)
    unidad_nombre = serializers.CharField(source='unidad.nombre', read_only=True)
    recursos = LeccionRecursoSerializer(many=True, read_only=True)
    imagenes = serializers.SerializerMethodField()

    def get_imagenes(self, obj):
        meta_int = lambda m, k: int(m.get(k) or 0)  # noqa: E731
        return [
            {
                'id': r.id, 'clave': r.clave, 'url': r.url, 'orden': r.orden,
                'pagina': (r.meta or {}).get('pagina'),
                'pie': (r.meta or {}).get('pie') or '', 'alt': (r.meta or {}).get('alt') or '',
                'ancho': meta_int(r.meta or {}, 'ancho'), 'alto': meta_int(r.meta or {}, 'alto'),
            }
            for r in obj.recursos.all() if r.tipo == 'imagen'
        ]

    class Meta:
        model = Leccion
        fields = [
            'id', 'curso', 'unidad', 'unidad_orden', 'unidad_nombre',
            'categoria', 'nombre', 'posicion', 'tipo', 'descripcion', 'contenido', 'transcripcion',
            'duracion_min', 'url_video', 'url_audio', 'url_pdf', 'recursos', 'imagenes',
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
    # ¿Tiene preguntas suficientes para una Sesión Temática? Solo se expone el
    # booleano (los clientes filtran con él; no muestran conteos).
    disponible_tematica = serializers.SerializerMethodField()

    class Meta:
        model = Categoria
        fields = '__all__'

    def get_disponible_tematica(self, obj):
        # `n_ejercicios` viene anotado por CategoriaViewSet; si no, se cuenta.
        n = getattr(obj, 'n_ejercicios', None)
        if n is None:
            n = obj.ejercicio_set.count()
        return n >= PREGUNTAS_SESION_TEMATICA