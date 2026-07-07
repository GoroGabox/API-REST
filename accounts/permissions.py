"""Permisos a nivel de endpoint y objeto para los 3 roles de AutoTest.

Roles:
    admin       -> is_staff / is_superuser
    director    -> is_director=True
    estudiante  -> is_estudiante=True

Decisiones de diseño:
- Roles son del dominio (no configurables en runtime) -> usamos permission
  classes en código en vez de Django Groups+permissions.
- El scoping object-level (director ve solo SU escuela, estudiante solo sus
  datos) se enforce aquí (`has_object_permission`) y además en `get_queryset`
  de cada ViewSet para que las listas también vengan filtradas.
"""
from rest_framework import permissions


def is_admin(user):
    return bool(user and user.is_authenticated and (user.is_staff or user.is_superuser))


def is_director(user):
    return bool(user and user.is_authenticated and getattr(user, 'is_director', False))


def is_estudiante(user):
    return bool(user and user.is_authenticated and getattr(user, 'is_estudiante', False))


class IsAdmin(permissions.BasePermission):
    """Solo admins de AutoTest (is_staff/is_superuser)."""
    def has_permission(self, request, view):
        return is_admin(request.user)


class IsDirector(permissions.BasePermission):
    def has_permission(self, request, view):
        return is_director(request.user)


class IsEstudiante(permissions.BasePermission):
    def has_permission(self, request, view):
        return is_estudiante(request.user)


class ReadOnlyOrAdmin(permissions.BasePermission):
    """Cualquier autenticado lee; solo admin escribe.

    Pensado para el catálogo de schools/* y sales.Producto: la tienda
    necesita ser legible por todos los roles (incluido estudiante para ver
    qué puede comprar).
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        return is_admin(request.user)


class PublicReadOrAdmin(permissions.BasePermission):
    """GET público (anónimo permitido); escritura solo admin.

    Para superficies del catálogo que alimentan la landing (/explore,
    detalle de curso público) — deben ser indexables y visibles sin
    sesión. El contenido premium (lecciones, ejercicios, recursos) sigue
    detrás de ReadOnlyOrAdmin.
    """
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return bool(request.user and request.user.is_authenticated and is_admin(request.user))


class IsAdminOrDirectorOfSchool(permissions.BasePermission):
    """Admin pasa siempre. Director solo si el objeto pertenece a su escuela.

    Se asume que el objeto expone una FK `escuela` o un campo accesible
    para resolverla (override `get_object_escuela_id` en la view si no).
    """
    def has_permission(self, request, view):
        return is_admin(request.user) or is_director(request.user)

    def has_object_permission(self, request, view, obj):
        if is_admin(request.user):
            return True
        if not is_director(request.user):
            return False
        escuela_id = _resolve_escuela_id(obj)
        user_escuela = getattr(request.user, 'escuela_id', None)
        return escuela_id is not None and escuela_id == user_escuela


class IsAdminOrSelf(permissions.BasePermission):
    """Admin pasa; usuario común solo sobre su propio registro."""
    def has_object_permission(self, request, view, obj):
        if is_admin(request.user):
            return True
        # `obj` puede ser un Usuario o algo con FK al usuario.
        if hasattr(obj, 'pk') and obj.__class__.__name__ == 'Usuario':
            return obj.pk == request.user.pk
        # FK a estudiante
        estudiante = getattr(obj, 'estudiante', None) or getattr(obj, 'estudiante_id', None)
        if estudiante is None:
            usuario = getattr(obj, 'usuario', None)
            return usuario is not None and getattr(usuario, 'pk', None) == request.user.pk
        return getattr(estudiante, 'pk', estudiante) == request.user.pk


class IsAdminOrOwnerOrDirectorOfSchool(permissions.BasePermission):
    """Combinación común para Pruebas, Certificados, Lecciones del estudiante:

    - admin: todo
    - estudiante: solo sobre objetos suyos
    - director: sobre objetos cuyo estudiante pertenece a su escuela
    """
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if is_admin(request.user):
            return True
        estudiante = getattr(obj, 'estudiante', None) or getattr(obj, 'usuario', None)
        if estudiante is None:
            return False
        if is_estudiante(request.user):
            return getattr(estudiante, 'pk', None) == request.user.pk
        if is_director(request.user):
            return getattr(estudiante, 'escuela_id', None) == request.user.escuela_id
        return False


def _resolve_escuela_id(obj):
    """Heurística para extraer escuela_id desde distintos modelos."""
    if hasattr(obj, 'escuela_id'):
        return obj.escuela_id
    if hasattr(obj, 'pk') and obj.__class__.__name__ == 'Escuela':
        return obj.pk
    usuario = getattr(obj, 'usuario', None) or getattr(obj, 'estudiante', None)
    if usuario is not None:
        return getattr(usuario, 'escuela_id', None)
    return None
