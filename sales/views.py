from rest_framework import viewsets, status
from rest_framework.decorators import action
from .models import Producto, Venta, AccessKey, TransbankTransaction, Usuario, Escuela, Curso, EstudianteCurso
from .serializers import ProductoSerializer, VentaSerializer, AccessKeySerializer, EstudianteCursoSerializer, ActivarCursoSerializer, EstudianteCursoDetailSerializer
from rest_framework.response import Response
from transbank.error.transbank_error import TransbankError
from transbank.webpay.webpay_plus.transaction import Transaction
from transbank.common.options import WebpayOptions
from transbank.common.integration_commerce_codes import IntegrationCommerceCodes
from transbank.common.integration_api_keys import IntegrationApiKeys
from transbank.common.integration_type import IntegrationType
from rest_framework.views import APIView
from django.db import transaction as db_transaction
from django.http import HttpResponse
from .utils import extract_ids_from_buy_order, parse_accounting_date
from rest_framework.exceptions import ValidationError
from django_filters.rest_framework import DjangoFilterBackend
from .serializers import EstudianteCursosActivosSerializer, CursoDisponibleSerializer
from abc import ABC, abstractmethod
from rest_framework import permissions as drf_permissions
from accounts.permissions import (
    IsAdmin,
    IsDirector,
    ReadOnlyOrAdmin,
    is_admin,
    is_director,
    is_estudiante,
)
from .services import (
    asignar_llave_y_curso,
    canjear_access_key,
    CanjeError,
    registrar_venta_transbank,
    registrar_venta_unificada,
    precio_final_producto,
)


def _frontend_return_url():
    """URL de retorno de la pasarela, desde env (no hardcodear localhost).

    En producción FRONTEND_URL debe apuntar al dominio real; el default local
    solo aplica en desarrollo.
    """
    import os
    base = os.getenv('FRONTEND_URL', 'http://localhost:3000').rstrip('/')
    return f'{base}/pay_confirmation'


def _validar_monto_contra_producto(buy_order, amount):
    """Valida que `amount` coincida con el precio autoritativo del Producto.

    El primer id del buy_order (`order_<product_id>_<student_id>`) identifica el
    Producto. Devuelve (Response de error, None) si no valida, o (None, producto)
    si todo OK. Cierra el hueco de manipulación de precio en el cliente.
    """
    product_id, _ = extract_ids_from_buy_order(buy_order)
    if product_id is None:
        return Response(
            {"error": "buy_order tiene formato inválido. Esperado: order_<product_id>_<student_id>."},
            status=status.HTTP_400_BAD_REQUEST,
        ), None
    producto = Producto.objects.filter(id=product_id).first()
    if producto is None:
        # El flujo de compra individual de curso (product_id = curso) no está
        # soportado por el modelo de ventas; rechazamos ANTES de cobrar.
        return Response(
            {"error": "Producto no encontrado para esta compra."},
            status=status.HTTP_404_NOT_FOUND,
        ), None
    esperado = precio_final_producto(producto)
    if int(round(float(amount))) != esperado:
        return Response(
            {"error": "El monto no coincide con el precio del producto.",
             "expected": esperado},
            status=status.HTTP_400_BAD_REQUEST,
        ), None
    return None, producto


def _scope_estudiante_curso(qs, user):
    """admin: todo; director: estudiantes de su escuela; estudiante: solo propios."""
    if is_admin(user):
        return qs
    if is_director(user):
        return qs.filter(estudiante_id__escuela_id=user.escuela_id)
    if is_estudiante(user):
        return qs.filter(estudiante_id=user)
    return qs.none()


def _enforce_payment_ownership(request, claimed_user_id):
    """Verifica que el user_id reclamado en el payload sea el del request.

    Admin puede iniciar/confirmar pagos para cualquier usuario. Cualquier
    otro rol debe coincidir con request.user.id — bloquea que A pague
    falsificando user_id=B.

    Returns Response (403) si falla, None si pasa.
    """
    if is_admin(request.user):
        return None
    try:
        claimed = int(claimed_user_id)
    except (TypeError, ValueError):
        return Response(
            {"error": "user_id inválido en payload."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if claimed != request.user.id:
        return Response(
            {"error": "No puedes iniciar/confirmar pagos para otro usuario."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


class ProductoViewSet(viewsets.ModelViewSet):
    """Catálogo de productos — lectura para todos los autenticados (tienda), escritura admin."""
    queryset = Producto.objects.all()
    serializer_class = ProductoSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['cant_basic_key', 'cant_professional_key', 'basic_access', 'professional_access']
    permission_classes = [ReadOnlyOrAdmin]


def _generar_comprobante_pdf(venta):
    """Genera un PDF de comprobante de pago (sin validez tributaria)."""
    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    x = 25 * mm
    right = W - 25 * mm
    y = H - 30 * mm

    # Encabezado
    c.setFillColorRGB(0.06, 0.06, 0.06)
    c.setFont("Helvetica-Bold", 22)
    c.drawString(x, y, "AutoTest")
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawRightString(right, y, "Comprobante de pago")
    c.drawRightString(right, y - 14, f"N° {venta.id}")
    y -= 34
    c.setStrokeColorRGB(0.85, 0.85, 0.85)
    c.line(x, y, right, y)
    y -= 24

    def row(label, value):
        nonlocal y
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawString(x, y, label)
        c.setFont("Helvetica-Bold", 11)
        c.setFillColorRGB(0.1, 0.1, 0.1)
        c.drawString(x, y - 14, str(value))
        y -= 32

    fecha = venta.fecha_venta.strftime("%d-%m-%Y %H:%M") if venta.fecha_venta else "—"
    escuela = venta.escuela.nombre if venta.escuela else "—"
    if venta.usuario:
        cliente = f"{venta.usuario.nombre or ''} {venta.usuario.apellido or ''}".strip() or venta.usuario.email
    else:
        cliente = "—"
    producto = venta.producto.nombre if venta.producto else "—"
    currency = getattr(venta.producto, "currency", "CLP") if venta.producto else "CLP"
    try:
        monto = f"{currency} ${venta.monto_pagado:,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        monto = f"{currency} ${venta.monto_pagado}"

    row("Fecha", fecha)
    row("Escuela", escuela)
    row("Cliente", cliente)
    row("Producto", producto)
    row("Método de pago", venta.pay_system or "—")
    row("Estado", venta.payment_status or "—")

    tx = getattr(venta, "transbank_transaction", None)
    if tx:
        if tx.buy_order:
            row("Orden de compra", tx.buy_order)
        if tx.status:
            row("Estado de la transacción", tx.status)

    # Total destacado
    y -= 6
    c.line(x, y, right, y)
    y -= 26
    c.setFont("Helvetica-Bold", 14)
    c.setFillColorRGB(0.1, 0.1, 0.1)
    c.drawString(x, y, "Total pagado")
    c.drawRightString(right, y, monto)
    y -= 44

    # Nota legal (honesta): no es un DTE.
    c.setFont("Helvetica-Oblique", 8)
    c.setFillColorRGB(0.5, 0.5, 0.5)
    c.drawString(x, y, "Comprobante de pago sin validez tributaria: no constituye boleta ni factura")
    c.drawString(x, y - 11, "electrónica (SII). Emitido automáticamente por AutoTest.")

    c.showPage()
    c.save()
    return buf.getvalue()


class VentaViewSet(viewsets.ModelViewSet):
    """admin: todo; director: ventas asociadas a su escuela; estudiante: sus propias compras."""
    queryset = Venta.objects.all()
    serializer_class = VentaSerializer
    permission_classes = [drf_permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if is_admin(user):
            return Venta.objects.all()
        if is_director(user):
            return Venta.objects.filter(escuela_id=user.escuela_id)
        if is_estudiante(user):
            return Venta.objects.filter(usuario=user)
        return Venta.objects.none()

    @action(detail=True, methods=["get"])
    def comprobante(self, request, pk=None):
        """GET /api/v1/sales/ventas/<id>/comprobante/ → PDF de comprobante.

        `get_object()` aplica el scoping de `get_queryset`: un director solo
        accede a ventas de su escuela; un estudiante a las suyas; en otro caso 404.
        """
        venta = self.get_object()
        pdf = _generar_comprobante_pdf(venta)
        resp = HttpResponse(pdf, content_type="application/pdf")
        resp["Content-Disposition"] = f'attachment; filename="comprobante_{venta.id}.pdf"'
        return resp


class AccessKeyViewSet(viewsets.ModelViewSet):
    """admin: full; director: solo llaves derivadas de sus ventas; estudiante: 403.

    Una llave es 'del director' si existe una Venta del director cuyo flujo
    haya generado un EstudianteCurso con esta llave.
    """
    queryset = AccessKey.objects.all()
    serializer_class = AccessKeySerializer
    # Lectura: autenticado (scopeada por get_queryset). Escritura: solo admin.
    # Los directores NO crean/editan llaves por esta vía genérica —usan los
    # flujos con reglas de negocio (activar_curso / revocar_llave / extender)—;
    # exponer create/update/delete aquí permitiría otorgar acceso sin consumir
    # saldo ni pasar por la pasarela de pago.
    permission_classes = [ReadOnlyOrAdmin]

    def get_queryset(self):
        user = self.request.user
        if is_admin(user):
            return AccessKey.objects.all()
        if is_director(user):
            return AccessKey.objects.filter(
                estudiantecurso__estudiante_id__escuela_id=user.escuela_id
            ).distinct()
        return AccessKey.objects.none()


class EstudianteCursoViewSet(viewsets.ModelViewSet):
    queryset = EstudianteCurso.objects.all()
    serializer_class = EstudianteCursoSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['estudiante_id', 'curso_id']
    # Lectura: autenticado (scopeada por get_queryset). Escritura: solo admin.
    # Crear inscripciones directas saltaría el consumo de llaves/cupos; los
    # directores inscriben vía activar_curso.
    permission_classes = [ReadOnlyOrAdmin]

    def get_queryset(self):
        return _scope_estudiante_curso(EstudianteCurso.objects.all(), self.request.user)


class EstudianteCursoDetailViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = EstudianteCurso.objects.all()
    serializer_class = EstudianteCursoDetailSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['estudiante_id', 'curso_id']
    permission_classes = [drf_permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = _scope_estudiante_curso(EstudianteCurso.objects.all(), self.request.user)
        escuela_id = self.request.query_params.get("escuela")

        if escuela_id is not None:
            try:
                escuela_id = int(escuela_id)
                queryset = queryset.filter(estudiante_id__escuela=escuela_id)
            except ValueError:
                return queryset.none()

        return queryset

class EstudiantesCursosActivosPorEscuelaView(APIView):
    """GET /escuelas/<id>/estudiantes-cursos/ — admin o director de esa escuela."""
    permission_classes = [drf_permissions.IsAuthenticated]

    def get(self, request, escuela_id):
        if not is_admin(request.user):
            if not is_director(request.user) or request.user.escuela_id != escuela_id:
                return Response({"detail": "No autorizado."}, status=status.HTTP_403_FORBIDDEN)

        # Prefetch los EstudianteCurso del estudiante con sus FK ya cargadas.
        # Reduce O(n) → O(1) en consultas para `get_registros`.
        from django.db.models import Prefetch

        registros_qs = (
            EstudianteCurso.objects
            .select_related('estudiante_id', 'curso_id', 'access_key_id')
        )
        estudiantes = (
            Usuario.objects
            .filter(escuela_id=escuela_id, is_estudiante=True)
            .prefetch_related(Prefetch('estudiantecurso_set', queryset=registros_qs, to_attr='_prefetched_registros'))
            .order_by('apellido', 'nombre')
        )

        serializer = EstudianteCursosActivosSerializer(
            estudiantes,
            many=True,
            context={'request': request, 'escuela_id': escuela_id}
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

class CursosDisponiblesParaUsuarioView(APIView):
    """Tienda — un usuario puede ver SU propio listado de cursos disponibles.

    admin: cualquiera; director: usuarios de su escuela; estudiante: solo
    si user_id == self.pk.
    """
    permission_classes = [drf_permissions.IsAuthenticated]

    def get(self, request, escuela_id, user_id):
        if not is_admin(request.user):
            if is_director(request.user):
                if request.user.escuela_id != escuela_id:
                    return Response({"detail": "No autorizado."}, status=status.HTTP_403_FORBIDDEN)
            elif is_estudiante(request.user):
                if request.user.pk != user_id:
                    return Response({"detail": "No autorizado."}, status=status.HTTP_403_FORBIDDEN)
            else:
                return Response({"detail": "No autorizado."}, status=status.HTTP_403_FORBIDDEN)

        # Obtener usuario y escuela
        try:
            user = Usuario.objects.get(id=user_id)
        except Usuario.DoesNotExist:
            return Response({"error": "Usuario no encontrado"}, status=status.HTTP_404_NOT_FOUND)

        try:
            escuela = Escuela.objects.get(id=escuela_id)
        except Escuela.DoesNotExist:
            return Response({"error": "Escuela no encontrada"}, status=status.HTTP_404_NOT_FOUND)

        # IDs de cursos que el usuario ya tiene
        owned_ids = set(
            EstudianteCurso.objects.filter(estudiante_id=user)
            .values_list("curso_id", flat=True)
        )

        # Todos los cursos del sistema (si luego los quieres por escuela, aquí se filtra).
        # Anotamos cantidad_lecciones para que el cliente calcule progreso sin
        # un fetch extra por curso (Leccion.curso es FK sin related_name → 'leccion').
        from django.db.models import Count
        cursos = Curso.objects.annotate(cantidad_lecciones=Count('leccion'))

        serializer = CursoDisponibleSerializer(
            cursos,
            many=True,
            context={
                "owned_ids": owned_ids,
                "escuela": escuela,
            },
        )

        return Response(serializer.data, status=status.HTTP_200_OK)

class CanjearLlaveView(APIView):
    """POST /api/v1/sales/canjear_llave/ — estudiante canjea su llave por inscripción.

    Body: {"access_key": "ABC123XYZ", "curso_id": 5}
    """
    permission_classes = [drf_permissions.IsAuthenticated]

    def post(self, request):
        key_str = (request.data.get('access_key') or '').strip()
        curso_id = request.data.get('curso_id')
        if not curso_id:
            return Response({"error": "curso_id requerido."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            inscripcion = canjear_access_key(request.user, key_str, int(curso_id))
        except CanjeError as e:
            code = e.code
            http_status = {
                'missing_key': status.HTTP_400_BAD_REQUEST,
                'curso_not_found': status.HTTP_404_NOT_FOUND,
                'key_not_found': status.HTTP_404_NOT_FOUND,
                'key_inactive': status.HTTP_400_BAD_REQUEST,
                'key_expired': status.HTTP_400_BAD_REQUEST,
                'already_enrolled': status.HTTP_409_CONFLICT,
            }.get(code, status.HTTP_400_BAD_REQUEST)
            return Response({"error": str(e), "code": code}, status=http_status)
        except (TypeError, ValueError):
            return Response({"error": "curso_id inválido."}, status=status.HTTP_400_BAD_REQUEST)

        # Notificar
        try:
            from accounts.notifications import notificar
            notificar(
                request.user, tipo='test_passed',  # reuso slug "actividad"; placeholder
                titulo='Curso activado',
                mensaje=f'Tu acceso al curso {inscripcion.curso_id.nombre} fue activado.',
                data={'curso_id': inscripcion.curso_id_id},
            )
        except Exception:
            pass  # notificación es best-effort, no debe romper canje

        return Response({
            "message": "Inscripción creada.",
            "estudiante_curso_id": inscripcion.id,
            "curso_id": inscripcion.curso_id_id,
        }, status=status.HTTP_201_CREATED)


class ActivarCursoView(APIView):
    """Activación manual de curso para un estudiante.

    Body:
      user_id: int
      curso_id: int
      days: int (opcional, default 30) — solo aplica a source='key'
      source: 'key' | 'seat' | 'auto' (opcional, default 'auto')

    Reglas por rol:
    - admin: activa sin descontar saldo (source='key' crea AccessKey
      temporal con expiración; source='seat' crea AccessKey sin expiración
      pero NO descuenta seats — se asume decisión administrativa).
    - director:
        * source='auto' (default): usa seat si suscripción activa y quedan
          cupos; sino usa key si hay saldo; sino 400.
        * source='seat': falla si sin suscripción o sin cupos.
        * source='key': falla si sin llaves.
      Descuenta atómicamente con select_for_update.
    - estudiante: 403.

    Response 201:
      { message, access_key, valid_until, origen }
    """
    serializer_class = ActivarCursoSerializer
    permission_classes = [drf_permissions.IsAuthenticated]

    def post(self, request):
        if is_estudiante(request.user):
            return Response({"detail": "No autorizado."}, status=status.HTTP_403_FORBIDDEN)
        if not (is_admin(request.user) or is_director(request.user)):
            return Response({"detail": "No autorizado."}, status=status.HTTP_403_FORBIDDEN)

        user_id = request.data.get("user_id")
        curso_id = request.data.get("curso_id")
        days = request.data.get("days")
        source = (request.data.get("source") or "auto").lower().strip()
        if source not in ("auto", "key", "seat"):
            return Response({"error": "source debe ser 'auto', 'key' o 'seat'."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = Usuario.objects.get(id=user_id)
            curso = Curso.objects.get(id=curso_id)
            days = int(days) if days else 30
        except Curso.DoesNotExist:
            return Response({"error": "Curso no encontrado."}, status=status.HTTP_404_NOT_FOUND)
        except Usuario.DoesNotExist:
            return Response({"error": "Usuario no encontrado."}, status=status.HTTP_404_NOT_FOUND)

        # Admin sin escuela objetivo: siempre usa key (no toca saldos).
        if is_admin(request.user):
            resolved_source = "seat" if source == "seat" else "key"
            access_key = _asignar_por_source(user, curso, days, resolved_source, decrement_escuela=None)
            return Response({
                "message": "Clave activada con éxito.",
                "access_key": access_key.key,
                "valid_until": access_key.valid_until,
                "origen": access_key.origen,
            }, status=status.HTTP_201_CREATED)

        # Director: valida escuela + saldo/cupos con lock.
        if user.escuela_id != request.user.escuela_id:
            return Response(
                {"error": "Solo puedes activar cursos para estudiantes de tu escuela."},
                status=status.HTTP_403_FORBIDDEN,
            )

        is_pro = bool(curso.is_profesional)
        with db_transaction.atomic():
            escuela = Escuela.objects.select_for_update().get(pk=request.user.escuela_id)
            resolved_source = _resolver_source_director(escuela, is_pro, source)
            if resolved_source is None:
                return Response(
                    {"error": _mensaje_sin_saldo(is_pro, source)},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            _decrementar_saldo(escuela, is_pro, resolved_source)
            escuela.save()
            access_key = _asignar_por_source(user, curso, days, resolved_source, decrement_escuela=None)

        return Response({
            "message": "Clave activada con éxito.",
            "access_key": access_key.key,
            "valid_until": access_key.valid_until,
            "origen": access_key.origen,
        }, status=status.HTTP_201_CREATED)


# ---- helpers para activación con seat/key ----

def _resolver_source_director(escuela, is_pro, source):
    """Devuelve 'seat' | 'key' | None según disponibilidad en la escuela."""
    if source == "seat":
        return "seat" if _tiene_seat(escuela, is_pro) else None
    if source == "key":
        return "key" if _tiene_key(escuela, is_pro) else None
    # auto: seat primero (más barato), luego key.
    if _tiene_seat(escuela, is_pro):
        return "seat"
    if _tiene_key(escuela, is_pro):
        return "key"
    return None


def _tiene_seat(escuela, is_pro):
    if is_pro:
        return escuela.professional_access and escuela.professional_seats_used < escuela.professional_seats_max
    return escuela.basic_access and escuela.basic_seats_used < escuela.basic_seats_max


def _tiene_key(escuela, is_pro):
    return (escuela.professional_key if is_pro else escuela.basic_key) > 0


def _decrementar_saldo(escuela, is_pro, resolved_source):
    if resolved_source == "seat":
        if is_pro:
            escuela.professional_seats_used += 1
        else:
            escuela.basic_seats_used += 1
    else:  # key
        if is_pro:
            escuela.professional_key -= 1
        else:
            escuela.basic_key -= 1


def _mensaje_sin_saldo(is_pro, source):
    tier = "profesional" if is_pro else "básico"
    if source == "seat":
        return f"Tu escuela no tiene cupos {tier}es en la suscripción."
    if source == "key":
        return f"Tu escuela no tiene llaves {tier}es disponibles."
    return f"Tu escuela no tiene ni cupos ni llaves {tier}es disponibles."


def _asignar_por_source(estudiante, curso, days, resolved_source, decrement_escuela=None):
    """Crea AccessKey + EstudianteCurso según el origen."""
    from django.utils import timezone
    from datetime import timedelta
    with db_transaction.atomic():
        if resolved_source == "seat":
            access_key = AccessKey.objects.create(
                valid_until=None,
                origen="seat",
            )
        else:
            access_key = AccessKey.objects.create(
                valid_until=timezone.now() + timedelta(days=days),
                origen="key",
            )
        EstudianteCurso.objects.create(
            estudiante_id=estudiante,
            curso_id=curso,
            access_key_id=access_key,
        )
    return access_key

class ExtenderLlaveView(APIView):
    """Extiende la fecha de validez de una `AccessKey` existente.

    Reglas:
    - admin: puede extender cualquier llave sin tocar saldo.
    - director: solo llaves de estudiantes de su escuela, y consume **1**
      llave del saldo (`basic_key` o `professional_key` según el curso
      asociado a la llave a través de `EstudianteCurso`). Atómico con
      `select_for_update` para evitar carreras.
    - estudiante: 403.

    Body:
      access_key_id: UUID o id de la AccessKey
      days: int (días a sumar; si la llave ya expiró, se cuenta desde hoy)
    """
    permission_classes = [drf_permissions.IsAuthenticated]

    def post(self, request):
        if is_estudiante(request.user) or not (is_admin(request.user) or is_director(request.user)):
            return Response({"detail": "No autorizado."}, status=status.HTTP_403_FORBIDDEN)

        access_key_id = request.data.get("access_key_id")
        days = request.data.get("days")
        if not access_key_id:
            return Response({"error": "access_key_id requerido."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            days = int(days) if days else 30
            if days <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return Response({"error": "days debe ser un entero positivo."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            access_key = AccessKey.objects.get(pk=access_key_id)
        except (AccessKey.DoesNotExist, ValueError):
            return Response({"error": "Llave no encontrada."}, status=status.HTTP_404_NOT_FOUND)

        # Los seats no tienen expiración: no pueden extenderse.
        if access_key.origen == "seat":
            return Response(
                {"error": "Los cupos de suscripción no tienen expiración; extender no aplica."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Necesitamos saber el curso asociado para decidir basic vs professional.
        inscripcion = EstudianteCurso.objects.select_related("curso_id", "estudiante_id").filter(
            access_key_id=access_key,
        ).first()
        if inscripcion is None:
            return Response({"error": "La llave no está asociada a una inscripción."}, status=status.HTTP_400_BAD_REQUEST)

        curso = inscripcion.curso_id
        estudiante = inscripcion.estudiante_id

        if is_director(request.user):
            if estudiante.escuela_id != request.user.escuela_id:
                return Response(
                    {"error": "Solo puedes extender llaves de estudiantes de tu escuela."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        from django.utils import timezone
        from datetime import timedelta

        with db_transaction.atomic():
            if is_director(request.user):
                escuela_locked = Escuela.objects.select_for_update().get(pk=request.user.escuela_id)
                if curso.is_profesional:
                    if escuela_locked.professional_key <= 0:
                        return Response(
                            {"error": "Tu escuela no tiene llaves profesionales disponibles."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    escuela_locked.professional_key -= 1
                else:
                    if escuela_locked.basic_key <= 0:
                        return Response(
                            {"error": "Tu escuela no tiene llaves básicas disponibles."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    escuela_locked.basic_key -= 1
                escuela_locked.save()

            now = timezone.now()
            if access_key.valid_until and access_key.valid_until >= now:
                # Aún activa: sumar a la fecha actual de expiración.
                access_key.valid_until = access_key.valid_until + timedelta(days=days)
            else:
                # Expirada: reiniciar desde hoy.
                access_key.valid_from = now
                access_key.valid_until = now + timedelta(days=days)
                access_key.status = "active"
            access_key.save()

        return Response({
            "message": "Llave extendida con éxito.",
            "access_key_id": str(access_key.id),
            "access_key": access_key.key,
            "valid_from": access_key.valid_from,
            "valid_until": access_key.valid_until,
            "status": access_key.status,
        }, status=status.HTTP_200_OK)


class RevocarLlaveView(APIView):
    """POST /api/v1/sales/revocar_llave/ {access_key_id}

    Revoca una AccessKey (status='revoked'), retirando el acceso del estudiante
    al curso. NO reembolsa el saldo de la escuela (la llave ya fue consumida).
    Si la llave es de origen 'seat', libera además el cupo (decrementa
    *_seats_used) para que vuelva al pool disponible.

    Permisos:
    - admin: cualquier llave.
    - director: solo llaves de estudiantes de su escuela.
    - estudiante: 403.
    """
    permission_classes = [drf_permissions.IsAuthenticated]

    def post(self, request):
        from django.core.exceptions import ValidationError as DjangoValidationError

        if is_estudiante(request.user) or not (is_admin(request.user) or is_director(request.user)):
            return Response({"detail": "No autorizado."}, status=status.HTTP_403_FORBIDDEN)

        access_key_id = request.data.get("access_key_id")
        if not access_key_id:
            return Response({"error": "access_key_id requerido."}, status=status.HTTP_400_BAD_REQUEST)

        with db_transaction.atomic():
            try:
                access_key = AccessKey.objects.select_for_update().get(pk=access_key_id)
            except (AccessKey.DoesNotExist, ValueError, DjangoValidationError):
                return Response({"error": "Llave no encontrada."}, status=status.HTTP_404_NOT_FOUND)

            inscripcion = (
                EstudianteCurso.objects
                .select_related("curso_id", "estudiante_id")
                .filter(access_key_id=access_key)
                .first()
            )

            # Scope director: solo llaves de estudiantes de su escuela.
            if is_director(request.user):
                est = inscripcion.estudiante_id if inscripcion else None
                if est is None or est.escuela_id != request.user.escuela_id:
                    return Response(
                        {"error": "Solo puedes revocar llaves de estudiantes de tu escuela."},
                        status=status.HTTP_403_FORBIDDEN,
                    )

            if access_key.status == "revoked":
                return Response(
                    {"status": "already_revoked", "access_key_id": str(access_key.id)},
                    status=status.HTTP_200_OK,
                )

            # Si ocupaba un cupo de suscripción, devolverlo al pool.
            if access_key.origen == "seat" and inscripcion is not None and inscripcion.estudiante_id.escuela_id:
                escuela = Escuela.objects.select_for_update().get(pk=inscripcion.estudiante_id.escuela_id)
                if inscripcion.curso_id.is_profesional:
                    if escuela.professional_seats_used > 0:
                        escuela.professional_seats_used -= 1
                else:
                    if escuela.basic_seats_used > 0:
                        escuela.basic_seats_used -= 1
                escuela.save()

            access_key.status = "revoked"
            access_key.save(update_fields=["status"])

        return Response(
            {"status": "revoked", "access_key_id": str(access_key.id)},
            status=status.HTTP_200_OK,
        )


#Solo TransBank
class SaleInitiationViewSet(APIView):
    permission_classes = [drf_permissions.IsAuthenticated]

    def post(self, request):
        amount, session_id, buy_order = self.validate_payment_request(request.data)

        # buy_order es 'order_{course_id}_{student_id}'. Validamos que el
        # student_id parseado sea el del request.user (excepto admin).
        _, student_id = extract_ids_from_buy_order(buy_order)
        if student_id is None:
            return Response(
                {"error": "buy_order tiene formato inválido. Esperado: order_<course_id>_<student_id>."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        owner_check = _enforce_payment_ownership(request, student_id)
        if owner_check is not None:
            return owner_check

        # Anti price-tampering (mismo criterio que el flujo unificado).
        price_err, _producto = _validar_monto_contra_producto(buy_order, amount)
        if price_err is not None:
            return price_err

        return_url = _frontend_return_url()

        options = WebpayOptions(IntegrationCommerceCodes.WEBPAY_PLUS, IntegrationApiKeys.WEBPAY, IntegrationType.TEST)
        tx = Transaction(options)

        try:
            response = tx.create(buy_order, session_id, amount, return_url)
            return Response({'url': response['url'], 'token': response['token']}, status=status.HTTP_200_OK)
        except TransbankError as e:
            error_message = str(e)
            return Response({"error": error_message}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def validate_payment_request(self, data):
        amount = data.get('amount')
        session_id = data.get('session_id')
        buy_order = data.get('buy_order')

        if not amount or not isinstance(amount, (int, float)):
            raise ValidationError("Amount is required and must be a valid number.")
        if not session_id or not isinstance(session_id, str):
            raise ValidationError("Session ID is required and must be a valid string.")
        if not buy_order or not isinstance(buy_order, str):
            raise ValidationError("Buy Order is required and must be a valid string.")
        
        return amount, session_id, buy_order
        
class PaymentConfirmationView(APIView):
    permission_classes = [drf_permissions.IsAuthenticated]

    def post(self, request):
        token_ws, product_id, user_id = self.validate_payment_request(request.data)

        owner_check = _enforce_payment_ownership(request, user_id)
        if owner_check is not None:
            return owner_check

        if TransbankTransaction.objects.filter(token=token_ws).exists():
                return Response({
                    "success": False,
                    'details': 'Esta transacción ya fue procesada.'
                }, status=400)
        
        options = WebpayOptions(IntegrationCommerceCodes.WEBPAY_PLUS, IntegrationApiKeys.WEBPAY, IntegrationType.TEST)
        tx = Transaction(options)
        try:
            result = tx.commit(token_ws)
        except TransbankError as e:
            return Response({"success": False, "message": f"Error en la transacción: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

        if result['status'] != 'AUTHORIZED':
            return Response({"success": False, "details": "Pago no autorizado."}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            user = Usuario.objects.get(id=user_id)
            producto = Producto.objects.get(id=product_id)
            escuela = user.escuela  # nullable; FK ya resuelve
        except (Usuario.DoesNotExist, Producto.DoesNotExist) as e:
            return Response({"success": False, 'details': f'Recursos no encontrados: {str(e)}'}, status=400)

        try:
            registrar_venta_transbank(
                user=user,
                producto=producto,
                escuela=escuela,
                result=result,
                token_ws=token_ws,
                fecha_venta=parse_accounting_date(result['accounting_date']),
            )
        except Exception as e:
            return Response({
                "success": False,
                'details': f'Error al guardar la venta o la transaccion: {str(e)}'
            }, status=500)

        return Response({"success": True, "details": "Venta creada con éxito.", "data": result}, status=status.HTTP_201_CREATED)

    def validate_payment_request(self, data):
        token_ws = data.get('token_ws')
        product_id = data.get('product_id')
        user_id = data.get('user_id')

        if not product_id or not isinstance(product_id, (int, float)):
            raise ValidationError("product_id is required and must be a valid number.")
        if not token_ws or not isinstance(token_ws, str):
            raise ValidationError("token_ws is required and must be a valid string.")
        if not user_id or not isinstance(user_id, (int, float)):
            raise ValidationError("user_id is required and must be a valid number.")
        
        return token_ws, product_id, user_id

#Pago unificado
class PaymentStrategy(ABC):
    @abstractmethod
    def create_transaction(self, amount, buy_order, session_id) -> dict:
        pass

    @abstractmethod
    def confirm_transaction(self, request_data) -> dict:
        pass

class TransbankPaymentStrategy(PaymentStrategy):
    def create_transaction(self, amount, buy_order, session_id):
        options = WebpayOptions(
            IntegrationCommerceCodes.WEBPAY_PLUS,
            IntegrationApiKeys.WEBPAY,
            IntegrationType.TEST
        )
        tx = Transaction(options)
        return_url = _frontend_return_url()
        response = tx.create(buy_order, session_id, amount, return_url)

        return {
            "url": response["url"],
            "token": response["token"],
            "payment_method": "transbank"
        }

    def confirm_transaction(self, request_data):
        token, product_id, user_id = self.validate_payment_request(request_data)
        if TransbankTransaction.objects.filter(token=token).exists():
            return {
                "success": False,
                "message": "Esta transacción ya fue procesada."
            }

        options = WebpayOptions(
            IntegrationCommerceCodes.WEBPAY_PLUS,
            IntegrationApiKeys.WEBPAY,
            IntegrationType.TEST
        )
        tx = Transaction(options)
        result = tx.commit(token)

        if result["status"] == "AUTHORIZED":
            return {
                "success": True,
                "user_id": user_id,
                "product_id": product_id,
                "amount": result["amount"],
                "status": result["status"],
                "transaction_date": result["transaction_date"],
                "extra_data": {
                    "token": token,
                    "buy_order": result["buy_order"],
                    "payment_type_code": result["payment_type_code"]
                }
            }
        else:
            return {
                "success": False,
                "message": "Pago no autorizado."
            }

    def validate_payment_request(self, data):
        token = data.get('token_ws')
        product_id = data.get('product_id')
        user_id = data.get('user_id')

        if not token or not isinstance(token, str):
            raise ValidationError("token_ws is required and must be a valid string.")
        if not product_id or not isinstance(product_id, (int, float)):
            raise ValidationError("product_id is required and must be a valid number.")
        if not user_id or not isinstance(user_id, (int, float)):
            raise ValidationError("user_id is required and must be a valid number.")

        return token, product_id, user_id

class MercadoPagoPaymentStrategy(PaymentStrategy):
    """Stub. La integración real con MercadoPago aún no está implementada.

    Para evitar conceder acceso gratis por accidente, ambos métodos lanzan
    NotImplementedError hasta que se cablee el SDK oficial de MercadoPago
    (verificación de firma del webhook, consulta de payment status, etc.).
    """

    def create_transaction(self, amount, buy_order, session_id):
        raise NotImplementedError(
            "MercadoPago no está implementado. Integrar SDK oficial antes de habilitar."
        )

    def confirm_transaction(self, request_data):
        raise NotImplementedError(
            "MercadoPago no está implementado. Integrar SDK oficial antes de habilitar."
        )

class UnifiedSaleInitiationView(APIView):
    permission_classes = [drf_permissions.IsAuthenticated]

    def post(self, request):
        try:
            amount, session_id, buy_order, method = self.validate_payment_request(request.data)

            _, student_id = extract_ids_from_buy_order(buy_order)
            if student_id is None:
                return Response(
                    {"error": "buy_order tiene formato inválido. Esperado: order_<product_id>_<student_id>."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            owner_check = _enforce_payment_ownership(request, student_id)
            if owner_check is not None:
                return owner_check

            # Anti price-tampering: el monto debe coincidir con el precio
            # autoritativo del producto (no confiar en el `amount` del cliente).
            price_err, _producto = _validar_monto_contra_producto(buy_order, amount)
            if price_err is not None:
                return price_err

            strategy = self.get_payment_strategy(method)
            payment_data = strategy.create_transaction(amount, buy_order, session_id)
            return Response(payment_data, status=status.HTTP_200_OK)
        except NotImplementedError as e:
            return Response({"error": str(e)}, status=status.HTTP_501_NOT_IMPLEMENTED)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    def validate_payment_request(self, data):
        amount = data.get('amount')
        session_id = data.get('session_id')
        buy_order = data.get('buy_order')
        method = data.get('payment_method')

        if not amount or not isinstance(amount, (int, float)):
            raise ValidationError("Amount is required and must be a valid number.")
        if not session_id or not isinstance(session_id, str):
            raise ValidationError("Session ID is required and must be a valid string.")
        if not buy_order or not isinstance(buy_order, str):
            raise ValidationError("Buy Order is required and must be a valid string.")
        if not method or not isinstance(method, str):
            raise ValidationError("Payment method is required and must be a valid string.")

        return amount, session_id, buy_order, method

    def get_payment_strategy(self, method_name: str) -> PaymentStrategy:
        if method_name == "transbank":
            return TransbankPaymentStrategy()
        elif method_name == "mercadopago":
            return MercadoPagoPaymentStrategy()
        raise ValueError("Método de pago no soportado")

class UnifiedPaymentConfirmationView(APIView):
    permission_classes = [drf_permissions.IsAuthenticated]

    def post(self, request):
        try:
            owner_check = _enforce_payment_ownership(request, request.data.get("user_id"))
            if owner_check is not None:
                return owner_check

            method = request.data.get("payment_method")
            strategy = self.get_payment_strategy(method)

            try:
                result = strategy.confirm_transaction(request.data)
            except NotImplementedError as e:
                return Response({"error": str(e)}, status=status.HTTP_501_NOT_IMPLEMENTED)

            if not result["success"]:
                return Response({"success": False, "details": result.get("message", "Pago no autorizado.")}, status=status.HTTP_401_UNAUTHORIZED)

            user = Usuario.objects.get(id=result["user_id"])
            producto = Producto.objects.get(id=result["product_id"])
            escuela = user.escuela

            registrar_venta_unificada(
                user=user,
                producto=producto,
                escuela=escuela,
                method=method,
                result=result,
                fecha_venta=parse_accounting_date(result["transaction_date"]),
            )

            return Response({"success": True, "details": "Venta completada.", "data": result}, status=status.HTTP_201_CREATED)

        except Exception as e:
            return Response({"success": False, "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def get_payment_strategy(self, method_name: str) -> PaymentStrategy:
        if method_name == "transbank":
            return TransbankPaymentStrategy()
        elif method_name == "mercadopago":
            return MercadoPagoPaymentStrategy()
        raise ValueError("Método de pago no soportado")


# ============================================================
# Gestión de suscripción (cupos)
# ============================================================

class SubscriptionStatusView(APIView):
    """GET /api/v1/schools/<school_id>/subscription-status/

    Resumen de la licencia de la escuela: llaves + suscripciones (con cupos
    y disponibilidad). Auth: admin (cualquier escuela) o director (solo la suya).
    """
    permission_classes = [drf_permissions.IsAuthenticated]

    def get(self, request, school_id):
        try:
            school_id = int(school_id)
        except (TypeError, ValueError):
            return Response({"detail": "school_id inválido."}, status=status.HTTP_400_BAD_REQUEST)

        if not is_admin(request.user):
            if not is_director(request.user) or request.user.escuela_id != school_id:
                return Response({"detail": "No autorizado."}, status=status.HTTP_403_FORBIDDEN)

        try:
            escuela = Escuela.objects.get(pk=school_id)
        except Escuela.DoesNotExist:
            return Response({"detail": "Escuela no encontrada."}, status=status.HTTP_404_NOT_FOUND)

        def tier(access, seats_used, seats_max, keys):
            return {
                "access": bool(access),
                "seats_max": int(seats_max),
                "seats_used": int(seats_used),
                "seats_available": max(0, int(seats_max) - int(seats_used)),
                "keys_available": int(keys),
            }

        return Response({
            "escuela_id": escuela.id,
            "basic": tier(
                escuela.basic_access, escuela.basic_seats_used, escuela.basic_seats_max, escuela.basic_key,
            ),
            "professional": tier(
                escuela.professional_access, escuela.professional_seats_used, escuela.professional_seats_max, escuela.professional_key,
            ),
        })


class SubscriptionSeatsView(APIView):
    """GET  /api/v1/schools/<school_id>/subscription-seats/
    DELETE /api/v1/schools/<school_id>/subscription-seats/<estudiante_curso_id>/

    GET: lista los EstudianteCurso que ocupan un seat (AccessKey.origen='seat').
    DELETE: libera el seat revocando el acceso (marca la key como revoked,
    borra el EstudianteCurso, decrementa `*_seats_used`).

    Permisos: admin (cualquier escuela) o director (solo la suya).
    """
    permission_classes = [drf_permissions.IsAuthenticated]

    def _authz(self, request, school_id):
        try:
            school_id = int(school_id)
        except (TypeError, ValueError):
            return None, Response({"detail": "school_id inválido."}, status=status.HTTP_400_BAD_REQUEST)
        if not is_admin(request.user):
            if not is_director(request.user) or request.user.escuela_id != school_id:
                return None, Response({"detail": "No autorizado."}, status=status.HTTP_403_FORBIDDEN)
        return school_id, None

    def get(self, request, school_id, estudiante_curso_id=None):
        school_id, err = self._authz(request, school_id)
        if err is not None:
            return err

        qs = (
            EstudianteCurso.objects
            .filter(
                estudiante_id__escuela_id=school_id,
                access_key_id__origen="seat",
            )
            .select_related("estudiante_id", "curso_id", "access_key_id")
            .order_by("id")
        )

        results = [
            {
                "id": ec.id,
                "estudiante": {
                    "id": ec.estudiante_id_id,
                    "nombre": f"{ec.estudiante_id.nombre} {ec.estudiante_id.apellido}".strip(),
                    "email": ec.estudiante_id.email,
                },
                "curso": {
                    "id": ec.curso_id_id,
                    "nombre": ec.curso_id.nombre,
                    "is_profesional": ec.curso_id.is_profesional,
                },
                "access_key": {
                    "id": str(ec.access_key_id.id),
                    "status": ec.access_key_id.status,
                    "valid_from": ec.access_key_id.valid_from,
                },
            }
            for ec in qs
        ]
        return Response({"count": len(results), "results": results})

    def delete(self, request, school_id, estudiante_curso_id):
        school_id, err = self._authz(request, school_id)
        if err is not None:
            return err

        try:
            estudiante_curso_id = int(estudiante_curso_id)
        except (TypeError, ValueError):
            return Response({"detail": "estudiante_curso_id inválido."}, status=status.HTTP_400_BAD_REQUEST)

        with db_transaction.atomic():
            ec = (
                EstudianteCurso.objects
                .select_related("access_key_id", "curso_id", "estudiante_id")
                .filter(pk=estudiante_curso_id)
                .first()
            )
            if ec is None:
                return Response({"detail": "Inscripción no encontrada."}, status=status.HTTP_404_NOT_FOUND)
            if ec.estudiante_id.escuela_id != school_id:
                return Response({"detail": "La inscripción no pertenece a esta escuela."}, status=status.HTTP_404_NOT_FOUND)

            access_key = ec.access_key_id
            if access_key.origen != "seat":
                return Response(
                    {"detail": "Esta inscripción no ocupa un cupo de suscripción; usa el flujo de llaves."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            escuela = Escuela.objects.select_for_update().get(pk=school_id)
            is_pro = bool(ec.curso_id.is_profesional)
            if is_pro:
                if escuela.professional_seats_used > 0:
                    escuela.professional_seats_used -= 1
            else:
                if escuela.basic_seats_used > 0:
                    escuela.basic_seats_used -= 1
            escuela.save()

            access_key.status = "revoked"
            access_key.save(update_fields=["status"])
            ec.delete()

        return Response({"status": "released"}, status=status.HTTP_200_OK)