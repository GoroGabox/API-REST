from django.contrib import admin
from .models import Producto, Venta, TransbankTransaction, AccessKey, EstudianteCurso

# Register your models here.

admin.site.register(Producto)
admin.site.register(Venta)
admin.site.register(TransbankTransaction)
admin.site.register(AccessKey)
admin.site.register(EstudianteCurso)