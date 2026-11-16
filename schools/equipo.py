"""Equipo de una escuela con plan Pro: varios directores y sedes.

Alcance (`estudiantes_visibles`): qué estudiantes de la escuela ve un usuario
en las listas de avance y accesos.
- Admin: todos.
- Director sin sede: toda la escuela. Con sede (y Pro activo): solo los de su sede.

Escritura (invitar directores, sedes) exige la función correspondiente del
plan (`sales.suscripciones.tiene_funcion`). Si la escuela baja a Esencial, los
datos se conservan y la lectura sigue disponible.
"""
import secrets

from django.db import IntegrityError, transaction
from rest_framework import permissions as drf_permissions
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Usuario
from accounts.permissions import is_admin, is_director
from sales.suscripciones import tiene_funcion

from .models import Escuela, Sede


def estudiantes_visibles(user, escuela_id):
    """QuerySet de estudiantes de `escuela_id` que `user` puede ver, o None si no puede."""
    base = Usuario.objects.filter(is_estudiante=True, escuela_id=escuela_id)
    if is_admin(user):
        return base
    if is_director(user) and user.escuela_id == int(escuela_id):
        if user.sede_id and tiene_funcion(escuela_id, 'sedes'):
            return base.filter(sede_id=user.sede_id)
        return base
    return None


# ---------------------------------------------------------------- vistas

def _miembro(u):
    return {
        "id": u.id,
        "nombre": f"{u.nombre} {u.apellido}".strip(),
        "email": u.email,
        "sede": u.sede_id,
        "activo": u.is_active,
    }


class _EquipoBase(APIView):
    permission_classes = [drf_permissions.IsAuthenticated]
    funcion = None  # función del plan exigida para escribir

    def escuela(self, request):
        user = request.user
        if not (is_director(user) and user.escuela_id):
            return None, Response({"error": "Solo un director con escuela."}, status=status.HTTP_403_FORBIDDEN)
        return user.escuela, None

    def exigir(self, escuela, funcion=None):
        funcion = funcion or self.funcion
        if funcion and not tiene_funcion(escuela, funcion):
            return Response(
                {"error": "Esta función es del plan Pro. Sube de plan desde Llaves y plan → Suscripción.",
                 "code": "requiere_pro"},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None


class EquipoView(_EquipoBase):
    """GET: directores y sedes de la escuela."""

    def get(self, request):
        escuela, err = self.escuela(request)
        if err:
            return err
        return Response({
            "pro": tiene_funcion(escuela, 'varios_directores'),
            "yo": request.user.id,
            "directores": [
                _miembro(u) for u in Usuario.objects.filter(escuela=escuela, is_director=True).order_by('nombre')
            ],
            "sedes": [
                {"id": s.id, "nombre": s.nombre, "estudiantes": s.usuarios.filter(is_estudiante=True).count()}
                for s in Sede.objects.filter(escuela=escuela)
            ],
        })


class InvitarMiembroView(_EquipoBase):
    """POST {email, nombre, apellido, sede?}: crea la cuenta de otro director y
    le envía el correo para definir su contraseña."""

    def post(self, request):
        from accounts.services import enviar_invitacion_password
        escuela, err = self.escuela(request)
        if err:
            return err
        err = self.exigir(escuela, 'varios_directores')
        if err:
            return err
        email = (request.data.get("email") or "").strip().lower()
        nombre = (request.data.get("nombre") or "").strip()
        if not email or not nombre:
            return Response({"error": "Indica nombre y email."}, status=status.HTTP_400_BAD_REQUEST)
        if Usuario.objects.filter(email__iexact=email).exists():
            return Response({"error": "Ya existe una cuenta con ese email."}, status=status.HTTP_409_CONFLICT)
        sede = _sede_de(escuela, request.data.get("sede"))
        if sede is False:
            return Response({"error": "Sede no encontrada."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            with transaction.atomic():
                u = Usuario.objects.create_user(
                    email=email, nombre=nombre, apellido=(request.data.get("apellido") or "").strip(),
                    # Contraseña aleatoria: el miembro define la suya con el enlace del correo.
                    password=secrets.token_urlsafe(32), is_director=True,
                )
                u.escuela = escuela
                u.sede = sede
                u.is_active = True
                u.save()
        except IntegrityError:
            return Response({"error": "Ya existe una cuenta con ese email."}, status=status.HTTP_409_CONFLICT)
        enviar_invitacion_password(u)
        return Response(_miembro(u), status=status.HTTP_201_CREATED)


def _sede_de(escuela, sede_id):
    """Sede de la escuela, None si no se indicó, False si no existe."""
    if sede_id in (None, "", "null"):
        return None
    try:
        return Sede.objects.get(pk=int(sede_id), escuela=escuela)
    except (Sede.DoesNotExist, TypeError, ValueError):
        return False


class MiembroView(_EquipoBase):
    """PATCH {sede}: cambia la sede de un director o estudiante.
    DELETE: saca a un director de la escuela (no a uno mismo)."""

    def _objetivo(self, escuela, user_id):
        return Usuario.objects.filter(pk=user_id, escuela=escuela).first()

    def patch(self, request, user_id):
        escuela, err = self.escuela(request)
        if err:
            return err
        err = self.exigir(escuela, 'sedes')
        if err:
            return err
        u = self._objetivo(escuela, user_id)
        if u is None:
            return Response({"error": "Usuario no encontrado."}, status=status.HTTP_404_NOT_FOUND)
        sede = _sede_de(escuela, request.data.get("sede"))
        if sede is False:
            return Response({"error": "Sede no encontrada."}, status=status.HTTP_400_BAD_REQUEST)
        u.sede = sede
        u.save(update_fields=['sede'])
        return Response(_miembro(u))

    def delete(self, request, user_id):
        escuela, err = self.escuela(request)
        if err:
            return err
        u = self._objetivo(escuela, user_id)
        if u is None or not u.is_director:
            return Response({"error": "Miembro no encontrado."}, status=status.HTTP_404_NOT_FOUND)
        if u.id == request.user.id:
            return Response({"error": "No puedes quitarte a ti mismo."}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            u.escuela = None
            u.sede = None
            u.is_director = False
            u.is_active = False
            u.save(update_fields=['escuela', 'sede', 'is_director', 'is_active'])
        return Response({"status": "removed"})


class SedesView(_EquipoBase):
    """POST {nombre}: crea. PATCH /<id>/ {nombre}: renombra. DELETE /<id>/: borra
    (sus usuarios quedan sin sede, es decir, en toda la escuela)."""
    funcion = 'sedes'

    def post(self, request, sede_id=None):
        escuela, err = self.escuela(request)
        if err or (err := self.exigir(escuela)):
            return err
        nombre = (request.data.get("nombre") or "").strip()[:100]
        if not nombre:
            return Response({"error": "Indica el nombre de la sede."}, status=status.HTTP_400_BAD_REQUEST)
        sede, creada = Sede.objects.get_or_create(escuela=escuela, nombre=nombre)
        if not creada:
            return Response({"error": "Ya existe una sede con ese nombre."}, status=status.HTTP_409_CONFLICT)
        return Response({"id": sede.id, "nombre": sede.nombre}, status=status.HTTP_201_CREATED)

    def patch(self, request, sede_id):
        escuela, err = self.escuela(request)
        if err or (err := self.exigir(escuela)):
            return err
        sede = Sede.objects.filter(pk=sede_id, escuela=escuela).first()
        nombre = (request.data.get("nombre") or "").strip()[:100]
        if sede is None or not nombre:
            return Response({"error": "Sede o nombre inválido."}, status=status.HTTP_400_BAD_REQUEST)
        sede.nombre = nombre
        try:
            sede.save(update_fields=['nombre'])
        except IntegrityError:
            return Response({"error": "Ya existe una sede con ese nombre."}, status=status.HTTP_409_CONFLICT)
        return Response({"id": sede.id, "nombre": sede.nombre})

    def delete(self, request, sede_id):
        escuela, err = self.escuela(request)
        if err or (err := self.exigir(escuela)):
            return err
        n, _ = Sede.objects.filter(pk=sede_id, escuela=escuela).delete()
        if not n:
            return Response({"error": "Sede no encontrada."}, status=status.HTTP_404_NOT_FOUND)
        return Response({"status": "deleted"})
