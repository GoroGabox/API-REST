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
    PlanCursoViewSet,
    VincularEstudianteView,
    DesvincularEstudianteView,
    ImportarEstudiantesView,
    EditarEstudianteView,
    ProgresoEstudianteDetalleView,
    CertificadosPorEscuelaView,
    CourseGenerateView,
    CoursePlanView,
    CourseImportView,
)
# Vistas de suscripción viven en `sales/views.py` porque manipulan
# EstudianteCurso/AccessKey, pero se exponen bajo el árbol de schools para
# consistencia con el spec del frontend.
from sales.views import SubscriptionStatusView, SubscriptionSeatsView
from . import equipo
from . import views_plan_estudio as vpe

router = DefaultRouter()
router.register(r'schools', EscuelaViewSet)
router.register(r'courses', CursoViewSet)
router.register(r'plan-cursos', PlanCursoViewSet)
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
    path("courses/plan/", CoursePlanView.as_view(), name="course_plan"),
    path("courses/import/", CourseImportView.as_view(), name="course_import"),
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
        "importar-estudiantes/",
        ImportarEstudiantesView.as_view(),
        name="importar_estudiantes",
    ),
    path(
        "estudiantes/<int:user_id>/",
        EditarEstudianteView.as_view(),
        name="editar_estudiante",
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
        "<int:school_id>/subscription-seats/<int:cupo_id>/",
        SubscriptionSeatsView.as_view(),
        name="subscription_seats_detail",
    ),
    # Equipo (plan Pro): directores y sedes.
    path("equipo/", equipo.EquipoView.as_view(), name="equipo"),
    path("equipo/invitar/", equipo.InvitarMiembroView.as_view(), name="equipo_invitar"),
    path("equipo/miembros/<int:user_id>/", equipo.MiembroView.as_view(), name="equipo_miembro"),
    path("equipo/sedes/", equipo.SedesView.as_view(), name="equipo_sedes"),
    path("equipo/sedes/<int:sede_id>/", equipo.SedesView.as_view(), name="equipo_sede"),
    # Planes de estudio por escuela (director; admin con ?escuela=).
    path("planes-estudio/", vpe.PlanesEstudioView.as_view(), name="planes_estudio"),
    path("planes-estudio/<int:plan_id>/", vpe.PlanEstudioDetalleView.as_view(), name="plan_estudio"),
    path("planes-estudio/<int:plan_id>/activar/",
         vpe.PlanEstudioEstadoView.as_view(accion='activar'), name="plan_estudio_activar"),
    path("planes-estudio/<int:plan_id>/desactivar/",
         vpe.PlanEstudioEstadoView.as_view(accion='desactivar'), name="plan_estudio_desactivar"),
    path("planes-estudio/<int:plan_id>/ops/", vpe.PlanEstudioOpsView.as_view(), name="plan_estudio_ops"),
    path("planes-estudio/<int:plan_id>/historial/",
         vpe.PlanEstudioHistorialView.as_view(), name="plan_estudio_historial"),
    path("planes-estudio/<int:plan_id>/lecciones/",
         vpe.PlanLeccionesPropiasView.as_view(), name="plan_estudio_lecciones"),
    path("planes-estudio/<int:plan_id>/lecciones/<int:leccion_id>/",
         vpe.PlanLeccionesPropiasView.as_view(), name="plan_estudio_leccion"),
    path("planes-estudio/<int:plan_id>/lecciones/<int:leccion_id>/recursos/",
         vpe.PlanLeccionRecursosView.as_view(), name="plan_estudio_recursos"),
    path("planes-estudio/<int:plan_id>/lecciones/<int:leccion_id>/recursos/<int:recurso_id>/",
         vpe.PlanLeccionRecursosView.as_view(), name="plan_estudio_recurso"),
    # Admin: costo de mantención de los planes por escuela.
    path("admin/mantencion/", vpe.MantencionView.as_view(), name="mantencion"),
    path("admin/mantencion/<int:escuela_id>/", vpe.MantencionEscuelaView.as_view(), name="mantencion_escuela"),
    path("admin/mantencion/<int:escuela_id>/costos/",
         vpe.MantencionCostosView.as_view(), name="mantencion_costos"),
    path("admin/mantencion/<int:escuela_id>/costos/<int:costo_id>/",
         vpe.MantencionCostosView.as_view(), name="mantencion_costo"),
    *router.urls,
]
