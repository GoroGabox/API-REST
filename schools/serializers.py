from rest_framework import serializers
from .models import Escuela, Curso, Leccion, Ejercicio, Glosario, Categoria

class EscuelaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Escuela
        fields = '__all__'

class CursoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Curso
        fields = '__all__'

class LeccionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Leccion
        fields = '__all__'

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