from pathlib import Path
from datetime import timedelta
from dotenv import load_dotenv
import os

load_dotenv()

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.getenv('SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is not set. Define it in .env before running.")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = bool(os.getenv('DEBUG', 'False') == 'True')

ALLOWED_HOSTS = [h.strip() for h in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h.strip()]


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles', # required for serving swagger ui's css/js files
    'drf_yasg',
    'django_filters',
    'corsheaders',
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'accounts',
    'schools',
    'sales',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',  # sirve estáticos en prod (tras SecurityMiddleware)
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'autotestAPI.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'autotestAPI.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.0/ref/settings/#databases

# Database — usa DATABASE_URL si está disponible, sino cae a sqlite local.
# Requiere `pip install dj-database-url` para parsear Postgres URLs.
_database_url = os.getenv('DATABASE_URL')
if _database_url:
    try:
        import dj_database_url
        DATABASES = {
            'default': dj_database_url.parse(_database_url, conn_max_age=600, ssl_require=not DEBUG),
        }
    except ImportError:
        raise RuntimeError(
            "DATABASE_URL is set but dj-database-url is not installed. "
            "Run: pip install dj-database-url"
        )
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }


# Password validation
# https://docs.djangoproject.com/en/5.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
# https://docs.djangoproject.com/en/5.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.0/howto/static-files/

# Configuración de archivos estáticos
STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

# WhiteNoise: comprime y cachea estáticos. Sin manifest para evitar fallos
# si drf-yasg/swagger referencia un asset ausente al hacer collectstatic.
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage',
    },
}

# Configuración de archivos multimedia (imágenes, archivos subidos, etc.)
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')
MEDIA_URL = '/media/'

# Default primary key field type
# https://docs.djangoproject.com/en/5.0/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTH_USER_MODEL = 'accounts.Usuario'

# CORS — orígenes permitidos vía .env (coma-separados). Sin fallback a "*".
CORS_ALLOWED_ORIGINS = [o.strip() for o in os.getenv('CORS_ALLOWED_ORIGINS', 'http://localhost:3000').split(',') if o.strip()]
CORS_ALLOW_ALL_ORIGINS = bool(os.getenv('CORS_ALLOW_ALL_ORIGINS', 'False') == 'True')

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=15),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    'BLACKLIST_AFTER_ROTATION': True,
}

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 25,
    'DEFAULT_FILTER_BACKENDS': (
        'django_filters.rest_framework.DjangoFilterBackend',
    ),
    # Rate limiting base (por IP para anónimos, por usuario para autenticados).
    # Los endpoints sensibles usan scopes más estrictos (ver accounts/throttles.py).
    'DEFAULT_THROTTLE_CLASSES': (
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ),
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100/min',          # navegación pública por IP
        'user': '1000/min',         # usuario autenticado
        'login': '10/min',          # intentos de login por IP
        'password_reset': '5/hour', # solicitudes de reset por IP
        'register': '10/hour',      # registros / reenvío de activación por IP
        'twofa': '10/min',          # verificación de código TOTP por usuario
    },
}

# ============================================================================
# Generador de cursos con LLM (Anthropic)
# Sin ANTHROPIC_API_KEY, el generador cae al pipeline extractivo determinista.
# El temario siempre usa el modelo "final" (barato: 1 llamada, alto impacto);
# las lecciones usan el modelo "draft" en modo draft y el "final" en modo final.
# ============================================================================
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
COURSE_LLM_ENABLED = os.environ.get('COURSE_LLM_ENABLED', '1') == '1' and bool(ANTHROPIC_API_KEY)
COURSE_LLM_MODEL = os.environ.get('COURSE_LLM_MODEL', 'claude-sonnet-5')
COURSE_LLM_MODEL_DRAFT = os.environ.get('COURSE_LLM_MODEL_DRAFT', 'claude-haiku-4-5-20251001')

# ============================================================================
# Email — configuración por env (provider-agnóstico).
# Default: Gmail SMTP (dev / MVP). Para un servicio transaccional en producción
# (Resend, SendGrid, Amazon SES) basta cambiar estas env vars, sin tocar código:
#   EMAIL_HOST=smtp.resend.com  EMAIL_PORT=587  EMAIL_USE_TLS=True
#   EMAIL_HOST_USER=resend      EMAIL_HOST_PASSWORD=<api-key>
# Gmail limita ~500 correos/día y marca transaccionales como spam: no usar a escala.
# ============================================================================
EMAIL_BACKEND = os.environ.get('EMAIL_BACKEND', 'django.core.mail.backends.smtp.EmailBackend')
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'True') == 'True'
EMAIL_USE_SSL = os.environ.get('EMAIL_USE_SSL', 'False') == 'True'
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')

# Remitente por defecto de todo correo transaccional. DEBE coincidir con la
# cuenta SMTP autenticada (EMAIL_HOST_USER): Gmail rechaza o marca como spam un
# `From` distinto al de la cuenta que envía. Se permite un display name.
# Configurable por env (DEFAULT_FROM_EMAIL) para producción.
DEFAULT_FROM_EMAIL = os.environ.get(
    'DEFAULT_FROM_EMAIL',
    f'AutoTest <{EMAIL_HOST_USER}>' if EMAIL_HOST_USER else 'no-reply@autotest.cl',
)
# Correo del que salen los mensajes de error del servidor a ADMINS.
SERVER_EMAIL = EMAIL_HOST_USER or DEFAULT_FROM_EMAIL

# Destinatarios de los reportes de error 500 (django.utils.log.AdminEmailHandler).
# Formato env ADMINS: coma-separado, cada uno "Nombre <correo>" o solo "correo".
#   ADMINS=Gabriel <alfons.diaz97@gmail.com>,ops@autotest.cl
def _parse_admins(raw):
    admins = []
    for item in raw.split(','):
        item = item.strip()
        if not item:
            continue
        if '<' in item and item.endswith('>'):
            name, _, email = item[:-1].partition('<')
            admins.append((name.strip(), email.strip()))
        else:
            admins.append((item, item))
    return admins

ADMINS = _parse_admins(os.environ.get('ADMINS', ''))
MANAGERS = ADMINS

# ============================================================================
# Autenticación en dos pasos (TOTP)
# `TOTP_ISSUER` es el nombre que muestra la app autenticadora (Google/Microsoft
# Authenticator) junto al código. Sin dominio productivo aún, se usa un valor de
# prueba desde .env.
# ============================================================================
TOTP_ISSUER = os.environ.get('TOTP_ISSUER', 'AutoTest')