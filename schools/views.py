from rest_framework import viewsets, status, permissions as drf_permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db import transaction as db_transaction
from django.db.models import Prefetch
from django_filters.rest_framework import DjangoFilterBackend

from accounts.permissions import ReadOnlyOrAdmin, is_admin, is_director, is_estudiante
from .models import Escuela, Curso, Leccion, Ejercicio, Glosario, Categoria, Unidad, Recurso
from .serializers import (
    EscuelaSerializer,
    CursoSerializer,
    LeccionSerializer,
    LeccionDetalleSerializer,
    EjercicioSerializer,
    EjercicioConRespuestaSerializer,
    GlosarioSerializer,
    CategoriaSerializer,
    UnidadSerializer,
    RecursoSerializer,
)


LECCION_ORDERING = ('curso_id', 'unidad__orden', 'unidad_id', 'posicion', 'id')


# Campos que un director puede modificar en su propia Escuela via PATCH.
# Todo lo demás (basic_key, professional_key, basic_access, professional_access)
# queda reservado a admin y procesos internos (pagos).
DIRECTOR_EDITABLE_ESCUELA_FIELDS = {"nombre", "email", "telefono", "direccion"}


class _EscuelaScopedPermission(drf_permissions.BasePermission):
    """Admin: full CRUD. Director: GET + PATCH sobre su propia escuela.
    Estudiante: sin acceso.
    """
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if is_admin(request.user):
            return True
        if is_director(request.user):
            # POST/DELETE prohibidos para director.
            return request.method in ("GET", "HEAD", "OPTIONS", "PATCH", "PUT")
        return False

    def has_object_permission(self, request, view, obj):
        if is_admin(request.user):
            return True
        if is_director(request.user):
            return obj.pk == request.user.escuela_id
        return False


class EscuelaViewSet(viewsets.ModelViewSet):
    """Admin: full CRUD sobre todas las escuelas.

    Director: puede VER y EDITAR (campos limitados) su propia escuela.
    Estudiante: no accede aquí.
    """
    queryset = Escuela.objects.all()
    serializer_class = EscuelaSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['email', 'basic_access', 'professional_access']
    permission_classes = [_EscuelaScopedPermission]

    def get_queryset(self):
        user = self.request.user
        if is_admin(user):
            return Escuela.objects.all()
        if is_director(user) and user.escuela_id:
            return Escuela.objects.filter(pk=user.escuela_id)
        return Escuela.objects.none()

    def update(self, request, *args, **kwargs):
        return self._perform_scoped_write(request, partial=kwargs.pop("partial", False), *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        return self._perform_scoped_write(request, partial=True, *args, **kwargs)

    def _perform_scoped_write(self, request, partial=False, *args, **kwargs):
        """PATCH/PUT: si es director, restringe los campos editables.
        Admin puede editar cualquier campo.
        """
        instance = self.get_object()  # 404/403 aplicados por has_object_permission
        data = request.data
        if is_director(request.user) and not is_admin(request.user):
            data = {k: v for k, v in dict(data).items() if k in DIRECTOR_EDITABLE_ESCUELA_FIELDS}
            if not data:
                return Response(
                    {"detail": f"Campos editables por director: {sorted(DIRECTOR_EDITABLE_ESCUELA_FIELDS)}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        serializer = self.get_serializer(instance, data=data, partial=partial)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class ProgresoEstudianteDetalleView(APIView):
    """GET /api/v1/schools/<school_id>/estudiantes/<user_id>/progreso/

    Devuelve el progreso detallado del estudiante en todos los cursos donde
    tiene acceso: lecciones completadas (existencia de EstudianteLeccion),
    pruebas realizadas, certificado si existe.

    Permisos:
    - admin: cualquier estudiante.
    - director: solo estudiantes de su escuela (school_id debe ser la suya).
    - estudiante: solo sobre sí mismo.
    """
    permission_classes = [drf_permissions.IsAuthenticated]

    def get(self, request, school_id, user_id):
        from accounts.models import Usuario, EstudianteLeccion, Prueba, Certificado
        from sales.models import EstudianteCurso

        try:
            school_id = int(school_id)
            user_id = int(user_id)
        except (TypeError, ValueError):
            return Response({"detail": "IDs inválidos."}, status=status.HTTP_400_BAD_REQUEST)

        if not is_admin(request.user):
            if is_director(request.user):
                if request.user.escuela_id != school_id:
                    return Response({"detail": "No autorizado para esa escuela."}, status=status.HTTP_403_FORBIDDEN)
            elif is_estudiante(request.user):
                if request.user.id != user_id:
                    return Response({"detail": "No autorizado."}, status=status.HTTP_403_FORBIDDEN)
            else:
                return Response({"detail": "No autorizado."}, status=status.HTTP_403_FORBIDDEN)

        try:
            estudiante = Usuario.objects.get(pk=user_id)
        except Usuario.DoesNotExist:
            return Response({"detail": "Estudiante no encontrado."}, status=status.HTTP_404_NOT_FOUND)

        if estudiante.escuela_id != school_id and not is_admin(request.user):
            return Response({"detail": "El estudiante no pertenece a esta escuela."}, status=status.HTTP_404_NOT_FOUND)

        inscripciones = (
            EstudianteCurso.objects
            .filter(estudiante_id=estudiante)
            .select_related("curso_id", "access_key_id")
        )
        curso_ids = [ec.curso_id_id for ec in inscripciones]

        # Batch queries por curso para minimizar N+1.
        lecciones_por_curso = {}
        for l in Leccion.objects.filter(curso_id__in=curso_ids):
            lecciones_por_curso.setdefault(l.curso_id, []).append(l)

        completadas = set(
            EstudianteLeccion.objects
            .filter(estudiante=estudiante, curso_id__in=curso_ids)
            .values_list("curso_id", "leccion_id", "updated_at")
        )
        fecha_leccion = {(c, l): t for (c, l, t) in completadas}
        set_completadas = set((c, l) for (c, l, _) in completadas)

        pruebas_por_curso = {}
        for p in Prueba.objects.filter(estudiante=estudiante, curso_id__in=curso_ids):
            pruebas_por_curso.setdefault(p.curso_id, []).append(p)

        certs_por_curso = {
            c.curso_id: c
            for c in Certificado.objects.filter(estudiante=estudiante, curso_id__in=curso_ids)
        }

        cursos_data = []
        for ec in inscripciones:
            curso = ec.curso_id
            key = ec.access_key_id
            lecs = lecciones_por_curso.get(curso.id, [])
            lecciones_detalle = [
                {
                    "id": l.id,
                    "nombre": l.nombre,
                    "unidad": l.unidad_id,
                    "posicion": l.posicion,
                    "completada": (curso.id, l.id) in set_completadas,
                    "fecha_completada": fecha_leccion.get((curso.id, l.id)),
                }
                for l in lecs
            ]
            lecciones_completadas = sum(1 for x in lecciones_detalle if x["completada"])
            pruebas = [
                {
                    "id": p.id,
                    "tipo": p.tipo,
                    "fecha": p.fecha,
                    "score": float(p.score) if p.score is not None else None,
                    "aprobado": p.aprobado,
                }
                for p in pruebas_por_curso.get(curso.id, [])
            ]
            cert = certs_por_curso.get(curso.id)
            certificado = None
            if cert is not None:
                certificado = {
                    "id": cert.id,
                    "codigo": str(cert.codigo),
                    "fecha_emision": cert.fecha_emision,
                }
            cursos_data.append({
                "curso_id": curso.id,
                "curso_nombre": curso.nombre,
                "acceso": {
                    "valid_from": key.valid_from if key else None,
                    "valid_until": key.valid_until if key else None,
                    "status": key.status if key else None,
                },
                "lecciones_total": len(lecs),
                "lecciones_completadas": lecciones_completadas,
                "lecciones_detalle": lecciones_detalle,
                "pruebas": pruebas,
                "certificado": certificado,
            })

        return Response({
            "estudiante": {
                "id": estudiante.id,
                "nombre": estudiante.nombre,
                "apellido": estudiante.apellido,
                "email": estudiante.email,
            },
            "cursos": cursos_data,
        })


class CertificadosPorEscuelaView(APIView):
    """GET /api/v1/schools/<school_id>/certificados/

    Lista los certificados emitidos a estudiantes de la escuela.
    Filtros: ?curso=<id>, ?desde=YYYY-MM-DD, ?hasta=YYYY-MM-DD

    Permisos: admin (cualquier escuela) o director (solo la suya).
    """
    permission_classes = [drf_permissions.IsAuthenticated]

    def get(self, request, school_id):
        from accounts.models import Certificado

        try:
            school_id = int(school_id)
        except (TypeError, ValueError):
            return Response({"detail": "school_id inválido."}, status=status.HTTP_400_BAD_REQUEST)

        if not is_admin(request.user):
            if not is_director(request.user) or request.user.escuela_id != school_id:
                return Response({"detail": "No autorizado."}, status=status.HTTP_403_FORBIDDEN)

        qs = (
            Certificado.objects
            .filter(estudiante__escuela_id=school_id)
            .select_related("estudiante", "curso", "prueba")
        )

        curso_param = request.query_params.get("curso")
        if curso_param:
            try:
                qs = qs.filter(curso_id=int(curso_param))
            except ValueError:
                return Response({"detail": "curso inválido."}, status=status.HTTP_400_BAD_REQUEST)

        desde = request.query_params.get("desde")
        hasta = request.query_params.get("hasta")
        if desde:
            qs = qs.filter(fecha_emision__gte=desde)
        if hasta:
            qs = qs.filter(fecha_emision__lte=hasta)

        qs = qs.order_by("-fecha_emision", "-id")
        results = [
            {
                "id": c.id,
                "codigo": str(c.codigo),
                "estudiante": {
                    "id": c.estudiante_id,
                    "nombre": f"{c.estudiante.nombre} {c.estudiante.apellido}".strip(),
                    "email": c.estudiante.email,
                },
                "curso": {
                    "id": c.curso_id,
                    "nombre": c.curso.nombre if c.curso else None,
                },
                "fecha_emision": c.fecha_emision,
                "prueba_id": c.prueba_id,
                "score": float(c.prueba.score) if c.prueba and c.prueba.score is not None else None,
            }
            for c in qs
        ]

        return Response({"count": len(results), "results": results})


class VincularEstudianteView(APIView):
    """POST /api/v1/schools/vincular-estudiante/

    Vincula (o crea) un estudiante a la escuela del director autenticado.
    Diseñado para reemplazar el patrón inseguro de buscar por email y luego
    PATCH del user — no expone emails a terceros.

    Body:
      email:     requerido
      nombre:    opcional (usado si se crea)
      apellido:  opcional (usado si se crea)

    Respuestas:
      201 { status: "created",        user_id, email }
      200 { status: "linked",         user_id, email }
      200 { status: "already_linked", user_id, email }
      403 { detail: ... }             si el email pertenece a un admin/director/staff
      400 { detail: "email requerido." }
    """
    permission_classes = [drf_permissions.IsAuthenticated]

    def post(self, request):
        if not (is_director(request.user) or is_admin(request.user)):
            return Response({"detail": "Solo director o admin."}, status=status.HTTP_403_FORBIDDEN)
        if is_director(request.user) and not request.user.escuela_id:
            return Response(
                {"detail": "El director no tiene escuela asignada."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        email = (request.data.get("email") or "").strip().lower()
        if not email:
            return Response({"detail": "email requerido."}, status=status.HTTP_400_BAD_REQUEST)

        nombre = (request.data.get("nombre") or "").strip()
        apellido = (request.data.get("apellido") or "").strip()

        from accounts.models import Usuario

        # Determinar la escuela objetivo:
        #   - director → su propia escuela.
        #   - admin  → puede indicar escuela_id en el body; si no, 400.
        if is_director(request.user):
            escuela_id = request.user.escuela_id
        else:
            escuela_id = request.data.get("escuela_id") or request.data.get("escuela")
            if not escuela_id:
                return Response(
                    {"detail": "escuela_id requerido cuando el caller es admin."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not Escuela.objects.filter(pk=escuela_id).exists():
                return Response({"detail": "Escuela no existe."}, status=status.HTTP_404_NOT_FOUND)

        with db_transaction.atomic():
            existing = Usuario.objects.filter(email__iexact=email).select_for_update().first()

            if existing is not None:
                if existing.is_staff or existing.is_superuser or existing.is_director:
                    return Response(
                        {"detail": "No se puede vincular usuario administrativo."},
                        status=status.HTTP_403_FORBIDDEN,
                    )
                if existing.escuela_id == escuela_id:
                    return Response(
                        {"status": "already_linked", "user_id": existing.id, "email": existing.email},
                        status=status.HTTP_200_OK,
                    )
                existing.escuela_id = escuela_id
                existing.is_estudiante = True
                existing.is_active = True
                existing.save(update_fields=["escuela", "is_estudiante", "is_active"])
                return Response(
                    {"status": "linked", "user_id": existing.id, "email": existing.email},
                    status=status.HTTP_200_OK,
                )

            # Crear usuario nuevo con password aleatoria; enviar por email.
            import secrets
            random_password = secrets.token_urlsafe(12)
            new_user = Usuario.objects.create_user(
                email=email,
                nombre=nombre or "Estudiante",
                apellido=apellido or "",
                password=random_password,
                is_estudiante=True,
            )
            new_user.is_active = True
            new_user.escuela_id = escuela_id
            new_user.save(update_fields=["is_active", "escuela"])

            # Enviar credenciales por email (best-effort).
            try:
                from django.core.mail import send_mail
                send_mail(
                    subject="Tu cuenta AutoTest",
                    message=(
                        f"Hola {nombre or 'estudiante'},\n\n"
                        f"Se creó tu cuenta en AutoTest.\n"
                        f"Correo: {email}\n"
                        f"Contraseña temporal: {random_password}\n\n"
                        f"Te recomendamos cambiarla en tu primer inicio de sesión."
                    ),
                    from_email="soporte.autotest@gmail.com",
                    recipient_list=[email],
                    fail_silently=True,
                )
            except Exception:
                pass

            return Response(
                {"status": "created", "user_id": new_user.id, "email": new_user.email},
                status=status.HTTP_201_CREATED,
            )


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
        lecciones_ordenadas = Leccion.objects.select_related('categoria').order_by('posicion', 'id')
        unidades = (
            Unidad.objects.filter(curso=curso)
            .prefetch_related(Prefetch('lecciones', queryset=lecciones_ordenadas))
            .order_by('orden', 'id')
        )
        return Response(UnidadSerializer(unidades, many=True).data)


class LeccionViewSet(viewsets.ModelViewSet):
    queryset = Leccion.objects.all()
    serializer_class = LeccionSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['curso', 'unidad', 'posicion', 'tipo']
    permission_classes = [ReadOnlyOrAdmin]
    pagination_class = None

    def get_queryset(self):
        return (
            Leccion.objects
            .select_related('curso', 'unidad', 'categoria')
            .order_by(*LECCION_ORDERING)
        )

    def get_serializer_class(self):
        # El frontend del curso consume /lessons/?curso=<id> como la lista completa.
        # En ese caso devolvemos contenido para evitar un fetch adicional por lección.
        if self.action == 'retrieve' or (self.action == 'list' and self.request.query_params.get('curso')):
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
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['categoria', 'curso', 'leccion']

    def get_serializer_class(self):
        """Admin ve y escribe `respuesta`; el resto recibe el serializer
        público que oculta la respuesta correcta."""
        if is_admin(self.request.user):
            return EjercicioConRespuestaSerializer
        return EjercicioSerializer


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

