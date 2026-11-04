from django.urls import path

from . import views

urlpatterns = [
    path('licencias/', views.LicenciaListView.as_view(), name='offline-licencias'),
    path('licencias/renovar/', views.LicenciaRenovarView.as_view(), name='offline-licencias-renovar'),
    path('licencias/<str:lic_id>/', views.LicenciaDetalleView.as_view(), name='offline-licencia'),
    path('licencias/<str:lic_id>/manifest/', views.LicenciaManifestView.as_view(), name='offline-licencia-manifest'),
    path('dispositivos/', views.DispositivoListView.as_view(), name='offline-dispositivos'),
    path('dispositivos/<str:device_id>/', views.DispositivoDetalleView.as_view(), name='offline-dispositivo'),
    path('llave-publica/', views.LlavePublicaView.as_view(), name='offline-llave-publica'),
]
