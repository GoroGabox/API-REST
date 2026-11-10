"""Análisis comercial para el panel de admin (sección Comercial).

Funciones puras sobre el ORM, sin request: las usan `sales/views_admin.py` y
los tests. Solo cuentan ventas pagadas (`ESTADOS_PAGADOS`). Montos en CLP
enteros. "B2C" = compra individual de curso (`item_type='curso'`); "B2B" =
llaves/suscripciones de escuelas (`item_type='producto'`).
"""
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Count, Max, Q, Sum
from django.db.models.functions import TruncDate, TruncWeek
from django.utils import timezone

from accounts.models import Certificado, EstudianteLeccion, Prueba, Usuario
from schools.models import Escuela, PlanCurso

from .constants import ESTADOS_PAGADOS
from .models import (
    CambioPrecio, EstudianteCurso, IntentoPago, MovimientoLlaves, Producto, Venta,
)
from .services import INTENTO_ABANDONO, estado_intento, suscripcion_vigente

# --- Umbrales de Oportunidades (ajustables) ---------------------------------
SEMANAS_SALDO_ALERTA = 2       # saldo por agotarse: se acaba antes de N semanas
SEMANAS_A_CUBRIR = 4           # la sugerencia de compra cubre N semanas de consumo
SEMANAS_CONSUMO = 8            # ventana para medir el consumo semanal
DIAS_UPSELL = 60               # ventana para detectar compras repetidas de packs chicos
DIAS_INACTIVA = 60             # sin compras en N días
DIAS_SIN_USO = 30              # sin gastar llaves en N días
ALUMNOS_PARA_SUSCRIPCION = 20  # alumnos activos por llave para sugerir suscripción
DIAS_AVISO_VENCE = 7           # acceso B2C que vence en N días
DIAS_REACTIVACION = 60         # acceso B2C vencido hace como máximo N días
DIAS_ABANDONO = 30             # pagos no concretados en los últimos N días


def pagadas():
    return Venta.objects.filter(payment_status__in=ESTADOS_PAGADOS)


def _int(v):
    return int(round(float(v or 0)))


def _delta(actual, anterior):
    """Variación % (entero) o None si no hay base de comparación."""
    if not anterior:
        return None
    return round((actual - anterior) * 100 / anterior)


def nombre_usuario(u):
    if not u:
        return ""
    return f"{u.nombre} {u.apellido}".strip() or u.email


def item_de(v):
    return v.producto.nombre if v.producto_id else (v.curso.nombre if v.curso_id else "—")


def precio_final(valor, descuento):
    """Paridad con `services.precio_final_producto`, sobre valores sueltos."""
    if valor is None:
        return None
    base = Decimal(str(valor))
    d = Decimal(str(descuento or 0))
    return int((base * (1 - d / 100)).quantize(Decimal("1"))) if d > 0 else int(base)


# --- Recomendación de compra (port de recomendarCompra del front) -----------
def recomendar_compra(necesarias, packs):
    """Combinación de packs más barata que cubre `necesarias` llaves.

    Mismo criterio que `src/utils/producto.js::recomendarCompra` del front:
    menor costo; en empate, menos compras y luego menos sobrantes.
    Devuelve {items: [(pack, veces)], costo, llaves, sobrantes} o None.
    """
    n = int(math.ceil(necesarias or 0))
    validos = [p for p in packs if (p.cant_basic_key or 0) > 0]
    if n <= 0 or not validos or n > 100000:
        return None
    precios = [precio_final(p.valor_neto, p.descuento) or 0 for p in validos]
    cants = [p.cant_basic_key for p in validos]
    costo = [0] * (n + 1)
    compras = [0] * (n + 1)
    llaves = [0] * (n + 1)
    eleccion = [-1] * (n + 1)
    for k in range(1, n + 1):
        mejor = None
        for i, cant in enumerate(cants):
            resto = max(0, k - cant)
            cand = (precios[i] + costo[resto], 1 + compras[resto], cant + llaves[resto], i)
            if mejor is None or cand[:3] < mejor[:3]:
                mejor = cand
        costo[k], compras[k], llaves[k], eleccion[k] = mejor
    veces = Counter()
    k = n
    while k > 0:
        i = eleccion[k]
        veces[i] += 1
        k = max(0, k - cants[i])
    items = sorted(((validos[i], v) for i, v in veces.items()), key=lambda t: -t[0].cant_basic_key)
    return {"items": items, "costo": costo[n], "llaves": llaves[n], "sobrantes": llaves[n] - n}


def texto_compra(rec):
    if not rec:
        return None
    partes = [f"{v} × {p.nombre}" if v > 1 else p.nombre for p, v in rec["items"]]
    return f"{' + '.join(partes)} (${rec['costo']:,})".replace(",", ".")


def packs_llave():
    return list(Producto.objects.filter(tipo="llave", activo=True, cant_basic_key__gt=0))


# --- Panorama ----------------------------------------------------------------
def _kpis(qs):
    agg = qs.aggregate(total=Sum("monto_pagado"), n=Count("id"), compradores=Count("usuario", distinct=True))
    ingresos, ventas = _int(agg["total"]), agg["n"] or 0
    return {
        "ingresos": ingresos,
        "ventas": ventas,
        "ticket_promedio": round(ingresos / ventas) if ventas else 0,
        "compradores": agg["compradores"] or 0,
    }


def resumen_intentos(desde, hasta, now=None):
    now = now or timezone.now()
    qs = IntentoPago.objects.filter(creado_en__gte=desde, creado_en__lt=hasta)
    cuenta = Counter(estado_intento(i, now) for i in qs.only("estado", "creado_en"))
    cerrados = cuenta["autorizado"] + cuenta["rechazado"] + cuenta["abandonado"]
    return {
        "total": sum(cuenta.values()),
        "autorizados": cuenta["autorizado"],
        "rechazados": cuenta["rechazado"],
        "abandonados": cuenta["abandonado"],
        "en_curso": cuenta["iniciado"],
        "conversion": round(cuenta["autorizado"] * 100 / cerrados) if cerrados else None,
    }


def panorama(desde, hasta):
    largo = hasta - desde
    actual = pagadas().filter(fecha_venta__gte=desde, fecha_venta__lt=hasta)
    anterior = pagadas().filter(fecha_venta__gte=desde - largo, fecha_venta__lt=desde)
    k_act, k_ant = _kpis(actual), _kpis(anterior)

    # Clientes que vuelven: compradores del período que ya habían comprado antes.
    compradores = set(actual.exclude(usuario=None).values_list("usuario", flat=True))
    previos = set(
        pagadas().filter(fecha_venta__lt=desde, usuario__in=compradores).values_list("usuario", flat=True)
    )
    k_act["recompra"] = round(len(previos) * 100 / len(compradores)) if compradores else None

    canales = {}
    for canal, filtro in (("b2c", Q(item_type="curso")), ("b2b", ~Q(item_type="curso"))):
        canales[canal] = _kpis(actual.filter(filtro))

    semanal = largo.days > 92
    trunc = TruncWeek if semanal else TruncDate
    filas = (
        actual.annotate(b=trunc("fecha_venta"))
        .values("b", "item_type")
        .annotate(monto=Sum("monto_pagado"), n=Count("id"))
    )
    buckets = defaultdict(lambda: {"b2c": 0, "b2b": 0, "ventas": 0})
    for f in filas:
        clave = f["b"].date() if hasattr(f["b"], "date") else f["b"]
        canal = "b2c" if f["item_type"] == "curso" else "b2b"
        buckets[clave][canal] += _int(f["monto"])
        buckets[clave]["ventas"] += f["n"]
    serie = []
    paso = timedelta(days=7 if semanal else 1)
    cursor = desde.date()
    if semanal:
        cursor -= timedelta(days=cursor.weekday())
    ultimo_dia = (hasta - timedelta(microseconds=1)).date()
    while cursor <= ultimo_dia:
        b = buckets.get(cursor, {"b2c": 0, "b2b": 0, "ventas": 0})
        serie.append({"fecha": cursor.isoformat(), **b})
        cursor += paso
        if len(serie) > 400:
            break

    def top(campo, nombre):
        return [
            {"id": r[campo], "nombre": r[nombre], "ingresos": _int(r["ingresos"]), "ventas": r["ventas"]}
            for r in actual.filter(**{f"{campo}__isnull": False})
            .values(campo, nombre)
            .annotate(ingresos=Sum("monto_pagado"), ventas=Count("id"))
            .order_by("-ingresos")[:5]
        ]

    mezcla = [
        {"dias": r["dias"], "ventas": r["ventas"], "ingresos": _int(r["ingresos"])}
        for r in actual.filter(item_type="curso").values("dias")
        .annotate(ventas=Count("id"), ingresos=Sum("monto_pagado")).order_by("dias")
    ]

    return {
        "desde": desde.isoformat(),
        "hasta": hasta.isoformat(),
        "kpis": k_act,
        "anterior": k_ant,
        "variacion": {k: _delta(k_act[k], k_ant[k]) for k in ("ingresos", "ventas", "ticket_promedio", "compradores")},
        "canales": canales,
        "granularidad": "semana" if semanal else "dia",
        "serie": serie,
        "top_cursos": top("curso", "curso__nombre"),
        "top_paquetes": top("producto", "producto__nombre"),
        "mezcla_planes": mezcla,
        "intentos": resumen_intentos(desde, hasta),
    }


# --- Ventas ------------------------------------------------------------------
def filtrar_ventas(params, base=None):
    qs = (base if base is not None else pagadas()).select_related("producto", "curso", "escuela", "usuario")
    g = params.get
    if g("desde"):
        qs = qs.filter(fecha_venta__gte=g("desde"))
    if g("hasta"):
        qs = qs.filter(fecha_venta__lt=g("hasta"))
    canal = (g("canal") or "").lower()
    if canal == "b2c":
        qs = qs.filter(item_type="curso")
    elif canal == "b2b":
        qs = qs.exclude(item_type="curso")
    for campo in ("producto", "curso", "escuela", "usuario", "dias"):
        if g(campo):
            qs = qs.filter(**{f"{campo}_id" if campo != "dias" else "dias": g(campo)})
    if g("monto_min"):
        qs = qs.filter(monto_pagado__gte=g("monto_min"))
    if g("monto_max"):
        qs = qs.filter(monto_pagado__lte=g("monto_max"))
    q = (g("q") or "").strip()
    if q:
        filtro = (
            Q(usuario__email__icontains=q) | Q(usuario__nombre__icontains=q)
            | Q(usuario__apellido__icontains=q) | Q(buy_order__icontains=q)
            | Q(escuela__nombre__icontains=q)
        )
        if q.isdigit():
            filtro |= Q(id=int(q))
        qs = qs.filter(filtro)
    orden = {"fecha": "fecha_venta", "-fecha": "-fecha_venta", "monto": "monto_pagado", "-monto": "-monto_pagado"}
    return qs.order_by(orden.get(g("orden") or "-fecha", "-fecha_venta"), "-id")


def fila_venta(v):
    u = v.usuario
    return {
        "id": v.id,
        "fecha": v.fecha_venta.isoformat() if v.fecha_venta else None,
        "comprador": {
            "id": u.id, "nombre": nombre_usuario(u), "email": u.email, "telefono": u.telefono or "",
        } if u else None,
        "escuela": {"id": v.escuela_id, "nombre": v.escuela.nombre} if v.escuela_id else None,
        "canal": "b2c" if v.item_type == "curso" else "b2b",
        "item": item_de(v),
        "producto_id": v.producto_id,
        "curso_id": v.curso_id,
        "dias": v.dias,
        "monto": _int(v.monto_pagado),
        "pay_system": v.pay_system,
        "payment_status": v.payment_status,
        "buy_order": v.buy_order,
    }


def fila_intento(i, now=None):
    return {
        "id": i.id,
        "fecha": i.creado_en.isoformat(),
        "resuelto_en": i.resuelto_en.isoformat() if i.resuelto_en else None,
        "estado": estado_intento(i, now),
        "codigo_respuesta": i.codigo_respuesta,
        "comprador": {
            "id": i.usuario_id, "nombre": nombre_usuario(i.usuario), "email": i.usuario.email,
            "telefono": i.usuario.telefono or "",
        } if i.usuario_id else None,
        "escuela": {"id": i.escuela_id, "nombre": i.escuela.nombre} if i.escuela_id else None,
        "canal": "b2c" if i.item_type == "curso" else "b2b",
        "item": i.producto.nombre if i.producto_id else (i.curso.nombre if i.curso_id else "—"),
        "dias": i.dias,
        "monto": i.monto,
        "buy_order": i.buy_order,
        "venta_id": i.venta_id,
    }


def filtrar_intentos(params, now=None):
    now = now or timezone.now()
    qs = IntentoPago.objects.select_related("usuario", "escuela", "producto", "curso")
    g = params.get
    if g("desde"):
        qs = qs.filter(creado_en__gte=g("desde"))
    if g("hasta"):
        qs = qs.filter(creado_en__lt=g("hasta"))
    estado = (g("estado") or "").lower()
    limite = now - INTENTO_ABANDONO
    if estado == "abandonado":
        qs = qs.filter(estado="iniciado", creado_en__lte=limite)
    elif estado == "iniciado":
        qs = qs.filter(estado="iniciado", creado_en__gt=limite)
    elif estado in ("autorizado", "rechazado"):
        qs = qs.filter(estado=estado)
    canal = (g("canal") or "").lower()
    if canal == "b2c":
        qs = qs.filter(item_type="curso")
    elif canal == "b2b":
        qs = qs.exclude(item_type="curso")
    q = (g("q") or "").strip()
    if q:
        qs = qs.filter(Q(usuario__email__icontains=q) | Q(usuario__nombre__icontains=q) | Q(buy_order__icontains=q))
    return qs.order_by("-creado_en")


# --- Precios -----------------------------------------------------------------
def _precio_de_cambio(c):
    if c.tipo == "producto":
        return precio_final(c.precio_nuevo, c.descuento_nuevo)
    return c.precio_nuevo


def evolucion_precio(tipo, objeto_id, desde, hasta):
    """Escalones de precio (CambioPrecio) + ventas semanales del ítem."""
    cambios = list(
        CambioPrecio.objects.filter(tipo=tipo, objeto_id=objeto_id).select_related("usuario").order_by("fecha", "id")
    )
    if tipo == "plan_curso":
        plan = PlanCurso.objects.select_related("curso").filter(pk=objeto_id).first()
        if plan is None:
            return None
        nombre = f"{plan.curso.nombre} · {plan.dias} días"
        ventas = pagadas().filter(curso_id=plan.curso_id, dias=plan.dias)
        actual = plan.precio
    else:
        prod = Producto.objects.filter(pk=objeto_id).first()
        if prod is None:
            return None
        nombre = prod.nombre
        ventas = pagadas().filter(producto_id=objeto_id)
        actual = precio_final(prod.valor_neto, prod.descuento)

    ventas = ventas.filter(fecha_venta__gte=desde, fecha_venta__lt=hasta)
    semanas = {
        (r["s"].date() if hasattr(r["s"], "date") else r["s"]): r
        for r in ventas.annotate(s=TruncWeek("fecha_venta")).values("s")
        .annotate(n=Count("id"), monto=Sum("monto_pagado"))
    }
    serie = []
    cursor = desde.date() - timedelta(days=desde.date().weekday())
    ultimo_dia = (hasta - timedelta(microseconds=1)).date()
    while cursor <= ultimo_dia and len(serie) < 120:
        r = semanas.get(cursor)
        n = r["n"] if r else 0
        monto = _int(r["monto"]) if r else 0
        serie.append({
            "semana": cursor.isoformat(), "ventas": n, "ingresos": monto,
            "precio_efectivo": round(monto / n) if n else None,
        })
        cursor += timedelta(days=7)

    return {
        "tipo": tipo,
        "id": objeto_id,
        "nombre": nombre,
        "precio_actual": actual,
        "cambios": [
            {
                "fecha": c.fecha.isoformat(),
                "precio": _precio_de_cambio(c),
                "precio_base": c.precio_nuevo,
                "referencia": c.referencia_nueva,
                "descuento": float(c.descuento_nuevo) if c.descuento_nuevo is not None else None,
                "activo": c.activo,
                "usuario": nombre_usuario(c.usuario) if c.usuario_id else None,
            }
            for c in cambios
        ],
        "semanas": serie,
    }


def fila_cambio(c):
    antes = precio_final(c.precio_anterior, c.descuento_anterior) if c.tipo == "producto" else c.precio_anterior
    despues = _precio_de_cambio(c)
    return {
        "id": c.id,
        "fecha": c.fecha.isoformat(),
        "tipo": c.tipo,
        "objeto_id": c.objeto_id,
        "nombre": c.nombre,
        "precio_anterior": antes,
        "precio_nuevo": despues,
        "variacion": _delta(despues, antes) if (antes and despues is not None) else None,
        "referencia_anterior": c.referencia_anterior,
        "referencia_nueva": c.referencia_nueva,
        "descuento_anterior": float(c.descuento_anterior) if c.descuento_anterior is not None else None,
        "descuento_nuevo": float(c.descuento_nuevo) if c.descuento_nuevo is not None else None,
        "activo": c.activo,
        "es_alta": c.precio_anterior is None and c.descuento_anterior is None,
        "usuario": nombre_usuario(c.usuario) if c.usuario_id else None,
    }


# --- Cartera -----------------------------------------------------------------
def _accesos_vigentes(now):
    return EstudianteCurso.objects.filter(access_key_id__status="active").filter(
        Q(access_key_id__valid_until__isnull=True) | Q(access_key_id__valid_until__gte=now)
    )


def cartera_escuelas(now=None):
    now = now or timezone.now()
    ventana = now - timedelta(weeks=SEMANAS_CONSUMO)

    compras = defaultdict(list)
    for v in pagadas().filter(escuela__isnull=False).exclude(item_type="curso").select_related("producto").order_by("fecha_venta"):
        compras[v.escuela_id].append(v)
    consumo = {
        r["escuela"]: -(r["s"] or 0)
        for r in MovimientoLlaves.objects.filter(fecha__gte=ventana).values("escuela").annotate(s=Sum("cantidad"))
    }
    ultimo_uso = dict(
        MovimientoLlaves.objects.filter(cantidad__lt=0).values("escuela").annotate(u=Max("fecha")).values_list("escuela", "u")
    )
    activos = dict(
        _accesos_vigentes(now).filter(estudiante_id__escuela__isnull=False)
        .values("estudiante_id__escuela").annotate(n=Count("estudiante_id", distinct=True))
        .values_list("estudiante_id__escuela", "n")
    )
    finales = {
        r["estudiante__escuela"]: r
        for r in Prueba.objects.examenes_finales()
        .filter(completada_en__isnull=False, completada_en__gte=now - timedelta(days=90), estudiante__escuela__isnull=False)
        .values("estudiante__escuela")
        .annotate(rendidos=Count("id"), aprobados=Count("id", filter=Q(aprobado=True)))
    }
    directores = defaultdict(list)
    for d in Usuario.objects.filter(is_director=True, escuela__isnull=False).only("escuela", "email", "telefono", "nombre", "apellido"):
        directores[d.escuela_id].append(d)

    filas = []
    for e in Escuela.objects.all().order_by("nombre"):
        vs = compras.get(e.id, [])
        total = sum(_int(v.monto_pagado) for v in vs)
        fechas = [v.fecha_venta for v in vs]
        gaps = [(b - a).days for a, b in zip(fechas, fechas[1:])]
        pack = Counter(v.producto.nombre for v in vs if v.producto_id).most_common(1)
        semanal = round(consumo.get(e.id, 0) / SEMANAS_CONSUMO, 1)
        semanas_saldo = round(e.basic_key / semanal, 1) if semanal > 0 else None
        fin = finales.get(e.id)
        director = (directores.get(e.id) or [None])[0]
        filas.append({
            "id": e.id,
            "nombre": e.nombre,
            "email": e.email or (director.email if director else ""),
            "telefono": e.telefono or (director.telefono if director else "") or "",
            "director": nombre_usuario(director) if director else None,
            "compras": len(vs),
            "total": total,
            "primera_compra": fechas[0].isoformat() if fechas else None,
            "ultima_compra": fechas[-1].isoformat() if fechas else None,
            "dias_entre_compras": round(sum(gaps) / len(gaps)) if gaps else None,
            "pack_habitual": pack[0][0] if pack else None,
            "saldo": e.basic_key,
            "consumo_semanal": semanal,
            "semanas_saldo": semanas_saldo,
            "ultimo_uso": ultimo_uso[e.id].isoformat() if e.id in ultimo_uso else None,
            "alumnos_activos": activos.get(e.id, 0),
            "examen_final": {"rendidos": fin["rendidos"], "aprobados": fin["aprobados"]} if fin else None,
            "suscripcion": {
                "activa": suscripcion_vigente(e, now),
                "cupos_usados": e.basic_seats_used,
                "cupos_max": e.basic_seats_max,
                "hasta": e.basic_access_until.isoformat() if e.basic_access_until else None,
            },
        })
    return filas


def cartera_estudiantes(params=None, now=None):
    """Compradores B2C (compra individual de curso)."""
    now = now or timezone.now()
    params = params or {}
    ventas = pagadas().filter(item_type="curso", usuario__isnull=False).select_related("curso")
    por_usuario = defaultdict(list)
    for v in ventas.order_by("fecha_venta"):
        por_usuario[v.usuario_id].append(v)
    if not por_usuario:
        return []

    usuarios = Usuario.objects.filter(id__in=por_usuario.keys()).select_related("escuela")
    q = (params.get("q") or "").strip()
    if q:
        usuarios = usuarios.filter(Q(email__icontains=q) | Q(nombre__icontains=q) | Q(apellido__icontains=q))

    accesos = defaultdict(list)
    for ec in (
        EstudianteCurso.objects.filter(estudiante_id__in=por_usuario.keys(), access_key_id__origen="purchase")
        .select_related("access_key_id", "curso_id")
    ):
        accesos[ec.estudiante_id_id].append(ec)
    lecciones = dict(
        EstudianteLeccion.objects.filter(estudiante_id__in=por_usuario.keys())
        .values("estudiante").annotate(n=Count("id")).values_list("estudiante", "n")
    )
    aprobados = set(
        Certificado.objects.filter(estudiante_id__in=por_usuario.keys()).values_list("estudiante_id", flat=True)
    )

    filas = []
    for u in usuarios:
        vs = por_usuario[u.id]
        cursos = Counter(v.curso_id for v in vs)
        vigentes = [
            ec for ec in accesos.get(u.id, [])
            if ec.access_key_id and ec.access_key_id.status == "active"
            and (ec.access_key_id.valid_until is None or ec.access_key_id.valid_until >= now)
        ]
        vence = min((ec.access_key_id.valid_until for ec in vigentes if ec.access_key_id.valid_until), default=None)
        plan = Counter(v.dias for v in vs if v.dias).most_common(1)
        filas.append({
            "id": u.id,
            "nombre": nombre_usuario(u),
            "email": u.email,
            "telefono": u.telefono or "",
            "escuela": u.escuela.nombre if u.escuela_id else None,
            "compras": len(vs),
            "renovaciones": sum(n - 1 for n in cursos.values()),
            "total": sum(_int(v.monto_pagado) for v in vs),
            "plan_preferido": plan[0][0] if plan else None,
            "primera_compra": vs[0].fecha_venta.isoformat(),
            "ultima_compra": vs[-1].fecha_venta.isoformat(),
            "acceso_vigente": bool(vigentes),
            "vence": vence.isoformat() if vence else None,
            "lecciones_completadas": lecciones.get(u.id, 0),
            "ultimo_dia_activo": u.last_active_date.isoformat() if u.last_active_date else None,
            "certificado": u.id in aprobados,
        })
    filas.sort(key=lambda f: f["ultima_compra"], reverse=True)
    return filas


# --- Oportunidades -----------------------------------------------------------
def _caso(id_, nombre, email, telefono, motivo, sugerencia, valor=None, extra=None):
    return {
        "id": id_, "nombre": nombre, "email": email or "", "telefono": telefono or "",
        "motivo": motivo, "sugerencia": sugerencia, "valor": valor, **(extra or {}),
    }


def oportunidades(now=None):
    now = now or timezone.now()
    hoy = timezone.localdate()
    packs = packs_llave()
    escuelas = cartera_escuelas(now)
    reglas = []

    # 1. Saldo por agotarse.
    casos = []
    for e in escuelas:
        if e["semanas_saldo"] is not None and e["semanas_saldo"] < SEMANAS_SALDO_ALERTA:
            faltan = max(math.ceil(e["consumo_semanal"] * SEMANAS_A_CUBRIR - e["saldo"]), 1)
            rec = recomendar_compra(faltan, packs)
            casos.append(_caso(
                e["id"], e["nombre"], e["email"], e["telefono"],
                f"Le quedan {e['saldo']} llaves y usa ~{e['consumo_semanal']} por semana "
                f"(≈{e['semanas_saldo']} semanas).",
                f"Ofrecer {texto_compra(rec)} para cubrir {SEMANAS_A_CUBRIR} semanas." if rec else "Ofrecer un pack de llaves.",
                valor=rec["costo"] if rec else None,
            ))
    reglas.append({
        "clave": "saldo_por_agotarse", "segmento": "escuela",
        "titulo": "Saldo por agotarse",
        "descripcion": f"Escuelas que se quedan sin llaves en menos de {SEMANAS_SALDO_ALERTA} semanas al ritmo actual.",
        "casos": sorted(casos, key=lambda c: c["valor"] or 0, reverse=True),
    })

    # 2. Upsell de volumen: compras repetidas de packs chicos.
    casos = []
    desde = now - timedelta(days=DIAS_UPSELL)
    recientes = defaultdict(list)
    for v in pagadas().filter(fecha_venta__gte=desde, producto__tipo="llave", escuela__isnull=False).select_related("producto"):
        recientes[v.escuela_id].append(v)
    por_id = {e["id"]: e for e in escuelas}
    for eid, vs in recientes.items():
        if len(vs) < 2:
            continue
        llaves = sum(v.producto.cant_basic_key or 0 for v in vs)
        pagado = sum(_int(v.monto_pagado) for v in vs)
        rec = recomendar_compra(llaves, packs)
        if rec and rec["costo"] < pagado:
            e = por_id.get(eid)
            if not e:
                continue
            casos.append(_caso(
                eid, e["nombre"], e["email"], e["telefono"],
                f"Compró {len(vs)} veces en {DIAS_UPSELL} días ({llaves} llaves por ${pagado:,}).".replace(",", "."),
                f"Ofrecer {texto_compra(rec)}: habría ahorrado ${pagado - rec['costo']:,}.".replace(",", "."),
                valor=pagado - rec["costo"],
            ))
    reglas.append({
        "clave": "upsell_volumen", "segmento": "escuela",
        "titulo": "Conviene un pack más grande",
        "descripcion": f"Escuelas que compraron varias veces en {DIAS_UPSELL} días y habrían pagado menos con un pack mayor.",
        "casos": sorted(casos, key=lambda c: c["valor"] or 0, reverse=True),
    })

    # 3. Escuelas inactivas.
    casos = []
    limite_compra = now - timedelta(days=DIAS_INACTIVA)
    limite_uso = now - timedelta(days=DIAS_SIN_USO)
    for e in escuelas:
        if not e["compras"]:
            continue
        ultima = datetime.fromisoformat(e["ultima_compra"])
        uso = datetime.fromisoformat(e["ultimo_uso"]) if e["ultimo_uso"] else None
        if ultima < limite_compra and (uso is None or uso < limite_uso):
            casos.append(_caso(
                e["id"], e["nombre"], e["email"], e["telefono"],
                f"Última compra hace {(now - ultima).days} días"
                + (f"; no usa llaves hace {(now - uso).days} días." if uso else "; no registra uso de llaves."),
                "Contactar para reactivar (¿nuevo curso, cambio de encargado?).",
                valor=e["total"],
            ))
    reglas.append({
        "clave": "escuela_inactiva", "segmento": "escuela",
        "titulo": "Escuelas inactivas",
        "descripcion": f"Compraban y llevan más de {DIAS_INACTIVA} días sin comprar ni {DIAS_SIN_USO} días sin usar llaves.",
        "casos": sorted(casos, key=lambda c: c["valor"] or 0, reverse=True),
    })

    # 4. Candidatas a suscripción.
    casos = []
    for e in escuelas:
        if not e["suscripcion"]["activa"] and e["alumnos_activos"] >= ALUMNOS_PARA_SUSCRIPCION:
            casos.append(_caso(
                e["id"], e["nombre"], e["email"], e["telefono"],
                f"{e['alumnos_activos']} alumnos con acceso vigente, sin suscripción.",
                "Ofrecer una suscripción con cupos reasignables.",
                valor=e["alumnos_activos"],
            ))
    reglas.append({
        "clave": "candidata_suscripcion", "segmento": "escuela",
        "titulo": "Candidatas a suscripción",
        "descripcion": f"Escuelas sin suscripción con {ALUMNOS_PARA_SUSCRIPCION} o más alumnos activos.",
        "casos": sorted(casos, key=lambda c: c["valor"] or 0, reverse=True),
    })

    planes = defaultdict(dict)
    for p in PlanCurso.objects.filter(activo=True):
        planes[p.curso_id][p.dias] = p.precio

    # 5. Acceso B2C por vencer con actividad reciente.
    casos = []
    for ec in (
        EstudianteCurso.objects.filter(
            access_key_id__origen="purchase", access_key_id__status="active",
            access_key_id__valid_until__gte=now,
            access_key_id__valid_until__lte=now + timedelta(days=DIAS_AVISO_VENCE),
            estudiante_id__last_active_date__gte=hoy - timedelta(days=7),
        ).select_related("estudiante_id", "curso_id", "access_key_id")
    ):
        u, c = ec.estudiante_id, ec.curso_id
        dias = (ec.access_key_id.valid_until - now).days
        opciones = planes.get(c.id, {})
        plan = 14 if 14 in opciones else (35 if 35 in opciones else 7)
        precio = opciones.get(plan)
        casos.append(_caso(
            u.id, nombre_usuario(u), u.email, u.telefono,
            f"Su acceso a {c.nombre} vence en {dias} días y estudió esta semana.",
            f"Ofrecer el plan de {plan} días" + (f" (${precio:,})".replace(",", ".") if precio else "") + ".",
            valor=precio, extra={"curso": c.nombre},
        ))
    reglas.append({
        "clave": "acceso_por_vencer", "segmento": "estudiante",
        "titulo": "Acceso por vencer",
        "descripcion": f"Estudiantes activos cuyo acceso comprado vence en {DIAS_AVISO_VENCE} días o menos.",
        "casos": casos,
    })

    # 6. Acceso vencido sin certificado.
    casos = []
    con_certificado = set(Certificado.objects.values_list("estudiante_id", "curso_id"))
    for ec in (
        EstudianteCurso.objects.filter(
            access_key_id__origen="purchase",
            access_key_id__valid_until__lt=now,
            access_key_id__valid_until__gte=now - timedelta(days=DIAS_REACTIVACION),
        ).select_related("estudiante_id", "curso_id", "access_key_id")
    ):
        u, c = ec.estudiante_id, ec.curso_id
        if (u.id, c.id) in con_certificado:
            continue
        precio = planes.get(c.id, {}).get(7)
        casos.append(_caso(
            u.id, nombre_usuario(u), u.email, u.telefono,
            f"Su acceso a {c.nombre} venció hace {(now - ec.access_key_id.valid_until).days} días sin aprobar la Prueba Final.",
            "Ofrecer una semana más para terminar" + (f" (${precio:,})".replace(",", ".") if precio else "") + ".",
            valor=precio, extra={"curso": c.nombre},
        ))
    reglas.append({
        "clave": "reactivacion", "segmento": "estudiante",
        "titulo": "Vencidos sin aprobar",
        "descripcion": f"Acceso comprado que venció en los últimos {DIAS_REACTIVACION} días sin certificado.",
        "casos": casos,
    })

    # 7. Pagos no concretados sin compra posterior.
    casos = []
    vistos = set()
    limite = now - INTENTO_ABANDONO
    fallidos = (
        IntentoPago.objects.filter(creado_en__gte=now - timedelta(days=DIAS_ABANDONO), usuario__isnull=False)
        .filter(Q(estado="rechazado") | Q(estado="iniciado", creado_en__lte=limite))
        .select_related("usuario", "producto", "curso", "escuela").order_by("-creado_en")
    )
    for i in fallidos:
        clave = (i.usuario_id, i.producto_id, i.curso_id)
        if clave in vistos:
            continue
        vistos.add(clave)
        compro = pagadas().filter(
            usuario_id=i.usuario_id, fecha_venta__gte=i.creado_en,
            producto_id=i.producto_id, curso_id=i.curso_id,
        ).exists()
        if compro:
            continue
        item = i.producto.nombre if i.producto_id else (i.curso.nombre if i.curso_id else "—")
        estado = estado_intento(i, now)
        casos.append(_caso(
            i.usuario_id, nombre_usuario(i.usuario), i.usuario.email, i.usuario.telefono,
            f"{'Rechazado' if estado == 'rechazado' else 'Abandonó'} el pago de {item} "
            f"(${i.monto:,}) hace {(now - i.creado_en).days} días.".replace(",", "."),
            "Contactar para retomar la compra.",
            valor=i.monto, extra={"segmento_real": "escuela" if i.item_type == "producto" else "estudiante"},
        ))
    reglas.append({
        "clave": "pago_no_concretado", "segmento": "mixto",
        "titulo": "Pagos no concretados",
        "descripcion": f"Pagos rechazados o abandonados en {DIAS_ABANDONO} días sin una compra posterior.",
        "casos": casos,
    })

    return reglas
