"""Endpoints de suscripciones de escuelas (director, catálogo público y admin).

Director (`sales/suscripcion/...`): estado, historial, cotizar, programar o
deshacer una bajada, "no renovaré", datos de facturación y pedir pago por
transferencia. El pago con Webpay usa el flujo unificado (`pay_init` /
`pay_confirm` con `item_type='suscripcion'` y buy_order `sus_<plan>_<meses>_<usuario>`).

Público: `sales/suscripcion/planes/` (planes activos, para /pricing y el panel).

Admin (`sales/admin/...`): CRUD de planes, suscripción de una escuela con
cortesías, y confirmar/anular transferencias.
"""
from django.db import transaction
from rest_framework import permissions as drf_permissions
from rest_framework import serializers, status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin, is_admin, is_director
from autotestAPI.auditoria import como_usuario
from schools.models import Escuela

from . import suscripciones as S
from .models import DatosFacturacion, PlanSuscripcion, Venta


def _error(e):
    return Response({"error": str(e), "code": e.code}, status=status.HTTP_400_BAD_REQUEST)


def _escuela_director(request):
    user = request.user
    if not (is_director(user) and user.escuela_id):
        return None, Response({"error": "Solo un director con escuela."}, status=status.HTTP_403_FORBIDDEN)
    return user.escuela, None


def _plan_y_meses(data):
    try:
        plan = S.plan_activo(int(data.get("plan_id")))
    except (TypeError, ValueError):
        raise S.SuscripcionError("Indica el plan.", 'plan_invalido')
    return plan, data.get("meses")


def _periodos(sus):
    if sus is None:
        return []
    return [
        {
            "id": p.id,
            "motivo": p.motivo,
            "plan": p.plan.nombre,
            "periodo_meses": p.periodo_meses,
            "inicio": p.inicio,
            "fin": p.fin,
            "monto": p.monto,
            "venta_id": p.venta_id,
        }
        for p in sus.periodos.select_related('plan')[:50]
    ]


def _transferencias(escuela):
    return [
        {
            "id": v.id,
            "referencia": v.buy_order,
            "plan": v.plan_suscripcion.nombre if v.plan_suscripcion else None,
            "periodo_meses": v.periodo_meses,
            "monto": int(v.monto_pagado),
            "fecha": v.fecha_venta,
        }
        for v in Venta.objects.select_related('plan_suscripcion').filter(
            escuela=escuela, pay_system='TRANSFERENCIA', payment_status=S.ESTADO_TRANSFERENCIA_PENDIENTE,
        )
    ]


class FacturacionSerializer(serializers.ModelSerializer):
    class Meta:
        model = DatosFacturacion
        fields = ['rut', 'razon_social', 'giro', 'direccion', 'email']


def detalle_escuela(escuela):
    sus = S.suscripcion_de(escuela)
    fact = DatosFacturacion.objects.filter(escuela=escuela).first()
    return {
        "suscripcion": S.resumen(escuela),
        "periodos": _periodos(sus),
        "facturacion": FacturacionSerializer(fact).data if fact else None,
        "transferencias_pendientes": _transferencias(escuela),
        "datos_bancarios": S.datos_bancarios(),
    }


# ---------------------------------------------------------------- público

class PlanesPublicosView(APIView):
    """GET planes activos (sin auth): para /pricing, /enterprices y el panel."""
    permission_classes = [drf_permissions.AllowAny]
    authentication_classes = []

    def get(self, request):
        planes = PlanSuscripcion.objects.filter(activo=True).order_by('tier', 'cupos')
        return Response([S.plan_dict(p) for p in planes])


# ---------------------------------------------------------------- director

class MiSuscripcionView(APIView):
    """GET estado completo de la suscripción de la escuela del director."""
    permission_classes = [drf_permissions.IsAuthenticated]

    def get(self, request):
        escuela, err = _escuela_director(request)
        if err:
            return err
        return Response(detalle_escuela(escuela))


class CotizarView(APIView):
    """POST {plan_id, meses} → {tipo, monto, dias_restantes?}."""
    permission_classes = [drf_permissions.IsAuthenticated]

    def post(self, request):
        escuela, err = _escuela_director(request)
        if err:
            return err
        try:
            plan, meses = _plan_y_meses(request.data)
            cot = S.cotizar(escuela, plan, meses)
        except S.SuscripcionError as e:
            return _error(e)
        return Response({
            "tipo": cot["tipo"],
            "monto": cot["monto"],
            "dias_restantes": cot.get("dias_restantes"),
            "plan": S.plan_dict(plan),
            "meses": cot["meses"],
            "buy_order": S.buy_order_suscripcion(plan.id, cot["meses"], request.user.id),
            "item_type": S.item_type_de(cot),
        })


class BajadaView(APIView):
    """POST {plan_id, meses}: programa una bajada para la renovación. DELETE: la deshace."""
    permission_classes = [drf_permissions.IsAuthenticated]

    def post(self, request):
        escuela, err = _escuela_director(request)
        if err:
            return err
        try:
            plan, meses = _plan_y_meses(request.data)
            S.programar_bajada(escuela, plan, meses)
        except S.SuscripcionError as e:
            return _error(e)
        return Response(detalle_escuela(escuela))

    def delete(self, request):
        escuela, err = _escuela_director(request)
        if err:
            return err
        try:
            S.cancelar_cambio(escuela)
        except S.SuscripcionError as e:
            return _error(e)
        return Response(detalle_escuela(escuela))


class NoRenovarView(APIView):
    """POST {no_renovara: bool, motivo}."""
    permission_classes = [drf_permissions.IsAuthenticated]

    def post(self, request):
        escuela, err = _escuela_director(request)
        if err:
            return err
        try:
            S.marcar_no_renovar(escuela, request.data.get("no_renovara", True), request.data.get("motivo", ""))
        except S.SuscripcionError as e:
            return _error(e)
        return Response(detalle_escuela(escuela))


class FacturacionView(APIView):
    """GET / PUT datos de facturación de la escuela."""
    permission_classes = [drf_permissions.IsAuthenticated]

    def get(self, request):
        escuela, err = _escuela_director(request)
        if err:
            return err
        fact = DatosFacturacion.objects.filter(escuela=escuela).first()
        return Response(FacturacionSerializer(fact).data if fact else None)

    def put(self, request):
        escuela, err = _escuela_director(request)
        if err:
            return err
        fact = DatosFacturacion.objects.filter(escuela=escuela).first()
        ser = FacturacionSerializer(fact, data=request.data)
        ser.is_valid(raise_exception=True)
        ser.save(escuela=escuela)
        return Response(ser.data)


class TransferenciaView(APIView):
    """POST {plan_id, meses}: pide pagar por transferencia. DELETE /<id>/: la anula."""
    permission_classes = [drf_permissions.IsAuthenticated]

    def post(self, request):
        escuela, err = _escuela_director(request)
        if err:
            return err
        try:
            plan, meses = _plan_y_meses(request.data)
            S.solicitar_transferencia(escuela, plan, meses, request.user)
        except S.SuscripcionError as e:
            return _error(e)
        return Response(detalle_escuela(escuela), status=status.HTTP_201_CREATED)

    def delete(self, request, venta_id):
        escuela, err = _escuela_director(request)
        if err:
            return err
        venta = Venta.objects.filter(pk=venta_id, escuela=escuela).first()
        if venta is None:
            return Response({"error": "Orden no encontrada."}, status=status.HTTP_404_NOT_FOUND)
        try:
            S.anular_transferencia(venta)
        except S.SuscripcionError as e:
            return _error(e)
        return Response(detalle_escuela(escuela))


# ---------------------------------------------------------------- admin

class PlanSuscripcionSerializer(serializers.ModelSerializer):
    nombre = serializers.CharField(read_only=True)

    class Meta:
        model = PlanSuscripcion
        fields = ['id', 'tier', 'cupos', 'nombre', 'precio_mensual', 'precio_semestral', 'precio_anual', 'activo']

    def validate(self, attrs):
        for campo in ('precio_mensual', 'precio_semestral', 'precio_anual'):
            if campo in attrs and attrs[campo] <= 0:
                raise serializers.ValidationError({campo: "El precio debe ser mayor que 0."})
        if 'cupos' in attrs and attrs['cupos'] <= 0:
            raise serializers.ValidationError({'cupos': "Debe tener al menos 1 cupo."})
        return attrs


class PlanSuscripcionAdminViewSet(viewsets.ModelViewSet):
    """CRUD de planes. Borrar un plan en uso no se permite: se desactiva."""
    permission_classes = [IsAdmin]
    serializer_class = PlanSuscripcionSerializer
    queryset = PlanSuscripcion.objects.all().order_by('tier', 'cupos')
    pagination_class = None

    def perform_create(self, serializer):
        with como_usuario(self.request.user):
            serializer.save()

    def perform_update(self, serializer):
        with como_usuario(self.request.user):
            serializer.save()

    def destroy(self, request, *args, **kwargs):
        plan = self.get_object()
        if plan.suscripciones.exists() or Venta.objects.filter(plan_suscripcion=plan).exists():
            return Response({"error": "El plan tiene ventas o suscripciones: desactívalo en vez de borrarlo."},
                            status=status.HTTP_409_CONFLICT)
        return super().destroy(request, *args, **kwargs)


class SuscripcionEscuelaAdminView(APIView):
    """GET detalle de la suscripción de una escuela. POST cortesía:
    {dias} extiende, o {plan_id, meses} regala un período de ese plan."""
    permission_classes = [IsAdmin]

    def get(self, request, escuela_id):
        escuela = Escuela.objects.filter(pk=escuela_id).first()
        if escuela is None:
            return Response({"error": "Escuela no encontrada."}, status=status.HTTP_404_NOT_FOUND)
        return Response(detalle_escuela(escuela))

    def post(self, request, escuela_id):
        escuela = Escuela.objects.filter(pk=escuela_id).first()
        if escuela is None:
            return Response({"error": "Escuela no encontrada."}, status=status.HTTP_404_NOT_FOUND)
        try:
            if request.data.get("plan_id"):
                plan = PlanSuscripcion.objects.filter(pk=request.data.get("plan_id")).first()
                if plan is None:
                    raise S.SuscripcionError("Plan no encontrado.", 'plan_invalido')
                S.cortesia(escuela, plan=plan, meses=request.data.get("meses") or 1, usuario=request.user)
            else:
                S.cortesia(escuela, dias=request.data.get("dias"), usuario=request.user)
        except S.SuscripcionError as e:
            return _error(e)
        return Response(detalle_escuela(escuela))


class TransferenciaAdminView(APIView):
    """POST {accion: 'confirmar'|'anular', folio?} sobre una venta pendiente.
    PATCH {folio}: anota el folio de la factura de cualquier venta."""
    permission_classes = [IsAdmin]

    def post(self, request, venta_id):
        venta = Venta.objects.select_related('escuela', 'plan_suscripcion').filter(pk=venta_id).first()
        if venta is None:
            return Response({"error": "Venta no encontrada."}, status=status.HTTP_404_NOT_FOUND)
        try:
            if request.data.get("accion") == "anular":
                S.anular_transferencia(venta)
            else:
                S.confirmar_transferencia(venta, request.user, folio=request.data.get("folio", ""))
        except S.SuscripcionError as e:
            return _error(e)
        return Response({"status": "ok"})

    def patch(self, request, venta_id):
        with transaction.atomic():
            n = Venta.objects.filter(pk=venta_id).update(folio_factura=(request.data.get("folio") or "").strip()[:40])
        if not n:
            return Response({"error": "Venta no encontrada."}, status=status.HTTP_404_NOT_FOUND)
        return Response({"status": "ok"})
