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

    class Meta:
        ordering = ['id']

    def __str__(self):
        return self.nombre

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

    def __str__(self):
        return f"Key {self.key}"

    def is_valid(self):
        now = timezone.now()
        if self.status != 'active':
            return False
        # Seat: sin fecha de expiración.
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