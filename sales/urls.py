from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ProductoViewSet, 
    VentaViewSet, 
    AccessKeyViewSet, 
    SaleInitiationViewSet, 
    PaymentConfirmationView, 
    EstudianteCursoViewSet, 
    UnifiedSaleInitiationView, 
    UnifiedPaymentConfirmationView, 
    ActivarCursoView, 
    EstudianteCursoDetailViewSet,
    EstudiantesCursosActivosPorEscuelaView,
    CursosDisponiblesParaUsuarioView
)

router = DefaultRouter()
router.register(r'productos', ProductoViewSet)
router.register(r'ventas', VentaViewSet)
router.register(r'access_key', AccessKeyViewSet)
router.register(r'estudiante_curso', EstudianteCursoViewSet)
router.register(r'estudiante-cursos-detail', EstudianteCursoDetailViewSet, basename='estudiante-curso-detail')

urlpatterns = [
    path('webpay_init/', SaleInitiationViewSet.as_view(), name='webpay_init_view'),
    path('webpay_confirm/', PaymentConfirmationView.as_view(), name='webpay_confirm_view'),
    path('pay_init/', UnifiedSaleInitiationView.as_view(), name='pay_init_view'),
    path('pay_confirm/', UnifiedPaymentConfirmationView.as_view(), name='pay_confirm_view'),
    path('activar_curso/', ActivarCursoView.as_view(), name='activar_curso_view'),
    path('escuelas/<int:escuela_id>/estudiantes-cursos-activos/', EstudiantesCursosActivosPorEscuelaView.as_view(),name='estudiantes_cursos_activos_por_escuela'),
    path(
        "escuelas/<int:escuela_id>/usuario/<int:user_id>/cursos-disponibles/",
        CursosDisponiblesParaUsuarioView.as_view(),
        name="cursos_disponibles_por_usuario",
    ),
] + router.urls
