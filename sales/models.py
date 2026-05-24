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
    cant_professional_key = models.IntegerField(default=0, blank=True, null=True)
    basic_access = models.BooleanField(default=False)
    professional_access = models.BooleanField(default=False)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return self.nombre

### 🛒 Venta ###
class Venta(models.Model):
    id = models.AutoField(primary_key=True, auto_created=True)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)
    escuela = models.ForeignKey(Escuela, on_delete=models.CASCADE, null=True)
    usuario = models.ForeignKey(Usuario, on_delete=models.CASCADE, null=True)
    fecha_venta = models.DateTimeField(auto_now_add=True)
    monto_pagado = models.DecimalField(max_digits=10, decimal_places=2)
    pay_system = models.CharField(max_length=20)
    payment_status = models.CharField(max_length=20, default='pending')

    def __str__(self):
        return f"Venta {self.id} - {self.producto.nombre} ({self.monto_pagado} {self.producto.currency})"

### 🔑 Llaves de Acceso ###
class AccessKey(models.Model):
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('used', 'Used'),
        ('revoked', 'Revoked'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=64, unique=True, blank=True, null=True)
    valid_from = models.DateTimeField(auto_now_add=True, null=True)
    valid_until = models.DateTimeField(null=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='active')

    def __str__(self):
        return f"Key {self.key}"
    
    def is_valid(self):
        now = timezone.now()
        return self.status == 'active' and self.valid_from <= now <= self.valid_until
    
    def save(self, *args, **kwargs):
        if not self.key:
            self.key = uuid.uuid4().hex[:12].upper()
        super().save(*args, **kwargs)

class EstudianteCurso(models.Model):
    estudiante_id = models.ForeignKey(Usuario, on_delete=models.CASCADE)
    curso_id = models.ForeignKey(Curso, on_delete=models.CASCADE)
    access_key_id = models.ForeignKey(AccessKey, on_delete=models.CASCADE)

class TransbankTransaction(models.Model):
    sale = models.OneToOneField(Venta, on_delete=models.CASCADE, related_name="transbank_transaction")
    transaction_date = models.DateTimeField(null=True)
    payment_type_code = models.CharField(max_length=200, null=True, blank=True)
    token = models.CharField(max_length=255, null=True, blank=True)  # Token generado por Transbank
    buy_order = models.CharField(max_length=255, null=True, blank=True)  # Orden de compra asociada
    status = models.CharField(max_length=50, null=True, blank=True)  # Estado de la transacción (ej. "AUTHORIZED", "FAILED")
    amount = models.FloatField(null=True)  # Monto pagado

    def __str__(self):
        return f'Transacción {self.id} - Venta {self.sale.id}'    