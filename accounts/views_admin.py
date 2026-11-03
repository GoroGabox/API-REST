"""Vistas de solo lectura para el panel de admin (Resumen, Estado del sistema, Calidad).

Todas exigen admin (``IsAdmin``). Ninguna expone valores de credenciales: el
estado de cada servicio se informa como booleano ("configurado sí/no").
"""
import os
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Certificado, Prueba, Usuario
from accounts.permissions import IsAdmin
from sales.models import EstudianteCurso, SolicitudAcceso, Venta
from schools.models import (
    PREGUNTAS_SESION_TEMATICA, Categoria, Curso, Ejercicio, Escuela, Leccion, LeccionRecurso,
)

# Transbank deja 'AUTHORIZED'; flujos internos/tests usan 'approved'.
ESTADOS_PAGADOS = ("AUTHORIZED", "authorized", "approved", "APPROVED", "paid")
PLACEHOLDER_URL = "http://placeholder.url"
SALDO_BAJO = 5  # llaves


def _dias(request, default=30):
    try:
        d = int(request.query_params.get("dias", default))
    except (TypeError, ValueError):
        d = default
    return max(1, min(d, 365))


class AdminResumenView(APIView):
    """GET accounts/admin/resumen/?dias=30 — KPIs del negocio para el inicio del admin."""

    permission_classes = [IsAdmin]

    def get(self, request):
        dias = _dias(request)
        now = timezone.now()
        desde = now - timedelta(days=dias)

        pagadas = Venta.objects.filter(payment_status__in=ESTADOS_PAGADOS)
        periodo = pagadas.filter(fecha_venta__gte=desde)
        agg = periodo.aggregate(total=Sum("monto_pagado"), n=Count("id"))
        ingresos = int(agg["total"] or 0)
        ventas = agg["n"] or 0

        por_dia = {
            r["d"]: r
            for r in periodo.annotate(d=TruncDate("fecha_venta"))
            .values("d")
            .annotate(monto=Sum("monto_pagado"), ventas=Count("id"))
        }
        serie = []
        for i in range(dias - 1, -1, -1):
            dia = (now - timedelta(days=i)).date()
            r = por_dia.get(dia)
            serie.append({
                "fecha": dia.isoformat(),
                "monto": int(r["monto"] or 0) if r else 0,
                "ventas": r["ventas"] if r else 0,
            })

        vigentes = (
            EstudianteCurso.objects.filter(access_key_id__status="active")
            .filter(Q(access_key_id__valid_until__isnull=True) | Q(access_key_id__valid_until__gte=now))
        )
        finales = Prueba.objects.filter(modalidad="evaluacion", fecha__gte=desde.date())

        ultimas = []
        for v in pagadas.select_related("producto", "curso", "usuario", "escuela").order_by("-fecha_venta")[:10]:
            ultimas.append({
                "id": v.id,
                "fecha": v.fecha_venta.isoformat() if v.fecha_venta else None,
                "monto": int(v.monto_pagado or 0),
                "item": (v.producto.nombre if v.producto else (v.curso.nombre if v.curso else "—")),
                "comprador": (f"{v.usuario.nombre} {v.usuario.apellido}".strip() if v.usuario else ""),
                "escuela": v.escuela.nombre if v.escuela else None,
            })

        return Response({
            "dias": dias,
            "ingresos": ingresos,
            "ventas": ventas,
            "ticket_promedio": round(ingresos / ventas) if ventas else 0,
            "serie": serie,
            "usuarios_nuevos": {
                "d7": Usuario.objects.filter(date_joined__gte=now - timedelta(days=7)).count(),
                "d30": Usuario.objects.filter(date_joined__gte=now - timedelta(days=30)).count(),
            },
            "estudiantes_con_acceso": vigentes.values("estudiante_id").distinct().count(),
            # Activa = tiene llaves o una suscripción vigente.
            "escuelas_activas": Escuela.objects.filter(Q(basic_key__gt=0) | Q(basic_access=True)).count(),
            "llaves_en_circulacion": int(Escuela.objects.aggregate(s=Sum("basic_key"))["s"] or 0),
            "solicitudes_pendientes": SolicitudAcceso.objects.filter(estado="pendiente").count(),
            "examenes_finales": {
                "rendidos": finales.count(),
                "aprobados": finales.filter(aprobado=True).count(),
            },
            "certificados": Certificado.objects.filter(fecha_emision__gte=desde.date()).count(),
            "ultimas_ventas": ultimas,
            "escuelas_saldo_bajo": list(
                Escuela.objects.filter(basic_key__lte=SALDO_BAJO)
                .order_by("basic_key", "nombre")
                .values("id", "nombre", "basic_key")[:10]
            ),
        })


class AdminEstadoView(APIView):
    """GET accounts/admin/estado/ — configuración de servicios, solo booleanos y nombres."""

    permission_classes = [IsAdmin]

    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            db_ok = True
        except Exception:
            db_ok = False

        commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("GIT_COMMIT") or ""
        storage = str(getattr(settings, "MEDIA_STORAGE", "local") or "local").lower()
        publico = os.environ.get("MEDIA_S3_PUBLIC_BASE_URL") or os.environ.get("AUDIO_S3_PUBLIC_BASE_URL") or None
        backend = str(getattr(settings, "EMAIL_BACKEND", "") or "")

        return Response({
            "api": {
                "db_ok": db_ok,
                "version": commit[:7] if commit else "local",
                "entorno": os.environ.get("DJANGO_ENV") or ("development" if settings.DEBUG else "production"),
            },
            "pagos": {"transbank": (os.environ.get("TBK_ENVIRONMENT") or "TEST").upper()},
            "medios": {"storage": storage, "bucket_publico": publico if storage == "s3" else None},
            "email": {
                "backend": backend.rsplit(".", 1)[-1] if backend else "",
                "resend_configurado": bool(os.environ.get("RESEND_API_KEY") or getattr(settings, "RESEND_API_KEY", "")),
            },
            "ia": {
                "anthropic_configurado": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "modelo": os.environ.get("COURSE_LLM_MODEL") or getattr(settings, "COURSE_LLM_MODEL", "") or "",
            },
        })


class AdminCalidadView(APIView):
    """GET accounts/admin/calidad/ — pendientes de contenido para corregir."""

    permission_classes = [IsAdmin]
    LIMITE = 100

    def get(self, request):
        sin_precio = (
            Curso.objects.annotate(n=Count("planes", filter=Q(planes__activo=True, planes__precio__gt=0)))
            .filter(n=0).order_by("nombre")
        )
        sin_lecciones = Curso.objects.annotate(n=Count("leccion")).filter(n=0).order_by("nombre")

        con_medios = set(
            LeccionRecurso.objects.filter(Q(tipo="imagen") | Q(rol="narracion")).values_list("leccion_id", flat=True)
        )
        lecciones_texto = (
            Leccion.objects.filter(tipo="texto").select_related("curso").order_by("curso_id", "posicion", "id")
        )
        sin_medios = [
            l for l in lecciones_texto
            if l.id not in con_medios and (not l.url_audio or l.url_audio == PLACEHOLDER_URL)
        ]

        categorias = (
            Categoria.objects.annotate(n=Count("ejercicio")).filter(n__lt=PREGUNTAS_SESION_TEMATICA).order_by("n", "nombre")
        )
        ejercicios = (
            Ejercicio.objects.filter(categoria__nombre__icontains="señal")
            .filter(Q(imagen__isnull=True) | Q(imagen="") | Q(imagen=PLACEHOLDER_URL))
            .select_related("categoria").order_by("id")
        )

        L = self.LIMITE
        return Response({
            "conteos": {
                "cursos_sin_precio": sin_precio.count(),
                "cursos_sin_lecciones": sin_lecciones.count(),
                "lecciones_sin_medios": len(sin_medios),
                "categorias_sin_tematica": categorias.count(),
                "ejercicios_sin_imagen": ejercicios.count(),
            },
            "cursos_sin_precio": list(sin_precio.values("id", "nombre", "slug")[:L]),
            "cursos_sin_lecciones": list(sin_lecciones.values("id", "nombre", "slug")[:L]),
            "lecciones_sin_medios": [
                {"id": l.id, "nombre": l.nombre, "curso": l.curso.nombre if l.curso else None} for l in sin_medios[:L]
            ],
            "categorias_sin_tematica": [
                {"id": c.id, "nombre": c.nombre, "preguntas": c.n} for c in categorias[:L]
            ],
            "ejercicios_sin_imagen": [
                {"id": e.id, "pregunta": (e.pregunta or "")[:140], "categoria": e.categoria.nombre if e.categoria else None}
                for e in ejercicios[:L]
            ],
        })
