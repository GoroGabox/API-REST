from rest_framework import serializers
from .models import Producto, Venta, AccessKey, EstudianteCurso
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
    class Meta:
        model = EstudianteCurso
        fields = '__all__'

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
