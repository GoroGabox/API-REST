from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend

from accounts.permissions import ReadOnlyOrAdmin, is_admin, is_director, is_estudiante
from .models import Escuela, Curso, Leccion, Ejercicio, Glosario, Categoria, Unidad, Recurso
from .serializers import (
    EscuelaSerializer,
    CursoSerializer,
    LeccionSerializer,
    LeccionDetalleSerializer,
    EjercicioSerializer,
    GlosarioSerializer,
    CategoriaSerializer,
    UnidadSerializer,
    RecursoSerializer,
)


class EscuelaViewSet(viewsets.ModelViewSet):
    """Admin: full CRUD sobre todas las escuelas.

    Director: solo puede VER su propia escuela. Estudiante: no accede aquí.
    """
    queryset = Escuela.objects.all()
    serializer_class = EscuelaSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['email', 'basic_access', 'professional_access']
    permission_classes = [ReadOnlyOrAdmin]

    def get_queryset(self):
        from accounts.permissions import is_admin, is_director
        user = self.request.user
        if is_admin(user):
            return Escuela.objects.all()
        if is_director(user) and user.escuela_id:
            return Escuela.objects.filter(pk=user.escuela_id)
        return Escuela.objects.none()


class CursoViewSet(viewsets.ModelViewSet):
    queryset = Curso.objects.all()
    serializer_class = CursoSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['costo']
    permission_classes = [ReadOnlyOrAdmin]

    @action(detail=True, methods=['get'], url_path='units')
    def units(self, request, pk=None):
        """GET /api/v1/schools/courses/<id>/units/ — unidades del curso con sus lecciones."""
        curso = self.get_object()
        unidades = (
            Unidad.objects.filter(curso=curso)
            .prefetch_related('lecciones')
            .order_by('orden', 'id')
        )
        return Response(UnidadSerializer(unidades, many=True).data)


class LeccionViewSet(viewsets.ModelViewSet):
    queryset = Leccion.objects.all()
    serializer_class = LeccionSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['curso', 'unidad', 'posicion', 'tipo']
    permission_classes = [ReadOnlyOrAdmin]

    def get_serializer_class(self):
        # Detalle (retrieve) trae contenido completo + transcripción.
        if self.action == 'retrieve':
            return LeccionDetalleSerializer
        return LeccionSerializer


class UnidadViewSet(viewsets.ModelViewSet):
    queryset = Unidad.objects.all()
    serializer_class = UnidadSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['curso']
    permission_classes = [ReadOnlyOrAdmin]


class EjercicioViewSet(viewsets.ModelViewSet):
    queryset = Ejercicio.objects.all()
    serializer_class = EjercicioSerializer
    permission_classes = [ReadOnlyOrAdmin]


class GlosarioViewSet(viewsets.ModelViewSet):
    queryset = Glosario.objects.all()
    serializer_class = GlosarioSerializer
    permission_classes = [ReadOnlyOrAdmin]


class CategoriaViewSet(viewsets.ModelViewSet):
    queryset = Categoria.objects.all()
    serializer_class = CategoriaSerializer
    permission_classes = [ReadOnlyOrAdmin]


class RecursoViewSet(viewsets.ModelViewSet):
    """Biblioteca: GET /api/v1/schools/library/?categoria=&curso=&tipo=&q=

    - Admin/director: ven todo (director ve catálogo global; restricción de
      lectura por escuela no aplica al catálogo).
    - Estudiante: ve recursos donde `requires_owned_course=False` o donde el
      `curso` está en sus inscripciones (EstudianteCurso).
    """
    queryset = Recurso.objects.select_related('categoria', 'curso', 'leccion')
    serializer_class = RecursoSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['categoria', 'curso', 'leccion', 'tipo', 'requires_owned_course']
    permission_classes = [ReadOnlyOrAdmin]

    def get_queryset(self):
        qs = super().get_queryset()
        # Búsqueda por título.
        q = self.request.query_params.get('q')
        if q:
            from django.db.models import Q
            qs = qs.filter(Q(titulo__icontains=q) | Q(descripcion__icontains=q))

        user = self.request.user
        if is_admin(user) or is_director(user):
            return qs

        # Estudiante: ocultar recursos con requires_owned_course=True
        # excepto los de cursos que posee.
        if is_estudiante(user):
            from sales.models import EstudianteCurso
            owned_cursos = EstudianteCurso.objects.filter(
                estudiante_id=user
            ).values_list('curso_id_id', flat=True)
            from django.db.models import Q
            return qs.filter(
                Q(requires_owned_course=False) | Q(curso_id__in=owned_cursos)
            )

        return qs.none()
