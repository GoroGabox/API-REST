from rest_framework import serializers
from .models import Producto, Venta, AccessKey, EstudianteCurso, SolicitudAcceso
from accounts.serializers import UsuarioSerializer
from schools.serializers import CursoSerializer
from django.utils import timezone
from accounts.models import Usuario
from schools.models import Curso


class ProductoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Producto
        fields = '__all__'

class VentaSerializer(serializers.ModelSerializer):
    producto = serializers.StringRelatedField()
    usuario = serializers.StringRelatedField()

    class Meta:
        model = Venta
        fields = ['id', 'producto', 'usuario', 'fecha_venta', 'monto_pagado', 'pay_system', 'payment_status']

class AccessKeySerializer(serializers.ModelSerializer):
    class Meta:
        model = AccessKey
        fields = '__all__'


class EstudianteCursoSerializer(serializers.ModelSerializer):
    # Estado del acceso para la UI "Mis cursos": permite distinguir un curso
    # con acceso vigente de uno vencido (y ofrecer "Renovar" si es compra).
    acceso_vigente = serializers.SerializerMethodField()
    valid_until = serializers.SerializerMethodField()
    origen = serializers.SerializerMethodField()

    class Meta:
        model = EstudianteCurso
        fields = [
            'id', 'estudiante_id', 'curso_id', 'access_key_id',
            'acceso_vigente', 'valid_until', 'origen',
        ]

    def get_acceso_vigente(self, obj):
        ak = obj.access_key_id
        return bool(ak and ak.is_valid())

    def get_valid_until(self, obj):
        ak = obj.access_key_id
        return ak.valid_until if ak else None

    def get_origen(self, obj):
        ak = obj.access_key_id
        return ak.origen if ak else None

class EstudianteCursoDetailSerializer(serializers.ModelSerializer):
    estudiante_id = UsuarioSerializer()
    curso_id = CursoSerializer()
    access_key = AccessKeySerializer(source='access_key_id', read_only=True)

    class Meta:
        model = EstudianteCurso
        fields = ['id', 'estudiante_id', 'curso_id', 'access_key']


class ActivarCursoSerializer(serializers.ModelSerializer):
    days = serializers.IntegerField(default=30)
    class Meta:
        model = EstudianteCurso
        fields = ['curso_id', 'estudiante_id','days']


class SolicitudAccesoSerializer(serializers.ModelSerializer):
    # Campos denormalizados para que ambas UIs (estudiante y director) rendericen
    # sin joins extra.
    curso_nombre = serializers.CharField(source='curso.nombre', read_only=True)
    curso_codigo = serializers.CharField(source='curso.codigo', read_only=True)
    curso_is_profesional = serializers.BooleanField(source='curso.is_profesional', read_only=True)
    escuela_nombre = serializers.CharField(source='escuela.nombre', read_only=True)
    estudiante_nombre = serializers.SerializerMethodField()
    estudiante_email = serializers.CharField(source='estudiante.email', read_only=True)

    class Meta:
        model = SolicitudAcceso
        fields = [
            'id', 'estudiante', 'estudiante_nombre', 'estudiante_email',
            'escuela', 'escuela_nombre', 'curso', 'curso_nombre', 'curso_codigo',
            'curso_is_profesional', 'estado', 'mensaje', 'motivo_rechazo',
            'created_at', 'resolved_at',
        ]
        read_only_fields = fields

    def get_estudiante_nombre(self, obj):
        est = obj.estudiante
        return f"{est.nombre} {est.apellido}".strip() if est else ''

class EstudianteCursosActivosSerializer(serializers.ModelSerializer):
    registros = serializers.SerializerMethodField()

    class Meta:
        model = Usuario
        fields = [
            'id',
            'nombre',
            'apellido',
            'email',
            'registros',  # lista de EstudianteCursoDetailSerializer
        ]

    def get_registros(self, obj):
        # Si el caller pre-cargó vía Prefetch(to_attr='_prefetched_registros'),
        # úsalo para evitar N+1. Fallback a query si no hubo prefetch.
        registros = getattr(obj, '_prefetched_registros', None)
        if registros is None:
            registros = (
                EstudianteCurso.objects
                .select_related('estudiante_id', 'curso_id', 'access_key_id')
                .filter(estudiante_id=obj)
            )
        return EstudianteCursoDetailSerializer(registros, many=True).data
    
class CursoDisponibleSerializer(serializers.ModelSerializer):
    user_can_access = serializers.SerializerMethodField()
    already_owned = serializers.SerializerMethodField()
    # Anotado en la vista con Count('leccion'); default 0 por robustez.
    cantidad_lecciones = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Curso
        fields = [
            "id",
            "nombre",
            "descripcion",
            "url_image",
            "url_icon",
            "costo",
            "is_profesional",
            "cantidad_lecciones",
            "user_can_access",
            "already_owned",
        ]

    def get_already_owned(self, obj):
        owned_ids = self.context.get("owned_ids", set())
        return obj.id in owned_ids

    def get_user_can_access(self, obj):
        escuela = self.context.get("escuela")
        already_owned = self.get_already_owned(obj)

        # Si ya es suyo, puede acceder
        if already_owned:
            return True

        if escuela is None:
            return False

        # Reglas de acceso de la escuela
        # - basic_access → solo cursos no profesionales
        # - professional_access → acceso a todos
        if not obj.is_profesional and escuela.basic_access:
            return True

        if escuela.professional_access:
            return True

        return False
