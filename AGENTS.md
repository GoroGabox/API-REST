# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Dominio

AutoTest es un SaaS de contenido teórico para aprender a conducir vehículos (cursos, lecciones video/audio, ejercicios, pruebas/exámenes, certificados). Tres roles de usuario:

- **admin** — empleado interno de AutoTest. Gestiona catálogo (cursos, lecciones, ejercicios, escuelas, productos). Sin flag dedicado en el modelo; corresponde a `is_staff` / `is_superuser`.
- **director** (`is_director=True`) — cliente empresa. Compra `AccessKey`s y suscripciones (`basic_access` / `professional_access`) **sobre su `Escuela`** para distribuirlas a sus trabajadores.
- **estudiante** (`is_estudiante=True`) — usuario final. Adquiere acceso vía (a) compra directa B2C (Webpay) o (b) un director le otorga una `AccessKey` desde su escuela.

Esta distinción es la que justifica la lógica condicional `if user.is_director:` en los flujos de pago — director afecta a la `Escuela`, estudiante afecta a `EstudianteCurso`.

## Stack

Django 5.2 + DRF 3.16 + SimpleJWT + drf-yasg (Swagger). SQLite locally (`db.sqlite3`), Postgres in deployed envs via `DATABASE_URL` (note: `settings.py` currently hardcodes sqlite — Postgres usage is via `.env` only, no `dj-database-url` wiring). Payment gateway: `transbank-sdk` (Webpay Plus). Virtualenv lives in-repo at `env/` (Windows layout: `env/Scripts/`).

## Commands

All commands assume the bundled venv. Project root path contains a space — quote it.

```powershell
# Activate venv (Windows)
& "env/Scripts/Activate.ps1"

# Run dev server (default 127.0.0.1:8000)
python manage.py runserver

# Migrations
python manage.py makemigrations
python manage.py migrate

# Single app migration
python manage.py makemigrations accounts

# Shell / superuser
python manage.py createsuperuser
python manage.py shell

# Tests (Django test runner — each app has tests.py)
python manage.py test
python manage.py test accounts
python manage.py test accounts.tests.SomeTestCase.test_method

# Freeze deps (no requirements.txt currently checked in)
env/Scripts/pip.exe freeze > requirements.txt
```

Swagger UI: `http://127.0.0.1:8000/api/v1/swagger/` · ReDoc: `/api/v1/redoc/` · Admin: `/admin/`.

## Environment

`.env` (loaded by `python-dotenv` in `settings.py`) must define: `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `DATABASE_URL` (currently unused by settings — wire `dj-database-url` if you want it active), `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`. No `.env.example` exists — `ALLOWED_HOSTS` is hardcoded to `['*']` and `CORS_ALLOW_ALL_ORIGINS = True` regardless of env.

## Architecture

Three Django apps mounted under `/api/v1/` from `autotestAPI/urls.py`:

- **`accounts/`** — auth, users, student progress, test/exam taking. Custom user model `accounts.Usuario` (`AUTH_USER_MODEL`) keyed on `email`, with mutually-exclusive role flags `is_director` / `is_estudiante` that ALSO auto-add the user to `Directores` / `Estudiantes` `Group` via `UserManager.create_user`. Profile tables (`DirectorProfile`, `EstudianteProfile`) are 1:1 shells — most user data lives on `Usuario` itself. Holds `Prueba` / `PruebaEjercicio` (exam attempts), `EstudianteLeccion` (lesson progress), `Certificado`.
- **`schools/`** — content catalog. `Escuela` → has many `Usuario` (FK from `accounts.Usuario.escuela`). `Curso` → `Leccion` → `Ejercicio`; `Categoria` cross-cuts `Leccion` and `Ejercicio`. `Glosario` is standalone. Views in `schools/views.py` are thin (~33 lines) — almost pure `ModelViewSet` over the router in `schools/urls.py`.
- **`sales/`** — commerce. `Producto` (type: `llave` or `suscripcion`), `Venta`, `AccessKey` (UUID PK, auto-generates 12-char hex `key` on save, status `active/used/revoked`, time-bounded by `valid_from`/`valid_until`), `EstudianteCurso` (the join giving a student access to a course via one `AccessKey`), `TransbankTransaction` (1:1 with `Venta`).

### Cross-app coupling to know

- `accounts.Usuario.escuela` → `schools.Escuela` (FK, nullable).
- `accounts.EstudianteLeccion.{leccion,curso}` → `schools.{Leccion,Curso}`.
- `accounts.Certificado.curso` → `schools.Curso`; `Prueba.estudiante` / `PruebaEjercicio.ejercicio` reach across.
- `sales.EstudianteCurso` joins `accounts.Usuario` × `schools.Curso` × `sales.AccessKey` — this is the canonical "student has access to course" check; don't infer enrollment from `Usuario.escuela` alone.
- Because of these FKs, `schools` is the lowest-level app — avoid importing `accounts`/`sales` from it.

### Payment flow (sales)

Two payment surfaces coexist:

1. Legacy Webpay-only: `POST /api/v1/sales/webpay_init/` → `SaleInitiationViewSet` (Transbank `Transaction.create`) → user redirects to Transbank → `POST /api/v1/sales/webpay_confirm/` → `PaymentConfirmationView` calls `Transaction.commit(token)`, marks `Venta.payment_status`, persists `TransbankTransaction`, and on success calls `sales.services.asignar_llave_y_curso(estudiante, curso, dias)` (atomic: creates `AccessKey` + `EstudianteCurso`).
2. Unified (newer): `pay_init/` + `pay_confirm/` — same idea but pluggable payment system; `payment_status` on `Venta` is the source of truth.

`buy_order` convention is `order_{course_id}_{student_id}` and is parsed back via `sales.utils.extract_ids_from_buy_order`. Preserve that format if you touch initiation/confirmation.

Manual activation (school-admin path): `POST /api/v1/sales/activar_curso/` → `ActivarCursoView`, also funnels through `asignar_llave_y_curso`.

### Auth

JWT via `rest_framework_simplejwt` — `MyTokenObtainPairView` extends the default to embed extra claims (see `accounts.serializers.MyTokenObtainPairSerializer`). Access token TTL is **5 minutes**; refresh 30 days with rotation + blacklist (`SIMPLE_JWT` in `settings.py`). Logout (`POST /api/v1/accounts/logout/`) blacklists the supplied refresh token. Password reset uses Django's `default_token_generator` + base64-encoded uid; confirmation hits `/api/v1/accounts/new_password/<uidb64>/<token>/`. Account activation has its own token flow under `send-activation-email/` and `activate/<token>/`.

**DRF default auth = JWT only, no `DEFAULT_PERMISSION_CLASSES` is set** → every endpoint is open unless a view sets `permission_classes` explicitly. Most viewsets currently don't. Treat this as a known gap, not as intentional.

### URL convention

Spanish resource names in URLs (`pruebas`, `perfil-estudiante`, `ventas`, `cursos`) — keep that style when adding routes. Routers in each app's `urls.py`; the project URLconf only mounts apps under `/api/v1/{accounts,sales,schools}/`.

## Conventions

- Models, fields, and serializers use Spanish identifiers (`Usuario`, `nombre`, `apellido`, `escuela`, `pregunta`, `respuesta`). Match that — don't introduce English names mid-domain.
- `__str__` methods on `Prueba` / `PruebaEjercicio` concatenate an int PK with `+` (`'#'+self.id+...`) — that's a latent bug (`TypeError`); don't copy the pattern. Cast with `str()` or use f-strings if you touch them.
- View files are large (`accounts/views.py` 541 LOC, `sales/views.py` 460 LOC) and mix `APIView`, `ViewSet`, and generics — when extending, follow the existing pattern in the same file rather than refactoring.
- Business logic that crosses models lives in `sales/services.py` (currently only `asignar_llave_y_curso`). Prefer adding to `services.py` over inflating views.
