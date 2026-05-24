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
    registrar_venta_transbank,
    registrar_venta_unificada,
)


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


class AccessKeyViewSet(viewsets.ModelViewSet):
    """admin: full; director: solo llaves derivadas de sus ventas; estudiante: 403.

    Una llave es 'del director' si existe una Venta del director cuyo flujo
    haya generado un EstudianteCurso con esta llave.
    """
    queryset = AccessKey.objects.all()
    serializer_class = AccessKeySerializer
    permission_classes = [drf_permissions.IsAuthenticated]

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
    permission_classes = [drf_permissions.IsAuthenticated]

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

        # Todos los cursos del sistema (si luego los quieres por escuela, aquí se filtra)
        cursos = Curso.objects.all()

        serializer = CursoDisponibleSerializer(
            cursos,
            many=True,
            context={
                "owned_ids": owned_ids,
                "escuela": escuela,
            },
        )

        return Response(serializer.data, status=status.HTTP_200_OK)

class ActivarCursoView(APIView):
    """Activación manual de curso para un estudiante.

    Reglas:
    - admin: puede activar cualquier curso para cualquier estudiante.
    - director: solo puede activar para estudiantes de SU escuela, y solo
      si su escuela tiene saldo de llaves comprado (basic_key o
      professional_key > 0 según el curso). El contador se decrementa.
    - estudiante: 403.
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

        try:
            user = Usuario.objects.get(id=user_id)
            curso = Curso.objects.get(id=curso_id)
            days = int(days) if days else 30
        except Curso.DoesNotExist:
            return Response({"error": "Curso no encontrado."}, status=status.HTTP_404_NOT_FOUND)
        except Usuario.DoesNotExist:
            return Response({"error": "Usuario no encontrado."}, status=status.HTTP_404_NOT_FOUND)

        # Restricciones de director: estudiante debe ser de su escuela y debe
        # tener saldo de llaves del tipo correspondiente al curso.
        if is_director(request.user):
            if user.escuela_id != request.user.escuela_id:
                return Response(
                    {"error": "Solo puedes activar cursos para estudiantes de tu escuela."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            with db_transaction.atomic():
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
                access_key = asignar_llave_y_curso(user, curso, days)
        else:
            access_key = asignar_llave_y_curso(user, curso, days)

        return Response({
            "message": "Clave activada con éxito.",
            "access_key": access_key.key,
            "valid_until": access_key.valid_until,
        }, status=status.HTTP_201_CREATED)

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

        return_url = 'http://localhost:3000/pay_confirmation'

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
        return_url = "http://localhost:3000/pay_confirmation"
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
                    {"error": "buy_order tiene formato inválido. Esperado: order_<course_id>_<student_id>."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            owner_check = _enforce_payment_ownership(request, student_id)
            if owner_check is not None:
                return owner_check

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