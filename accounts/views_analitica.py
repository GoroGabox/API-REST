"""Analítica de pruebas para director y admin (JSON paginado o CSV).

- ``GET accounts/analitica/pruebas/``    una fila por prueba.
- ``GET accounts/analitica/respuestas/`` una fila por respuesta (PruebaEjercicio).

Se registran TODAS las pruebas (práctica y examen final); cada fila trae
``es_examen_final`` / ``cuenta_progreso`` para separarlas. Solo el examen final
del curso mide el progreso del estudiante.

Filtros (query): ``desde``, ``hasta`` (YYYY-MM-DD, sobre la fecha de la
prueba), ``escuela_id`` (solo admin), ``curso_id``, ``estudiante_id``,
``ambito`` = todas|final|practica (def. todas), ``entregadas`` = 1|0 (def. 1).
``formato=csv`` descarga todas las filas filtradas (sin paginar).

Alcance: admin ve todo; director solo estudiantes de su escuela; resto 403.
"""
import csv
from datetime import date

from django.http import StreamingHttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import Prueba, PruebaEjercicio
from .permissions import IsAdmin, IsDirector, is_admin

AMBITOS = ("todas", "final", "practica")


class _Paginacion(PageNumberPagination):
    page_size = 100
    page_size_query_param = "page_size"
    max_page_size = 1000


class _Eco:
    """Pseudo-buffer para csv.writer en una respuesta en streaming."""

    def write(self, value):
        return value


def _fecha(valor, nombre):
    if not valor:
        return None
    try:
        return date.fromisoformat(valor)
    except ValueError:
        raise ValueError(f"{nombre} debe tener formato YYYY-MM-DD.")


def _entero(valor, nombre):
    if valor in (None, ""):
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        raise ValueError(f"{nombre} debe ser un entero.")


class _AnaliticaBase(APIView):
    permission_classes = [IsAuthenticated, IsAdmin | IsDirector]
    modelo = None
    prefijo = ""
    columnas = []
    nombre_archivo = ""

    def filas(self, qs):  # pragma: no cover - lo implementa cada subclase
        raise NotImplementedError

    def _filtros(self, request):
        qp = request.query_params
        ambito = (qp.get("ambito") or "todas").lower()
        if ambito not in AMBITOS:
            raise ValueError("ambito debe ser todas, final o practica.")
        escuela_id = _entero(qp.get("escuela_id"), "escuela_id")
        if not is_admin(request.user):
            # Director: siempre su escuela, ignore lo que pida.
            escuela_id = request.user.escuela_id or -1
        return {
            "desde": _fecha(qp.get("desde"), "desde"),
            "hasta": _fecha(qp.get("hasta"), "hasta"),
            "escuela_id": escuela_id,
            "curso_id": _entero(qp.get("curso_id"), "curso_id"),
            "estudiante_id": _entero(qp.get("estudiante_id"), "estudiante_id"),
            "ambito": ambito,
            "entregadas": qp.get("entregadas", "1") not in ("0", "false", "no"),
        }

    def get(self, request):
        try:
            filtros = self._filtros(request)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        qs = services.filtrar_pruebas_analitica(
            self.modelo.objects.all(), prefijo=self.prefijo, **filtros
        )

        if (request.query_params.get("formato") or "").lower() == "csv":
            return self._csv(qs)

        paginador = _Paginacion()
        pagina = paginador.paginate_queryset(qs.order_by("-pk"), request, view=self)
        ids = [obj.pk for obj in pagina]
        filas = list(self.filas(self.modelo.objects.filter(pk__in=ids).order_by("-pk")))
        return paginador.get_paginated_response(filas)

    def _csv(self, qs):
        escritor = csv.DictWriter(_Eco(), fieldnames=self.columnas)

        def generar():
            yield "﻿"  # BOM: Excel abre bien los acentos.
            yield escritor.writerow(dict(zip(self.columnas, self.columnas)))
            for fila in self.filas(qs):
                yield escritor.writerow(fila)

        nombre = f"{self.nombre_archivo}_{timezone.localdate().isoformat()}.csv"
        resp = StreamingHttpResponse(generar(), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{nombre}"'
        return resp


class AnaliticaPruebasView(_AnaliticaBase):
    """GET accounts/analitica/pruebas/ — una fila por prueba."""

    modelo = Prueba
    prefijo = ""
    columnas = services.COLUMNAS_ANALITICA_PRUEBAS
    nombre_archivo = "pruebas"

    def filas(self, qs):
        return services.filas_analitica_pruebas(qs)


class AnaliticaRespuestasView(_AnaliticaBase):
    """GET accounts/analitica/respuestas/ — una fila por respuesta."""

    modelo = PruebaEjercicio
    prefijo = "prueba__"
    columnas = services.COLUMNAS_ANALITICA_RESPUESTAS
    nombre_archivo = "respuestas"

    def filas(self, qs):
        return services.filas_analitica_respuestas(qs)
