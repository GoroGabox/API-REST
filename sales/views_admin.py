"""Sección Comercial del panel de admin (solo lectura, `IsAdmin`).

La lógica vive en `sales/comercial.py`; aquí solo se leen parámetros, se pagina
y se exporta. Período: `?desde=YYYY-MM-DD&hasta=YYYY-MM-DD` (ambos inclusive) o
`?dias=N` (por defecto 30, hasta hoy). `formato=csv` descarga sin paginar.
"""
import csv
from datetime import datetime, time, timedelta

from django.db.models import Sum
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin

from . import comercial
from .models import CambioPrecio, IntentoPago, Venta


class _Paginacion(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 500


class _Eco:
    def write(self, value):
        return value


def _csv(nombre, columnas, filas):
    escritor = csv.writer(_Eco())

    def generar():
        yield "﻿"  # BOM: Excel abre bien los acentos.
        yield escritor.writerow(columnas)
        for fila in filas:
            yield escritor.writerow(fila)

    resp = StreamingHttpResponse(generar(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{nombre}_{timezone.localdate().isoformat()}.csv"'
    return resp


def _es_csv(request):
    return (request.query_params.get("formato") or "").lower() == "csv"


def _fecha(valor):
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError(f"Fecha inválida: {valor!r} (formato YYYY-MM-DD).")


def rango(request, dias_default=30):
    """(desde, hasta) aware; `hasta` exclusivo. Lanza ValueError si es inválido."""
    p = request.query_params
    tz = timezone.get_current_timezone()
    if p.get("desde") or p.get("hasta"):
        hasta_d = _fecha(p["hasta"]) if p.get("hasta") else timezone.localdate()
        desde_d = _fecha(p["desde"]) if p.get("desde") else hasta_d - timedelta(days=dias_default - 1)
        if desde_d > hasta_d:
            raise ValueError("`desde` es posterior a `hasta`.")
        desde = timezone.make_aware(datetime.combine(desde_d, time.min), tz)
        hasta = timezone.make_aware(datetime.combine(hasta_d + timedelta(days=1), time.min), tz)
        return desde, hasta
    try:
        dias = max(1, min(int(p.get("dias", dias_default)), 730))
    except (TypeError, ValueError):
        dias = dias_default
    hasta = timezone.now()
    inicio = timezone.localdate() - timedelta(days=dias - 1)
    return timezone.make_aware(datetime.combine(inicio, time.min), tz), hasta


def _params_con_rango(request, desde, hasta):
    params = request.query_params.copy()
    params["desde"], params["hasta"] = desde, hasta
    return params


def _malo(e):
    return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class PanoramaView(APIView):
    """GET sales/admin/panorama/ — KPIs vs. período anterior, canales, serie, tops."""
    permission_classes = [IsAdmin]

    def get(self, request):
        try:
            desde, hasta = rango(request)
        except ValueError as e:
            return _malo(e)
        return Response(comercial.panorama(desde, hasta))


COLUMNAS_VENTAS = [
    "id", "fecha", "comprador", "email", "telefono", "escuela", "canal", "item", "dias",
    "monto", "medio", "estado", "buy_order",
]


class VentasAdminView(APIView):
    """GET sales/admin/ventas/ — ventas pagadas con filtros (canal, producto,
    curso, escuela, dias, monto_min/max, q, orden), paginadas o en CSV."""
    permission_classes = [IsAdmin]

    def get(self, request):
        params = request.query_params.copy()
        if params.get("desde") or params.get("hasta") or params.get("dias"):
            try:
                desde, hasta = rango(request)
            except ValueError as e:
                return _malo(e)
            params = _params_con_rango(request, desde, hasta)
        qs = comercial.filtrar_ventas(params)
        if _es_csv(request):
            def filas():
                for v in qs.iterator():
                    f = comercial.fila_venta(v)
                    c = f["comprador"] or {}
                    yield [
                        f["id"], f["fecha"], c.get("nombre", ""), c.get("email", ""), c.get("telefono", ""),
                        (f["escuela"] or {}).get("nombre", ""), f["canal"], f["item"], f["dias"] or "",
                        f["monto"], f["pay_system"], f["payment_status"], f["buy_order"] or "",
                    ]
            return _csv("ventas", COLUMNAS_VENTAS, filas())
        paginador = _Paginacion()
        pagina = paginador.paginate_queryset(qs, request, view=self)
        resp = paginador.get_paginated_response([comercial.fila_venta(v) for v in pagina])
        resp.data["total_monto"] = comercial._int(qs.order_by().aggregate(s=Sum("monto_pagado"))["s"])
        return resp


class VentaAdminDetalleView(APIView):
    """GET sales/admin/ventas/<id>/ — venta + transacción + intentos del mismo buy_order."""
    permission_classes = [IsAdmin]

    def get(self, request, venta_id):
        v = get_object_or_404(Venta.objects.select_related("producto", "curso", "escuela", "usuario"), pk=venta_id)
        data = comercial.fila_venta(v)
        tx = getattr(v, "transbank_transaction", None)
        data["transaccion"] = {
            "fecha": tx.transaction_date.isoformat() if tx.transaction_date else None,
            "tipo_pago": tx.payment_type_code,
            "estado": tx.status,
            "monto": tx.amount,
            "buy_order": tx.buy_order,
        } if tx else None
        intentos = IntentoPago.objects.select_related("usuario", "escuela", "producto", "curso").filter(
            buy_order=v.buy_order
        ) if v.buy_order else IntentoPago.objects.none()
        data["intentos"] = [comercial.fila_intento(i) for i in intentos[:20]]
        return Response(data)


COLUMNAS_INTENTOS = ["id", "fecha", "estado", "codigo", "comprador", "email", "escuela", "canal", "item", "dias", "monto", "buy_order"]


class IntentosAdminView(APIView):
    """GET sales/admin/intentos/ — intentos de pago (estado, canal, q), con resumen."""
    permission_classes = [IsAdmin]

    def get(self, request):
        try:
            desde, hasta = rango(request)
        except ValueError as e:
            return _malo(e)
        now = timezone.now()
        qs = comercial.filtrar_intentos(_params_con_rango(request, desde, hasta), now=now)
        if _es_csv(request):
            def filas():
                for i in qs.iterator():
                    f = comercial.fila_intento(i, now)
                    c = f["comprador"] or {}
                    yield [
                        f["id"], f["fecha"], f["estado"], f["codigo_respuesta"] or "", c.get("nombre", ""),
                        c.get("email", ""), (f["escuela"] or {}).get("nombre", ""), f["canal"], f["item"],
                        f["dias"] or "", f["monto"], f["buy_order"],
                    ]
            return _csv("intentos_pago", COLUMNAS_INTENTOS, filas())
        paginador = _Paginacion()
        pagina = paginador.paginate_queryset(qs, request, view=self)
        resp = paginador.get_paginated_response([comercial.fila_intento(i, now) for i in pagina])
        resp.data["resumen"] = comercial.resumen_intentos(desde, hasta, now)
        return resp


class PrecioEvolucionView(APIView):
    """GET sales/admin/precios/evolucion/?tipo=plan_curso|producto&id=N[&dias|desde/hasta]."""
    permission_classes = [IsAdmin]

    def get(self, request):
        tipo = request.query_params.get("tipo")
        if tipo not in ("plan_curso", "producto"):
            return _malo("`tipo` debe ser plan_curso o producto.")
        try:
            objeto_id = int(request.query_params.get("id"))
        except (TypeError, ValueError):
            return _malo("`id` inválido.")
        try:
            desde, hasta = rango(request, dias_default=180)
        except ValueError as e:
            return _malo(e)
        data = comercial.evolucion_precio(tipo, objeto_id, desde, hasta)
        if data is None:
            return Response({"detail": "No encontrado."}, status=status.HTTP_404_NOT_FOUND)
        return Response(data)


class PrecioHistorialView(APIView):
    """GET sales/admin/precios/historial/?tipo=&q= — cambios de precio, paginados."""
    permission_classes = [IsAdmin]

    def get(self, request):
        qs = CambioPrecio.objects.select_related("usuario").order_by("-fecha", "-id")
        tipo = request.query_params.get("tipo")
        if tipo in ("plan_curso", "producto"):
            qs = qs.filter(tipo=tipo)
        q = (request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(nombre__icontains=q)
        if (request.query_params.get("altas") or "").lower() not in ("1", "true", "si"):
            # Por defecto se ocultan las "fotos" iniciales (sin valor anterior).
            qs = qs.exclude(precio_anterior__isnull=True, descuento_anterior__isnull=True)
        paginador = _Paginacion()
        pagina = paginador.paginate_queryset(qs, request, view=self)
        return paginador.get_paginated_response([comercial.fila_cambio(c) for c in pagina])


COLUMNAS_ESCUELAS = [
    "id", "nombre", "email", "telefono", "director", "compras", "total", "ultima_compra",
    "dias_entre_compras", "pack_habitual", "saldo", "consumo_semanal", "semanas_saldo",
    "alumnos_activos", "suscripcion_activa",
]
COLUMNAS_ESTUDIANTES = [
    "id", "nombre", "email", "telefono", "escuela", "compras", "renovaciones", "total",
    "plan_preferido", "ultima_compra", "acceso_vigente", "vence", "lecciones_completadas",
    "ultimo_dia_activo", "certificado",
]


class CarteraView(APIView):
    """GET sales/admin/cartera/<escuelas|estudiantes>/ — comportamiento de clientes."""
    permission_classes = [IsAdmin]

    def get(self, request, segmento):
        if segmento == "escuelas":
            filas = comercial.cartera_escuelas()
            q = (request.query_params.get("q") or "").strip().lower()
            if q:
                filas = [f for f in filas if q in f["nombre"].lower() or q in (f["email"] or "").lower()]
            columnas = COLUMNAS_ESCUELAS
            plano = lambda f: [*(f[c] for c in columnas[:-1]), f["suscripcion"]["activa"]]  # noqa: E731
        elif segmento == "estudiantes":
            filas = comercial.cartera_estudiantes(request.query_params)
            columnas = COLUMNAS_ESTUDIANTES
            plano = lambda f: [f[c] for c in columnas]  # noqa: E731
        else:
            return Response({"detail": "Segmento inválido."}, status=status.HTTP_404_NOT_FOUND)
        if _es_csv(request):
            return _csv(f"cartera_{segmento}", columnas, (plano(f) for f in filas))
        return Response({"results": filas, "count": len(filas)})


class OportunidadesView(APIView):
    """GET sales/admin/oportunidades/ — a quién ofrecer qué (motivo + sugerencia).

    `?regla=<clave>&formato=csv` exporta los casos de una regla.
    """
    permission_classes = [IsAdmin]

    def get(self, request):
        reglas = comercial.oportunidades()
        if _es_csv(request):
            clave = request.query_params.get("regla")
            casos = next((r["casos"] for r in reglas if r["clave"] == clave), None)
            if casos is None:
                return _malo("`regla` inválida.")
            columnas = ["nombre", "email", "telefono", "motivo", "sugerencia", "valor"]
            return _csv(f"oportunidades_{clave}", columnas, ([c[k] for k in columnas] for c in casos))
        return Response({"reglas": reglas})
