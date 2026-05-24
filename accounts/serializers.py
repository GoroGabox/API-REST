from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth.models import Group, Permission
from .models import Usuario, DirectorProfile, EstudianteProfile, Certificado, Prueba, PruebaEjercicio, EstudianteLeccion  # noqa: F401

class UsuariorRegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])
    password2 = serializers.CharField(write_only=True)

    class Meta:
        model = Usuario
        # Registro público: solo datos de identidad. Roles y activación se gestionan
        # del lado del servidor (estudiante por defecto, activación por email).
        fields = [
            'nombre',
            'apellido',
            'email',
            'password',
            'password2',
            'escuela',
        ]

    def validate(self, data):
        if data['password'] != data['password2']:
            raise serializers.ValidationError({"password2": "Las contraseñas no coinciden."})
        return data

    def create(self, validated_data):
        validated_data.pop('password2')
        password = validated_data.pop('password')

        # Endpoint público = siempre estudiante, nunca director, nunca pre-activo.
        user = Usuario.objects.create_user(
            password=password,
            is_director=False,
            is_estudiante=True,
            **validated_data
        )
        user.is_active = False
        user.save(update_fields=['is_active'])
        return user

class UsuarioSerializer(serializers.ModelSerializer):
    class Meta:
        model = Usuario
        fields = [
            'id',
            'nombre',
            'apellido',
            'email',
            'escuela',
            'is_director',
            'is_estudiante',
            'is_superuser',
            'is_active',
            'is_staff'
        ]

class DirectorProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = DirectorProfile
        fields = '__all__'

class EstudianteProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = EstudianteProfile
        fields = '__all__'

class MyTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)

        # Add custom claims
        token['nombre'] = user.nombre
        token['apellido'] = user.apellido
        token['email'] = user.email
        token['escuela'] = user.escuela.id if user.escuela else None
        token['is_active'] = user.is_active
        token['is_director'] = user.is_director
        token['is_estudiante'] = user.is_estudiante
        token['is_superuser'] = user.is_superuser

        return token

class CertificadoSerializer(serializers.ModelSerializer):
    codigo = serializers.UUIDField(read_only=True)

    class Meta:
        model = Certificado
        fields = ['id', 'estudiante', 'curso', 'prueba', 'codigo', 'fecha_emision']


class CertificadoPublicoSerializer(serializers.ModelSerializer):
    """Serializer público para verificación (sin datos sensibles)."""
    estudiante_nombre = serializers.SerializerMethodField()
    curso_nombre = serializers.CharField(source='curso.nombre', read_only=True)

    class Meta:
        model = Certificado
        fields = ['codigo', 'estudiante_nombre', 'curso_nombre', 'fecha_emision']

    def get_estudiante_nombre(self, obj):
        return f"{obj.estudiante.nombre} {obj.estudiante.apellido}"


class PruebaDetalleSerializer(serializers.ModelSerializer):
    """Resumen para historial."""
    class Meta:
        model = Prueba
        fields = [
            'id', 'tipo', 'modalidad', 'curso', 'fecha', 'completada_en',
            'total_correctas', 'score', 'aprobado',
        ]


class SubmitPruebaSerializer(serializers.Serializer):
    respuestas = serializers.DictField(
        child=serializers.CharField(max_length=2, allow_blank=True),
        help_text="Mapa pregunta_id -> opcion_key ('a'..'f')",
    )

class PruebaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Prueba
        fields = '__all__'

class PruebaEjercicioSerializer(serializers.ModelSerializer):
    class Meta:
        model = PruebaEjercicio
        fields = '__all__'
    
class SendEmailSerializer(serializers.ModelSerializer):
    class Meta:
        model = Usuario
        fields = ['email']

class ResetPasswordSerializer(serializers.Serializer):
    uid = serializers.CharField(required=True)
    token = serializers.CharField(required=True)
    new_password = serializers.CharField(write_only=True, required=True)
    confirm_password = serializers.CharField(write_only=True, required=True)

    def validate(self, data):
        new_password = data.get('new_password')
        confirm_password = data.get('confirm_password')

        if new_password != confirm_password:
            raise serializers.ValidationError({"confirm_password": "Las contraseñas no coinciden."})

        user = self.context.get('user')
        try:
            validate_password(new_password, user=user)
        except ValidationError as e:
            raise serializers.ValidationError({"new_password": e.messages})
        
        return data
    
class GroupSerializer(serializers.ModelSerializer):
    permissions = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Permission.objects.all()
    )

    class Meta:
        model = Group
        fields = ['id', 'name', 'permissions']

class PermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Permission
        fields = ['id', 'name', 'codename', 'content_type']

class EstudianteLeccionSerializer(serializers.ModelSerializer):
    class Meta:
        model = EstudianteLeccion
        fields = '__all__'


class EscuelaConDirectorSerializer(serializers.Serializer):
    """Crea Escuela + Director en un solo flujo atómico (solo admin)."""
    # Datos de la escuela
    escuela_nombre = serializers.CharField(max_length=100)
    escuela_direccion = serializers.CharField(max_length=200)
    escuela_email = serializers.EmailField()
    escuela_telefono = serializers.CharField(max_length=20)
    # Datos del director
    director_nombre = serializers.CharField(max_length=100)
    director_apellido = serializers.CharField(max_length=100)
    director_email = serializers.EmailField()
    director_password = serializers.CharField(
        write_only=True, validators=[validate_password]
    )

    def validate_director_email(self, value):
        if Usuario.objects.filter(email=value).exists():
            raise serializers.ValidationError("Ya existe un usuario con ese email.")
        return value

    def create(self, validated_data):
        from schools.models import Escuela
        from django.db import transaction as db_transaction

        with db_transaction.atomic():
            escuela = Escuela.objects.create(
                nombre=validated_data['escuela_nombre'],
                direccion=validated_data['escuela_direccion'],
                email=validated_data['escuela_email'],
                telefono=validated_data['escuela_telefono'],
            )
            director = Usuario.objects.create_user(
                email=validated_data['director_email'],
                nombre=validated_data['director_nombre'],
                apellido=validated_data['director_apellido'],
                password=validated_data['director_password'],
                is_director=True,
                is_estudiante=False,
            )
            director.is_active = True  # admin lo crea activo, sin email-flow
            director.escuela = escuela
            director.save(update_fields=['is_active', 'escuela'])
            return {'escuela': escuela, 'director': director}

    def to_representation(self, instance):
        escuela = instance['escuela']
        director = instance['director']
        return {
            'escuela': {
                'id': escuela.id,
                'nombre': escuela.nombre,
                'email': escuela.email,
            },
            'director': {
                'id': director.id,
                'email': director.email,
                'nombre': director.nombre,
                'apellido': director.apellido,
            },
        }