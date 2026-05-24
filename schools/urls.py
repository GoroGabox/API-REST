from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import EscuelaViewSet, CursoViewSet, LeccionViewSet, GlosarioViewSet, CategoriaViewSet, EjercicioViewSet

router = DefaultRouter()
router.register(r'schools', EscuelaViewSet)
router.register(r'courses', CursoViewSet)
router.register(r'lessons', LeccionViewSet)
router.register(r'glosary', GlosarioViewSet)
router.register(r'exercices', EjercicioViewSet)
router.register(r'categories', CategoriaViewSet)

urlpatterns = router.urls