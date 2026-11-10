from django.db import models
from django.utils import timezone
from schools.models import Escuela, Curso
from accounts.models import Usuario
import uuid

### 📜 Productos ###
class Producto(models.Model):
    id = models.AutoField(primary_key=True, auto_created=True)
    nombre = models.CharField(max_length=50)
    tipo = models.CharField(max_length=20, choices=[('llave', 'Llave'), ('suscripcion', 'Suscripción')])
    valor_neto = models.DecimalField(max_digits=10, decimal_places=2, null=True)
    descuento = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    currency = models.CharField(max_length=3, default='CLP')
    descripcion = models.TextField()
    
    cant_basic_key = models.IntegerField(default=0, blank=True, null=True)
    basic_access = models.BooleanField(default=False)
    # Suscripción: cupos que suma a `Escuela.basic_seats_max` y días de vigencia
    # que extiende `Escuela.basic_access_until` (null = sin vencimiento).
    cant_seats = models.IntegerField(default=0)
    duracion_dias = models.IntegerField(null=True, blank=True)
    # Permite ocultar un paquete del catálogo sin borrarlo (control admin).
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return self.nombre

ITEM_TYPES = [('curso', 'Curso'), ('producto', 'Producto')]


### 🛒 Venta ###
class Venta(models.Model):
    id = models.AutoField(primary_key=True, auto_created=True)
    # Una venta refiere a un Producto (llave/suscripción, típicamente compra de
    # escuela) O a un Curso (compra individual de un estudiante). Exactamente uno
    # de los dos según el tipo de compra.
    producto = models.ForeignKey(Producto, on_delete=models.SET_NULL, null=True, blank=True)
    curso = models.ForeignKey(Curso, on_delete=models.SET_NULL, null=True, blank=True)
    escuela = models.ForeignKey(Escuela, on_delete=models.CASCADE, null=True)
    usuario = models.ForeignKey(Usuario, on_delete=models.CASCADE, null=True)
    fecha_venta = models.DateTimeField(auto_now_add=True)
    monto_pagado = models.DecimalField(max_digits=10, decimal_places=2)
    pay_system = models.CharField(max_length=20)
    payment_status = models.CharField(max_length=20, default='pending')
    # Datos de la compra para el análisis comercial (antes solo vivían dentro
    # del buy_order de TransbankTransaction). `item_type`: 'curso' (B2C) o
    # 'producto' (B2B: llaves/suscripción); `dias`: plan del curso (7/14/35).
    item_type = models.CharField(max_length=10, choices=ITEM_TYPES, null=True, blank=True)
    dias = models.PositiveIntegerField(null=True, blank=True)
    buy_order = models.CharField(max_length=255, null=True, blank=True, db_index=True)

    def __str__(self):
        item = self.producto.nombre if self.producto else (self.curso.nombre if self.curso else "—")
        currency = self.producto.currency if self.producto else "CLP"
        return f"Venta {self.id} - {item} ({self.monto_pagado} {currency})"

### 🔑 Llaves de Acceso ###
class AccessKey(models.Model):
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('used', 'Used'),
        ('revoked', 'Revoked'),
    ]
    ORIGEN_CHOICES = [
        ('key', 'Llave temporal'),
        ('seat', 'Cupo de suscripción'),
        ('purchase', 'Compra individual'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=64, unique=True, blank=True, null=True)
    valid_from = models.DateTimeField(auto_now_add=True, null=True)
    # NULL = sin expiración (típico de origen='seat').
    valid_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='active')
    origen = models.CharField(max_length=10, choices=ORIGEN_CHOICES, default='key')
    # Códigos canjeables generados por un director (ver services.generar_codigos):
    # escuela emisora, días de acceso que otorga al canjearse (la vigencia corre
    # desde el canje; antes, valid_until es el vencimiento del código) y llaves
    # descontadas del saldo (se devuelven al anular). Null/0 en las demás llaves.
    escuela = models.ForeignKey(
        'schools.Escuela', null=True, blank=True, on_delete=models.SET_NULL, related_name='codigos',
    )
    dias = models.PositiveIntegerField(null=True, blank=True)
    llaves = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"Key {self.key}"

    def is_valid(self):
        now = timezone.now()
        if self.status != 'active':
            return False
        # Sin vencimiento (p. ej. cupo de una suscripción sin fecha de fin).
        if self.valid_until is None:
            return self.valid_from is None or self.valid_from <= now
        return self.valid_from <= now <= self.valid_until

    def save(self, *args, **kwargs):
        if not self.key:
            self.key = uuid.uuid4().hex[:12].upper()
        super().save(*args, **kwargs)

class EstudianteCurso(models.Model):
    estudiante_id = models.ForeignKey(Usuario, on_delete=models.CASCADE)
    curso_id = models.ForeignKey(Curso, on_delete=models.CASCADE)
    access_key_id = models.ForeignKey(AccessKey, on_delete=models.CASCADE)

    class Meta:
        # Un estudiante no puede tener el mismo curso 2 veces vía llaves distintas.
        constraints = [
            models.UniqueConstraint(
                fields=['estudiante_id', 'curso_id'],
                name='unique_estudiantecurso_estudiante_curso',
            ),
        ]
        ordering = ['id']

### 🙋 Solicitud de acceso (estudiante → escuela) ###
class SolicitudAcceso(models.Model):
    """Solicitud in-app de un estudiante para obtener acceso a un curso a
    través de una escuela (identificada por su `codigo`).

    Reemplaza el canal externo previo (el estudiante pedía por fuera y el
    director activaba el curso a mano). Al aprobarla, el director enrola al
    estudiante — vincula la escuela si hacía falta y consume 1 llave/cupo,
    igual que `activar_curso`.
    """
    ESTADO_CHOICES = [
        ('pendiente', 'Pendiente'),
        ('aprobada', 'Aprobada'),
        ('rechazada', 'Rechazada'),
        ('cancelada', 'Cancelada'),
    ]
    estudiante = models.ForeignKey(Usuario, on_delete=models.CASCADE, related_name='solicitudes_acceso')
    escuela = models.ForeignKey(Escuela, on_delete=models.CASCADE, related_name='solicitudes_acceso')
    curso = models.ForeignKey(Curso, on_delete=models.CASCADE)
    estado = models.CharField(max_length=12, choices=ESTADO_CHOICES, default='pendiente')
    mensaje = models.TextField(blank=True, default='')        # nota opcional del estudiante
    motivo_rechazo = models.TextField(blank=True, default='')  # nota opcional del director
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resuelta_por = models.ForeignKey(
        Usuario, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            # A lo sumo una solicitud PENDIENTE por (estudiante, curso): evita
            # spam y solicitudes duplicadas. Resueltas no cuentan (permite
            # re-pedir tras un rechazo o cuando el acceso vence).
            models.UniqueConstraint(
                fields=['estudiante', 'curso'],
                condition=models.Q(estado='pendiente'),
                name='unique_solicitud_pendiente_por_curso',
            ),
        ]
        indexes = [models.Index(fields=['escuela', 'estado', '-created_at'])]

    def __str__(self):
        return f"Solicitud {self.id} [{self.estado}] {self.estudiante_id}→{self.curso_id}"


class TransbankTransaction(models.Model):
    sale = models.OneToOneField(Venta, on_delete=models.CASCADE, related_name="transbank_transaction")
    transaction_date = models.DateTimeField(null=True)
    payment_type_code = models.CharField(max_length=200, null=True, blank=True)
    # Token de Transbank. `unique=True` es la barrera final contra doble
    # confirmación (además del chequeo exists() en la vista): dos commits del
    # mismo token no pueden crear dos ventas. NULL permitido (varios nulos OK).
    token = models.CharField(max_length=255, null=True, blank=True, unique=True)  # Token generado por Transbank
    buy_order = models.CharField(max_length=255, null=True, blank=True)  # Orden de compra asociada
    status = models.CharField(max_length=50, null=True, blank=True)  # Estado de la transacción (ej. "AUTHORIZED", "FAILED")
    amount = models.FloatField(null=True)  # Monto pagado

    def __str__(self):
        return f'Transacción {self.id} - Venta {self.sale.id}'


class IntentoPago(models.Model):
    """Un intento de pago (pay_init → pay_confirm), pagado o no.

    Separado de `Venta` (que solo guarda lo cobrado) para medir la conversión
    del pago sin ensuciar el historial de compras de directores y estudiantes.
    Un intento `iniciado` que nunca se confirmó se considera abandonado al
    consultarlo (ver `sales.services.estado_intento`); no hay tarea programada.
    """
    ESTADOS = [
        ('iniciado', 'Iniciado'),
        ('autorizado', 'Autorizado'),
        ('rechazado', 'Rechazado'),
    ]

    buy_order = models.CharField(max_length=255, db_index=True)
    token = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    usuario = models.ForeignKey(Usuario, on_delete=models.SET_NULL, null=True, blank=True)
    escuela = models.ForeignKey(Escuela, on_delete=models.SET_NULL, null=True, blank=True)
    item_type = models.CharField(max_length=10, choices=ITEM_TYPES)
    producto = models.ForeignKey(Producto, on_delete=models.SET_NULL, null=True, blank=True)
    curso = models.ForeignKey(Curso, on_delete=models.SET_NULL, null=True, blank=True)
    dias = models.PositiveIntegerField(null=True, blank=True)
    monto = models.PositiveIntegerField()
    pay_system = models.CharField(max_length=20)
    estado = models.CharField(max_length=12, choices=ESTADOS, default='iniciado', db_index=True)
    codigo_respuesta = models.CharField(max_length=50, null=True, blank=True)
    venta = models.ForeignKey(Venta, on_delete=models.SET_NULL, null=True, blank=True, related_name='intentos')
    creado_en = models.DateTimeField(auto_now_add=True, db_index=True)
    resuelto_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-creado_en']

    def __str__(self):
        return f"Intento {self.id} [{self.estado}] {self.buy_order}"


class CambioPrecio(models.Model):
    """Historial de precios de `schools.PlanCurso` y `sales.Producto`.

    Se registra por señales (`sales/signals.py`) cuando cambia un campo de
    precio; la migración 0027 guardó el estado inicial de cada ítem. `nombre`
    es una foto del nombre del ítem al momento del cambio.
    """
    TIPOS = [('plan_curso', 'Plan de curso'), ('producto', 'Producto')]

    tipo = models.CharField(max_length=12, choices=TIPOS)
    objeto_id = models.PositiveIntegerField()
    nombre = models.CharField(max_length=200)
    curso = models.ForeignKey(Curso, on_delete=models.SET_NULL, null=True, blank=True)
    dias = models.PositiveIntegerField(null=True, blank=True)
    precio_anterior = models.PositiveIntegerField(null=True, blank=True)
    precio_nuevo = models.PositiveIntegerField(null=True, blank=True)
    referencia_anterior = models.PositiveIntegerField(null=True, blank=True)
    referencia_nueva = models.PositiveIntegerField(null=True, blank=True)
    descuento_anterior = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    descuento_nuevo = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    activo = models.BooleanField(default=True)
    usuario = models.ForeignKey(Usuario, on_delete=models.SET_NULL, null=True, blank=True)
    fecha = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-fecha']
        indexes = [models.Index(fields=['tipo', 'objeto_id', 'fecha'])]

    def __str__(self):
        return f"{self.nombre}: {self.precio_anterior} → {self.precio_nuevo}"

class MovimientoLlaves(models.Model):
    """Uso de llaves del saldo de una escuela (consumo para el análisis comercial).

    `cantidad` negativa = llaves gastadas (activar/extender alumnos, generar
    códigos); positiva = devueltas (anular un código). Las compras viven en
    `Venta` y los ajustes manuales del admin no se registran aquí.
    """
    MOTIVOS = [
        ('activacion', 'Activación'),
        ('extension', 'Extensión'),
        ('codigos', 'Códigos'),
        ('anulacion', 'Anulación de código'),
    ]

    escuela = models.ForeignKey(Escuela, on_delete=models.CASCADE, related_name='movimientos_llaves')
    cantidad = models.IntegerField()
    motivo = models.CharField(max_length=12, choices=MOTIVOS)
    fecha = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-fecha']

    def __str__(self):
        return f"{self.escuela_id}: {self.cantidad:+d} ({self.motivo})"
