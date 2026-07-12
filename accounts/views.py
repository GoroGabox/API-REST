import os
from collections import defaultdict
from django.conf import settings
from django.http import Http404
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.contrib.auth.models import Group, Permission
from django.contrib.auth.password_validation import validate_password
from rest_framework import viewsets, status, permissions
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework.generics import CreateAPIView, ListAPIView, ListCreateAPIView, RetrieveUpdateDestroyAPIView, RetrieveAPIView
from django_filters.rest_framework import DjangoFilterBackend
from .models import  Usuario, DirectorProfile, EstudianteProfile, Certificado, Prueba, PruebaEjercicio, EstudianteLeccion
from schools.models import Ejercicio, Categoria, Leccion
from sales.models import EstudianteCurso
from .serializers import  (
    UsuariorRegisterSerializer,
    UsuarioSerializer,
    AdminUsuarioCreateSerializer,
    UsuarioMeSerializer,
    LogroSerializer,
    LeaderboardEntrySerializer,
    NotificacionSerializer,
    PushTokenSerializer,
    DirectorProfileSerializer,
    EstudianteProfileSerializer,
    MyTokenObtainPairSerializer,
    CertificadoSerializer,
    CertificadoPublicoSerializer,
    PruebaSerializer,
    PruebaDetalleSerializer,
    PruebaEjercicioSerializer,
    SubmitPruebaSerializer,
    SendEmailSerializer,
    ResetPasswordSerializer,
    GroupSerializer,
    PermissionSerializer,
    EstudianteLeccionSerializer,
    EscuelaConDirectorSerializer,
)
from .permissions import (
    IsAdmin,
    IsDirector,
    IsEstudiante,
    ReadOnlyOrAdmin,
    IsAdminOrSelf,
    IsAdminOrOwnerOrDirectorOfSchool,
    is_admin,
    is_director,
    is_estudiante,
)

class UsuarioRegisterView(CreateAPIView):
    serializer_class = UsuariorRegisterSerializer
    permission_classes = [permissions.AllowAny]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            user = serializer.save()
            token_serializer = MyTokenObtainPairSerializer.get_token(user)
            response_data = {
                    "refresh": str(token_serializer),
                    "access": str(token_serializer.access_token),
                }
            return Response(response_data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class UsuarioListView(ListCreateAPIView):
    """GET admin: ve todos; director: solo de su escuela; estudiante: 403.
    POST: alta de usuarios con roles/estado (solo admin)."""
    queryset = Usuario.objects.all()
    serializer_class = UsuarioSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['escuela', 'is_director', 'is_estudiante', 'is_active', 'email']

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return AdminUsuarioCreateSerializer
        return UsuarioSerializer

    def get_permissions(self):
        # Crear usuarios (con roles) queda reservado a admin.
        if self.request.method == 'POST':
            return [permissions.IsAuthenticated(), IsAdmin()]
        return [permissions.IsAuthenticated()]

    def get_queryset(self):
        user = self.request.user
        if is_admin(user):
            return Usuario.objects.all()
        if is_director(user):
            return Usuario.objects.filter(escuela_id=user.escuela_id, is_estudiante=True)
        return Usuario.objects.none()


class UsuarioRetrieveUpdateDestroyView(RetrieveUpdateDestroyAPIView):
    queryset = Usuario.objects.all()
    serializer_class = UsuarioSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdminOrSelf]


class EscuelaConDirectorView(APIView):
    """POST /api/v1/accounts/registrar-escuela-director/ — solo admin."""
    permission_classes = [IsAdmin]

    def post(self, request):
        serializer = EscuelaConDirectorSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()
        return Response(serializer.to_representation(instance), status=status.HTTP_201_CREATED)

class MyTokenObtainPairView(TokenObtainPairView):
    serializer_class = MyTokenObtainPairSerializer
    permission_classes = [permissions.AllowAny]

class DirectorProfileViewSet(viewsets.ModelViewSet):
    queryset = DirectorProfile.objects.all()
    serializer_class = DirectorProfileSerializer
    permission_classes = [IsAdmin]


class EstudianteProfileViewSet(viewsets.ModelViewSet):
    queryset = EstudianteProfile.objects.all()
    serializer_class = EstudianteProfileSerializer
    permission_classes = [IsAdmin]

class LogOutAPIView(APIView):
    def post(self, request, format=None):
        try:
            refresh_token = request.data.get('refresh')
            token_obj = RefreshToken(refresh_token)
            token_obj.blacklist()
            return Response(status=status.HTTP_200_OK)
        except Exception:
            return Response(status=status.HTTP_400_BAD_REQUEST)

#cambiar links de retorno
class PasswordResetRequestView(APIView):
    serializer_class = SendEmailSerializer
    permission_classes = [permissions.AllowAny]

    def post(self, request, *args, **kwargs):
        email = request.data.get('email')
        user = Usuario.objects.filter(email=email).first()
        if user:
            uid = urlsafe_base64_encode(force_bytes(user.id))
            token = default_token_generator.make_token(user)
            frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:3000').rstrip('/')
            password_reset_link = f'{frontend_url}/change-password?uidb64={uid}&token={token}'
            
            send_mail(
                subject='Restablecimiento de contraseña',
                message='Sigue este enlace para restablecer tu contraseña: {}'.format(password_reset_link),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
            )
            return Response({"mensaje": "Se ha enviado un correo electrónico con instrucciones para restablecer tu contraseña."}, status=status.HTTP_200_OK)
        return Response({"error": "No se ha encontrado un usuario con ese correo electrónico."}, status=status.HTTP_400_BAD_REQUEST)

class PasswordResetInviteView(APIView):
    """POST /api/v1/accounts/password-reset-invite/<user_id>/

    Genera un enlace de reset y lo envía por email al usuario indicado.
    Pensado para el flujo director/admin: tras crear/vincular un estudiante,
    disparar este endpoint para que el usuario defina su propia password
    sin exponer credenciales temporales.

    Permisos:
    - admin: cualquier usuario.
    - director: solo estudiantes de su escuela.
    - estudiante: 403.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, user_id):
        if not (is_admin(request.user) or is_director(request.user)):
            return Response({"detail": "No autorizado."}, status=status.HTTP_403_FORBIDDEN)

        try:
            target = Usuario.objects.get(pk=user_id)
        except Usuario.DoesNotExist:
            return Response({"detail": "Usuario no encontrado."}, status=status.HTTP_404_NOT_FOUND)

        if is_director(request.user):
            if target.escuela_id != request.user.escuela_id:
                return Response({"detail": "No autorizado sobre ese usuario."}, status=status.HTTP_403_FORBIDDEN)
            if target.is_staff or target.is_superuser or target.is_director:
                return Response({"detail": "No autorizado sobre ese usuario."}, status=status.HTTP_403_FORBIDDEN)

        uid = urlsafe_base64_encode(force_bytes(target.id))
        token = default_token_generator.make_token(target)
        frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:3000').rstrip('/')
        link = f'{frontend_url}/change-password?uidb64={uid}&token={token}'

        try:
            send_mail(
                subject='Configura tu contraseña — AutoTest',
                message=(
                    f'Hola {target.nombre or ""},\n\n'
                    f'Tu cuenta AutoTest ha sido creada. Define tu contraseña '
                    f'accediendo al siguiente enlace:\n\n{link}\n\n'
                    f'Si no reconoces esta invitación, ignora este correo.'
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[target.email],
                fail_silently=True,
            )
        except Exception:
            pass

        return Response(
            {"status": "sent", "user_id": target.id, "email": target.email},
            status=status.HTTP_200_OK,
        )


class CustomPasswordResetConfirmView(APIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = ResetPasswordSerializer

    def post(self, request, *args, **kwargs):
        try:
            uid = urlsafe_base64_decode(request.data.get('uid')).decode()
            user = Usuario.objects.get(pk=uid)
            token = default_token_generator.check_token(user, request.data.get('token'))

            if user is not None and token:
                serializer = self.serializer_class(data=request.data, context={'user': user})
                serializer.is_valid(raise_exception=True)

                # Establecer la nueva contraseña
                new_password = serializer.validated_data['new_password']
                user.set_password(new_password)
                user.save()
                return Response({"mensaje": "La contraseña ha sido restablecida con éxito."}, status=status.HTTP_200_OK)
            else:
                return Response({"error": "El enlace de restablecimiento no es válido o ha expirado."}, status=status.HTTP_400_BAD_REQUEST)
        except (TypeError, ValueError, OverflowError, Usuario.DoesNotExist):
            return Response({"error": "Ha ocurrido un error al procesar la solicitud."}, status=status.HTTP_400_BAD_REQUEST)

def _scope_by_estudiante(qs, user, estudiante_field='estudiante'):
    """Filtra queryset según rol: admin todo; estudiante solo propios; director solo su escuela."""
    if is_admin(user):
        return qs
    if is_estudiante(user):
        return qs.filter(**{estudiante_field: user})
    if is_director(user):
        return qs.filter(**{f'{estudiante_field}__escuela_id': user.escuela_id})
    return qs.none()


class CertificadoViewSet(viewsets.ModelViewSet):
    queryset = Certificado.objects.all()
    serializer_class = CertificadoSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['estudiante', 'curso', 'fecha_emision']
    permission_classes = [permissions.IsAuthenticated, IsAdminOrOwnerOrDirectorOfSchool]

    def get_queryset(self):
        return _scope_by_estudiante(Certificado.objects.all(), self.request.user)


class PruebaViewSet(viewsets.ModelViewSet):
    queryset = Prueba.objects.all()
    serializer_class = PruebaSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['estudiante', 'fecha', 'aprobado']
    permission_classes = [permissions.IsAuthenticated, IsAdminOrOwnerOrDirectorOfSchool]

    def get_queryset(self):
        return _scope_by_estudiante(Prueba.objects.all(), self.request.user)


class PruebaEjercicioViewSet(viewsets.ModelViewSet):
    queryset = PruebaEjercicio.objects.all()
    serializer_class = PruebaEjercicioSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['prueba', 'ejercicio']
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return _scope_by_estudiante(
            PruebaEjercicio.objects.all(), self.request.user,
            estudiante_field='prueba__estudiante',
        )

class SendActivationEmailView(APIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = SendEmailSerializer

    def post(self, request):
        email = request.data.get('email')
        try:
            user = Usuario.objects.get(email=email)
            if user.is_active:
                return Response({"detail": "La cuenta ya está activa."}, status=status.HTTP_400_BAD_REQUEST)

            # Rotar el token si fue invalidado por un activate previo.
            if user.activation_token is None:
                import uuid as _uuid
                user.activation_token = _uuid.uuid4()
                user.save(update_fields=['activation_token'])

            activation_url = request.build_absolute_uri(
                reverse('activate_account', args=[str(user.activation_token)])
            )
            send_mail(
                subject="Activa tu cuenta",
                message=f"Por favor activa tu cuenta usando el siguiente enlace: {activation_url}",
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
            )
            return Response({"detail": "Correo de activación enviado."}, status=status.HTTP_200_OK)
        except Usuario.DoesNotExist:
            return Response({"detail": "Usuario no encontrado."}, status=status.HTTP_404_NOT_FOUND)

class ActivateAccountView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request, token):
        try:
            user = Usuario.objects.get(activation_token=token)
        except (Usuario.DoesNotExist, ValueError, Http404):
            return Response({"detail": "Token inválido o usuario no encontrado."}, status=status.HTTP_404_NOT_FOUND)

        if user.is_active:
            return Response({"detail": "La cuenta ya está activa."}, status=status.HTTP_200_OK)

        user.is_active = True
        # Invalidar el token de activación (un solo uso).
        user.activation_token = None
        user.save(update_fields=['is_active', 'activation_token'])
        return Response({"detail": "Cuenta activada con éxito."}, status=status.HTTP_200_OK)

class GroupViewSet(viewsets.ModelViewSet):
    """Gestión de Grupos — solo admin."""
    queryset = Group.objects.all()
    serializer_class = GroupSerializer
    permission_classes = [IsAdmin]


class PermissionViewSet(viewsets.ReadOnlyModelViewSet):
    """Lectura de permisos — solo admin."""
    queryset = Permission.objects.all()
    serializer_class = PermissionSerializer
    permission_classes = [IsAdmin]


class EstudianteLeccionViewSet(viewsets.ModelViewSet):
    queryset = EstudianteLeccion.objects.all()
    serializer_class = EstudianteLeccionSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["estudiante", "curso", "leccion"]
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = EstudianteLeccion.objects.select_related("estudiante", "curso", "leccion")
        return _scope_by_estudiante(qs, self.request.user)

    def create(self, request, *args, **kwargs):
        """POST idempotente.

        Si el estudiante ya tiene un registro para la lección, devolvemos
        el existente con HTTP 200 (no duplicamos). Sólo la primera vez se
        crea uno nuevo (HTTP 201). La DB también lo enforce vía
        `UniqueConstraint(estudiante, leccion)`.

        Implementación: el lookup de existencia se hace ANTES de pasar
        por `serializer.is_valid()` porque el `UniqueTogetherValidator`
        del serializer rechazaría duplicados con 400 antes de poder
        decidir si responder idempotentemente.
        """
        data = request.data
        leccion_id = data.get("leccion")

        # Estudiante: ignora `estudiante` del body y usa request.user.
        if is_estudiante(request.user):
            estudiante_id = request.user.id
        else:
            estudiante_id = data.get("estudiante")

        if not leccion_id or not estudiante_id:
            return Response(
                {"detail": "estudiante y leccion son requeridos."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing = EstudianteLeccion.objects.filter(
            estudiante_id=estudiante_id, leccion_id=leccion_id,
        ).first()
        if existing is not None:
            return Response(
                self.get_serializer(existing).data,
                status=status.HTTP_200_OK,
            )

        # No existe: validar con serializer (sin disparar uniqueness, porque ya
        # confirmamos que no hay duplicado) y crear.
        payload = dict(data)
        payload["estudiante"] = estudiante_id
        serializer = self.get_serializer(data=payload)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()
        return Response(
            self.get_serializer(instance).data,
            status=status.HTTP_201_CREATED,
        )

from . import services as accounts_services


class GenerarPruebaView(APIView):
    """
    POST /api/pruebas/generar
    Body:
    {
      "tipo": "completa" | "rapida" | "categoria",
      "categoria_id": 123  # requerido solo si tipo="categoria"
    }

    Respuesta:
    {
      "prueba_id": int,
      "tipo": str,
      "preguntas": [
        {
          "id": int,
          "pregunta": str,
          "imagen": str,
          "opciones": {
            "a": str | null,
            "b": str | null,
            "c": str | null,
            "d": str | null,
            "e": str | null,
            "f": str | null
          },
          "categoria_id": int,
          "curso_id": int | null,
          "leccion_id": int | null
        }, ...
      ],
      "total": int
    }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tipo = request.data.get('tipo')
        categoria_id = request.data.get('categoria_id')
        modalidad = (request.data.get('modalidad') or 'practica').lower().strip()
        if modalidad not in ('practica', 'evaluacion'):
            return Response({"detail": "modalidad debe ser 'practica' o 'evaluacion'."}, status=status.HTTP_400_BAD_REQUEST)

        curso = None
        curso_id = request.data.get('curso_id')
        if curso_id:
            from schools.models import Curso as _Curso
            try:
                curso = _Curso.objects.get(pk=curso_id)
            except _Curso.DoesNotExist:
                return Response({"detail": "Curso no existe."}, status=status.HTTP_404_NOT_FOUND)

        ejercicios, error = accounts_services.seleccionar_ejercicios(tipo, categoria_id)
        if error:
            return Response({"detail": error.detail}, status=error.status)

        # Iniciar pruebas no consume recursos: los clientes pagan por acceso
        # al generador y la energía como recurso limitado fue eliminada.
        prueba = accounts_services.crear_prueba_con_ejercicios(
            request.user, ejercicios,
            tipo=tipo.lower().strip(),
            modalidad=modalidad,
            curso=curso,
        )

        return Response({
            "prueba_id": prueba.id,
            "tipo": prueba.tipo,
            "modalidad": prueba.modalidad,
            "curso_id": prueba.curso_id,
            "preguntas": accounts_services.serializar_preguntas_publicas(ejercicios),
            "total": len(ejercicios),
        }, status=status.HTTP_201_CREATED)


class SubmitPruebaView(APIView):
    """POST /api/v1/accounts/tests/<prueba_id>/submit/

    Body: { "respuestas": { pregunta_id: opcion_key ('a'..'f') } }

    Devuelve: aprobado, score, total_correctas, detalles[].
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, prueba_id):
        try:
            prueba = Prueba.objects.select_related('estudiante', 'curso').get(pk=prueba_id)
        except Prueba.DoesNotExist:
            return Response({"detail": "Prueba no encontrada."}, status=status.HTTP_404_NOT_FOUND)

        # Solo el dueño o admin pueden enviar.
        if not is_admin(request.user) and prueba.estudiante_id != request.user.id:
            return Response({"detail": "No autorizado."}, status=status.HTTP_403_FORBIDDEN)

        serializer = SubmitPruebaSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        respuestas_raw = serializer.validated_data['respuestas']
        respuestas = {int(k): v for k, v in respuestas_raw.items()}

        try:
            resultado = accounts_services.submit_prueba(prueba, respuestas)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        # Si el signal emitió un certificado, exponerlo.
        prueba.refresh_from_db()
        cert = Certificado.objects.filter(prueba=prueba).first()
        if cert:
            resultado['certificado'] = {
                'id': cert.id, 'codigo': str(cert.codigo),
                'fecha_emision': cert.fecha_emision,
            }
        resultado['prueba'] = PruebaDetalleSerializer(prueba).data
        return Response(resultado, status=status.HTTP_200_OK)


class MisPruebasView(ListAPIView):
    """GET /api/v1/accounts/me/tests/ — historial del estudiante."""
    serializer_class = PruebaDetalleSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Prueba.objects.filter(estudiante=self.request.user).order_by('-fecha', '-id')


class MisPruebasDetalleView(APIView):
    """GET /api/v1/accounts/me/tests/<prueba_id>/ — detalle con respuestas."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, prueba_id):
        try:
            prueba = (
                Prueba.objects
                .prefetch_related('items__ejercicio')
                .get(pk=prueba_id)
            )
        except Prueba.DoesNotExist:
            return Response({"detail": "Prueba no encontrada."}, status=status.HTTP_404_NOT_FOUND)

        if not is_admin(request.user) and prueba.estudiante_id != request.user.id:
            return Response({"detail": "No autorizado."}, status=status.HTTP_403_FORBIDDEN)

        detalles = []
        for item in prueba.items.all():
            detalles.append({
                "pregunta_id": item.ejercicio_id,
                "pregunta": item.ejercicio.pregunta,
                "respuesta_estudiante": item.respuesta_estudiante,
                "correcta": item.correcta,
                "opcion_correcta": accounts_services._opcion_correcta_para(item.ejercicio),
                "explicacion": item.ejercicio.explicacion or "",
            })
        return Response({
            "prueba": PruebaDetalleSerializer(prueba).data,
            "respuestas": detalles,
        })


class SocialLoginGoogleView(APIView):
    """POST /api/v1/accounts/social/google/  Body: { id_token }"""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        from . import social
        id_token = (request.data.get('id_token') or '').strip()
        if not id_token:
            return Response({"detail": "id_token requerido."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            payload = social.verify_google_id_token(id_token)
        except social.SocialAuthError as e:
            return Response({"detail": str(e)}, status=status.HTTP_401_UNAUTHORIZED)
        user, _created = social.obtener_o_crear_usuario_social(payload)
        return Response(social.emitir_jwt_para_usuario(user), status=status.HTTP_200_OK)


class SocialLoginAppleView(APIView):
    """POST /api/v1/accounts/social/apple/  Body: { identity_token }"""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        from . import social
        token = (request.data.get('identity_token') or '').strip()
        if not token:
            return Response({"detail": "identity_token requerido."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            payload = social.verify_apple_identity_token(token)
        except social.SocialAuthError as e:
            return Response({"detail": str(e)}, status=status.HTTP_401_UNAUTHORIZED)
        user, _created = social.obtener_o_crear_usuario_social(payload)
        return Response(social.emitir_jwt_para_usuario(user), status=status.HTTP_200_OK)


class MeView(APIView):
    """GET /api/v1/accounts/me/ — perfil + stats. PATCH para actualizar perfil."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(UsuarioMeSerializer(request.user).data)

    def patch(self, request):
        serializer = UsuarioSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UsuarioMeSerializer(request.user).data)


class MeStatsView(APIView):
    """GET /api/v1/accounts/me/stats/ — solo el bloque de gamificación."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from . import gamification
        gamification.regenerar_recursos(request.user)
        u = request.user
        u.refresh_from_db()
        nivel = gamification.nivel_para_xp(u.xp)
        return Response({
            'hearts': u.hearts,
            'max_hearts': gamification.MAX_HEARTS,
            'next_heart_regen_at': u.next_heart_regen_at,
            'xp': u.xp,
            **nivel,
            'streak_current': u.streak_current,
            'streak_longest': u.streak_longest,
            'streak_frozen_until': u.streak_frozen_until,
            'last_active_date': u.last_active_date,
        })


class TwoFASetupView(APIView):
    """POST /api/v1/accounts/me/2fa/setup/ — inicia el enrolamiento 2FA.

    Genera (o regenera) un secreto TOTP pendiente y devuelve la URI de
    aprovisionamiento + un QR PNG (data URI) para escanear con la app
    autenticadora. NO activa 2FA todavía: eso ocurre al verificar un código
    válido en `TwoFAVerifyView`.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        import base64
        import io
        import pyotp
        import qrcode

        user = request.user
        if user.is_2fa_enabled:
            return Response(
                {"detail": "La autenticación en dos pasos ya está activada."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        secret = pyotp.random_base32()
        user.totp_secret = secret
        user.save(update_fields=["totp_secret"])

        issuer = getattr(settings, "TOTP_ISSUER", "AutoTest")
        otpauth_uri = pyotp.TOTP(secret).provisioning_uri(
            name=user.email, issuer_name=issuer
        )

        img = qrcode.make(otpauth_uri)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

        return Response(
            {"secret": secret, "otpauth_uri": otpauth_uri, "qr": qr_data_uri},
            status=status.HTTP_200_OK,
        )


class TwoFAVerifyView(APIView):
    """POST /api/v1/accounts/me/2fa/verify/ {code} — activa 2FA.

    Valida el código de 6 dígitos contra el secreto pendiente. Solo entonces
    marca `is_2fa_enabled=True`.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        import pyotp

        user = request.user
        code = str(request.data.get("code") or "").strip()

        if not user.totp_secret:
            return Response(
                {"detail": "Primero inicia la configuración de 2FA."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not code:
            return Response(
                {"detail": "Ingresa el código de 6 dígitos."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not pyotp.TOTP(user.totp_secret).verify(code, valid_window=1):
            return Response(
                {"detail": "Código inválido o expirado. Intenta nuevamente."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.is_2fa_enabled = True
        user.save(update_fields=["is_2fa_enabled"])
        return Response(
            {"detail": "Autenticación en dos pasos activada.", "is_2fa_enabled": True},
            status=status.HTTP_200_OK,
        )


class TwoFADisableView(APIView):
    """POST /api/v1/accounts/me/2fa/disable/ {code} — desactiva 2FA.

    Exige un código válido (o, si el secreto ya se perdió, permite limpiar el
    estado) antes de desactivar, para que un tercero con la sesión abierta no la
    apague trivialmente.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        import pyotp

        user = request.user
        if not user.is_2fa_enabled:
            return Response(
                {"detail": "La autenticación en dos pasos no está activada."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        code = str(request.data.get("code") or "").strip()
        if not code or not pyotp.TOTP(user.totp_secret).verify(code, valid_window=1):
            return Response(
                {"detail": "Código inválido. No se desactivó 2FA."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.is_2fa_enabled = False
        user.totp_secret = ""
        user.save(update_fields=["is_2fa_enabled", "totp_secret"])
        return Response(
            {"detail": "Autenticación en dos pasos desactivada.", "is_2fa_enabled": False},
            status=status.HTTP_200_OK,
        )


class LeaderboardView(APIView):
    """GET /api/v1/accounts/leaderboard/?scope=global|escuela&limit=20

    Devuelve top-N usuarios por XP + el rank del usuario actual (si no entra
    en el top). admin: cualquier escuela; director: solo la suya;
    estudiante: global o su propia escuela.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .gamification import nivel_para_xp

        scope = request.query_params.get('scope', 'global')
        try:
            limit = min(100, max(1, int(request.query_params.get('limit', 20))))
        except (TypeError, ValueError):
            limit = 20

        qs = Usuario.objects.filter(is_estudiante=True, is_active=True)
        if scope == 'escuela':
            escuela_id = request.user.escuela_id
            if not escuela_id:
                return Response({"detail": "No perteneces a una escuela."}, status=status.HTTP_400_BAD_REQUEST)
            if is_director(request.user) or is_admin(request.user):
                escuela_param = request.query_params.get('escuela_id')
                if escuela_param and is_admin(request.user):
                    try:
                        escuela_id = int(escuela_param)
                    except (TypeError, ValueError):
                        return Response({"detail": "escuela_id inválido."}, status=400)
            qs = qs.filter(escuela_id=escuela_id)

        top = list(qs.order_by('-xp', 'id')[:limit])
        entries = []
        for idx, u in enumerate(top, start=1):
            entries.append({
                'rank': idx,
                'usuario_id': u.id,
                'nombre': u.nombre,
                'apellido': u.apellido,
                'avatar_url': u.avatar_url or '',
                'xp': u.xp,
                'level': nivel_para_xp(u.xp)['level'],
            })

        # Rank del usuario actual si quedó fuera del top
        mi_rank = None
        if not any(e['usuario_id'] == request.user.id for e in entries):
            mejor_xp = qs.filter(xp__gt=request.user.xp).count()
            mi_rank = mejor_xp + 1

        return Response({
            'scope': scope,
            'top': LeaderboardEntrySerializer(entries, many=True).data,
            'me': {
                'rank': mi_rank,
                'xp': request.user.xp,
                'level': nivel_para_xp(request.user.xp)['level'],
            },
        })


class PushTokensView(APIView):
    """POST /api/v1/accounts/me/push-token/    registra/actualiza token
    DELETE /api/v1/accounts/me/push-token/<token>/   elimina
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from .models import PushToken
        token = (request.data.get('token') or '').strip()
        platform = (request.data.get('platform') or 'expo').strip()
        if not token:
            return Response({"detail": "token requerido."}, status=status.HTTP_400_BAD_REQUEST)
        obj, _ = PushToken.objects.update_or_create(
            token=token,
            defaults={'usuario': request.user, 'platform': platform},
        )
        return Response(PushTokenSerializer(obj).data, status=status.HTTP_200_OK)


class PushTokenDeleteView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, token):
        from .models import PushToken
        deleted, _ = PushToken.objects.filter(usuario=request.user, token=token).delete()
        if not deleted:
            return Response({"detail": "Token no encontrado."}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


class NotificacionesView(ListAPIView):
    """GET /api/v1/accounts/me/notifications/?unread=true"""
    serializer_class = NotificacionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = self.request.user.notificaciones.all()
        if (self.request.query_params.get('unread') or '').lower() == 'true':
            qs = qs.filter(read_at__isnull=True)
        return qs


class NotificacionMarcarLeidaView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        from django.utils import timezone as _tz
        updated = request.user.notificaciones.filter(pk=pk, read_at__isnull=True).update(read_at=_tz.now())
        if not updated:
            return Response({"detail": "No encontrada o ya leída."}, status=status.HTTP_404_NOT_FOUND)
        return Response({"ok": True})


class NotificacionesMarcarTodasView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from django.utils import timezone as _tz
        n = request.user.notificaciones.filter(read_at__isnull=True).update(read_at=_tz.now())
        return Response({"marcadas": n})


class MeAchievementsView(APIView):
    """GET /api/v1/accounts/me/achievements/ — catálogo + flag earned."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .gamification import LOGROS_CATALOGO
        from .models import UsuarioLogro

        obtenidos = {
            ul.logro.slug: ul.obtenido_en
            for ul in UsuarioLogro.objects.filter(usuario=request.user).select_related('logro')
        }
        items = []
        for slug, meta in LOGROS_CATALOGO.items():
            items.append({
                'slug': slug,
                'nombre': meta['nombre'],
                'descripcion': meta['descripcion'],
                'icono': meta['icono'],
                'obtenido_en': obtenidos.get(slug),
                'earned': slug in obtenidos,
            })
        return Response(LogroSerializer(items, many=True).data)


class MisCertificadosView(ListAPIView):
    """GET /api/v1/accounts/me/certificates/ — certificados del estudiante."""
    serializer_class = CertificadoSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Certificado.objects.filter(estudiante=self.request.user).order_by('-fecha_emision', '-id')


class VerifyCertificadoView(APIView):
    """GET /api/v1/accounts/certificates/<codigo>/verify/ — público (QR scan)."""
    permission_classes = [permissions.AllowAny]

    def get(self, request, codigo):
        try:
            cert = Certificado.objects.select_related('estudiante', 'curso').get(codigo=codigo)
        except (Certificado.DoesNotExist, ValueError):
            return Response({"valid": False, "detail": "Código no encontrado."}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            "valid": True,
            **CertificadoPublicoSerializer(cert).data,
        })


class GenerarPruebaGratisView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        tipo = request.data.get('tipo')
        categoria_id = request.data.get('categoria_id')

        ejercicios, error = accounts_services.seleccionar_ejercicios(tipo, categoria_id)
        if error:
            return Response({"detail": error.detail}, status=error.status)

        return Response({
            "tipo": tipo.lower().strip(),
            "preguntas": accounts_services.serializar_preguntas_publicas(ejercicios),
            "total": len(ejercicios),
        }, status=status.HTTP_201_CREATED)


class ProgresoEstudiantesView(APIView):
    """Progreso agregado de estudiantes por escuela.

    Admin: cualquier escuela. Director: solo la suya. Estudiante: 403.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, escuela_id):
        if not is_admin(request.user):
            if not is_director(request.user):
                return Response({"detail": "No autorizado."}, status=status.HTTP_403_FORBIDDEN)
            if request.user.escuela_id != int(escuela_id):
                return Response({"detail": "No autorizado para esa escuela."}, status=status.HTTP_403_FORBIDDEN)

        curso_id = request.query_params.get("curso_id")
        try:
            curso_id_int = int(curso_id) if curso_id else None
        except (TypeError, ValueError):
            return Response({"detail": "curso_id inválido."}, status=status.HTTP_400_BAD_REQUEST)

        estudiantes_qs = Usuario.objects.filter(is_estudiante=True, escuela_id=escuela_id)
        if curso_id_int is not None:
            estudiantes_qs = estudiantes_qs.filter(
                id__in=EstudianteCurso.objects.filter(curso_id=curso_id_int).values("estudiante_id")
            )

        estudiante_ids = list(estudiantes_qs.values_list("id", flat=True))
        if not estudiante_ids:
            return Response([], status=status.HTTP_200_OK)

        # Cursos inscritos por estudiante (filtrados a curso_id si vino).
        ec_qs = EstudianteCurso.objects.filter(estudiante_id__in=estudiante_ids)
        if curso_id_int is not None:
            ec_qs = ec_qs.filter(curso_id=curso_id_int)

        cursos_por_estudiante = defaultdict(set)
        for est_pk, curso_pk in ec_qs.values_list("estudiante_id", "curso_id"):
            cursos_por_estudiante[est_pk].add(curso_pk)

        todos_cursos = {c for cs in cursos_por_estudiante.values() for c in cs}

        # Lecciones por curso → contar una sola vez.
        lecciones_por_curso = defaultdict(int)
        leccion_pk_por_curso = defaultdict(set)
        for curso_pk, leccion_pk in Leccion.objects.filter(curso_id__in=todos_cursos).values_list("curso_id", "id"):
            lecciones_por_curso[curso_pk] += 1
            leccion_pk_por_curso[curso_pk].add(leccion_pk)

        # Lecciones vistas distintas (estudiante, curso, leccion) en una sola consulta.
        vistas_qs = (
            EstudianteLeccion.objects
            .filter(estudiante_id__in=estudiante_ids, curso_id__in=todos_cursos)
            .values_list("estudiante_id", "curso_id", "leccion_id")
            .distinct()
        )
        vistas_por_estudiante_curso = defaultdict(set)
        for est_pk, curso_pk, leccion_pk in vistas_qs:
            vistas_por_estudiante_curso[(est_pk, curso_pk)].add(leccion_pk)

        # Pruebas totales / aprobadas en una sola consulta agregada.
        from django.db.models import Count, Q
        pruebas_agg = (
            Prueba.objects.filter(estudiante_id__in=estudiante_ids)
            .values("estudiante_id")
            .annotate(total=Count("id"), aprobadas=Count("id", filter=Q(aprobado=True)))
        )
        pruebas_por_estudiante = {p["estudiante_id"]: (p["total"], p["aprobadas"]) for p in pruebas_agg}

        respuesta = []
        for est in estudiantes_qs:
            cursos = cursos_por_estudiante.get(est.id, set())
            total_lecciones = sum(lecciones_por_curso.get(c, 0) for c in cursos)
            lecciones_vistas = sum(
                len(vistas_por_estudiante_curso.get((est.id, c), set()) & leccion_pk_por_curso.get(c, set()))
                for c in cursos
            )
            progreso_pct = round((lecciones_vistas / total_lecciones) * 100) if total_lecciones else 0

            total_p, aprobadas = pruebas_por_estudiante.get(est.id, (0, 0))
            aprobacion_pct = round((aprobadas / total_p) * 100) if total_p else 0

            respuesta.append({
                "id": est.id,
                "nombre": f"{est.nombre} {est.apellido}",
                "clasesVistas": progreso_pct,
                "aprobacionPruebas": aprobacion_pct,
            })

        return Response(respuesta, status=status.HTTP_200_OK)

        return Response(respuesta)
