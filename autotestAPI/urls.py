from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from.swagger import schema_view
from .health import healthz


urlpatterns = [
    # Health check (PaaS / balanceador)
    path('healthz/', healthz, name='healthz'),

    # Swagger
    path('api/v1/swagger<format>/', schema_view.without_ui(cache_timeout=0), name='schema-json'),
    path('api/v1/swagger/', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui'),
    path('api/v1/redoc/', schema_view.with_ui('redoc', cache_timeout=0), name='schema-redoc'),

    # Django Admin
    path('admin/', admin.site.urls),

    # Modules
    path('api/v1/accounts/', include('accounts.urls')),
    path('api/v1/sales/', include('sales.urls')),
    path('api/v1/schools/', include('schools.urls')),
]
