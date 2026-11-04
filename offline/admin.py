from django.contrib import admin

from .models import DispositivoOffline, LicenciaOffline


@admin.register(DispositivoOffline)
class DispositivoOfflineAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'nombre', 'plataforma', 'device_id', 'ultimo_uso')
    search_fields = ('usuario__email', 'nombre', 'device_id')


@admin.register(LicenciaOffline)
class LicenciaOfflineAdmin(admin.ModelAdmin):
    list_display = ('id', 'dispositivo', 'curso', 'renovada_en', 'offline_hasta', 'revocada')
    list_filter = ('revocada', 'curso')
    search_fields = ('dispositivo__usuario__email',)
    exclude = ('sal',)
