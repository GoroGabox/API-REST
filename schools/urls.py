from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    EscuelaViewSet,
    CursoViewSet,
    LeccionViewSet,
    GlosarioViewSet,
    CategoriaViewSet,
    EjercicioViewSet,
    UnidadViewSet,
    RecursoViewSet,
    VincularEstudianteView,
    ProgresoEstudianteDetalleView,
    CertificadosPorEscuelaView,
)

router = DefaultRouter()
router.register(r'schools', EscuelaViewSet)
router.register(r'courses', CursoViewSet)
router.register(r'lessons', LeccionViewSet)
router.register(r'units', UnidadViewSet)
router.register(r'glosary', GlosarioViewSet)
router.register(r'exercices', EjercicioViewSet)
router.register(r'categories', CategoriaViewSet)
router.register(r'library', RecursoViewSet)

urlpatterns = [
    path(
        "vincular-estudiante/",
        VincularEstudianteView.as_view(),
        name="vincular_estudiante",
    ),
    path(
        "<int:school_id>/estudiantes/<int:user_id>/progreso/",
        ProgresoEstudianteDetalleView.as_view(),
        name="progreso_estudiante_detalle",
    ),
    path(
        "<int:school_id>/certificados/",
        CertificadosPorEscuelaView.as_view(),
        name="certificados_por_escuela",
    ),
    *router.urls,
]
