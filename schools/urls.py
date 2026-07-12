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
    DesvincularEstudianteView,
    ProgresoEstudianteDetalleView,
    CertificadosPorEscuelaView,
    CourseGenerateView,
)
# Vistas de suscripción viven en `sales/views.py` porque manipulan
# EstudianteCurso/AccessKey, pero se exponen bajo el árbol de schools para
# consistencia con el spec del frontend.
from sales.views import SubscriptionStatusView, SubscriptionSeatsView

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
    # Debe ir ANTES de `*router.urls`: si no, el router captura
    # `courses/generate/` como detalle `courses/<pk=generate>/`.
    path(
        "courses/generate/",
        CourseGenerateView.as_view(),
        name="course_generate",
    ),
    path(
        "vincular-estudiante/",
        VincularEstudianteView.as_view(),
        name="vincular_estudiante",
    ),
    path(
        "desvincular-estudiante/",
        DesvincularEstudianteView.as_view(),
        name="desvincular_estudiante",
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
    path(
        "<int:school_id>/subscription-status/",
        SubscriptionStatusView.as_view(),
        name="subscription_status",
    ),
    path(
        "<int:school_id>/subscription-seats/",
        SubscriptionSeatsView.as_view(),
        name="subscription_seats_list",
    ),
    path(
        "<int:school_id>/subscription-seats/<int:estudiante_curso_id>/",
        SubscriptionSeatsView.as_view(),
        name="subscription_seats_detail",
    ),
    *router.urls,
]
