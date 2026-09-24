# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Dominio

AutoTest es un SaaS de contenido teórico para aprender a conducir vehículos (cursos, lecciones video/audio, ejercicios, pruebas/exámenes, certificados). Tres roles de usuario:

- **admin** — empleado interno de AutoTest. Gestiona catálogo (cursos, lecciones, ejercicios, escuelas, productos). Sin flag dedicado en el modelo; corresponde a `is_staff` / `is_superuser`.
- **director** (`is_director=True`) — cliente empresa. Sobre su `Escuela` compra **llaves** (`basic_key`; un único tipo — cada llave habilita **7 días** de acceso, así que otorgar N días cuesta `ceil(N/7)` llaves) y/o una **suscripción** (`basic_access`, acceso ilimitado en tiempo acotado por cupos `basic_seats_max`/`basic_seats_used`) para distribuirlas a sus trabajadores. No existe un tier profesional (llaves ni cupos); `Curso.is_profesional` sobrevive solo como etiqueta y no afecta acceso ni consumo.
- **estudiante** (`is_estudiante=True`) — usuario final. Adquiere acceso vía (a) compra directa B2C (Webpay) — planes 7/14/35 días cuyo precio vive en `schools.PlanCurso` (no en `Curso`) — o (b) un director le otorga una `AccessKey` desde su escuela.

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
- **`schools/`** — content catalog. `Escuela` → has many `Usuario` (FK from `accounts.Usuario.escuela`). `Curso` → `Leccion` → `Ejercicio`; `Categoria` cross-cuts `Leccion` and `Ejercicio`. `Glosario` is standalone. **`PlanCurso`** (FK → `Curso`, `related_name="planes"`) holds B2C pricing per duration: one row per `(curso, dias)` (7/14/35) with `precio`, `precio_referencia`, `etiqueta`, `activo` — **fuente única de precio del curso; `Curso` ya NO tiene campo `costo`**. El plan de 7 días es el valor unitario (`precio_unitario`, expuesto por `CursoSerializer`). CRUD admin en `PlanCursoViewSet` (router `plan-cursos`).
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

**`buy_order` convention (estricto, sin fallbacks):** producto → `order_{producto_id}_{student_id}` (3 segmentos); compra individual de curso (`item_type='curso'`) → `order_{curso_id}_{student_id}_{dias}` (**4 segmentos, siempre lleva `dias`**). Parseado con `sales.utils.extract_ids_from_buy_order` (ids) y `extract_dias_from_buy_order` (días del curso). Preservar ese formato.

**Compra individual de curso (B2C, estudiante):** `pay_init`/`pay_confirm` con `item_type='curso'`. El precio autoritativo es `sales.services.precio_plan(curso, dias)` = `PlanCurso.precio` del plan activo; el monto pagado debe coincidir EXACTO (anti-tampering) y los días se derivan del `buy_order` (sin `dias` → 400). `sales.services.registrar_compra_curso_individual` crea la `AccessKey(origen='purchase')` + `EstudianteCurso` + `Venta(curso)` y maneja renovación de compra vencida. Crear/generar un curso exige `precio_unitario` > 0 (write-only en `schools.serializers.CursoSerializer` y en `CourseGenerateView`), que crea el `PlanCurso` de 7 días.

Manual activation (school-admin path): `POST /api/v1/sales/activar_curso/` → `ActivarCursoView`, also funnels through `asignar_llave_y_curso`.

### Pruebas, examen final y gamificación (accounts)

- `POST accounts/tests/generate/` crea una `Prueba` (`tipo` rapida/categoria/completa = 10/15/35 preguntas, `services.SIZES`; `modalidad` practica/evaluacion). El **Simulacro Oficial** del Gimnasio es `completa` + `practica` (banco general, 70%, sin certificado) — no confundir con el examen final. El **examen final** es `tipo='completa'` + `modalidad='evaluacion'` + `curso_id`: usa solo preguntas del curso, aprueba con 80% y al aprobar emite `Certificado` (signal → `services.emitir_certificado_si_corresponde`). `generate_free/` + `grade_free/` = práctica pública sin login (no persiste). Las preguntas nunca exponen la clave (`services.serializar_preguntas_publicas`).
- `services.submit_prueba` corrige (set exacto de keys; acepta `'a'`, `['a','b']` o `'a,b'`) y devuelve `{aprobado, score, total_correctas, total, detalles, xp_ganado, streak_actual, logros_nuevos}` (+ `certificado` en la vista).
- **Elegibilidad del examen final** (`services.elegibilidad_examen_final`, `GET me/courses/<id>/final-exam/`): `razon` ∈ `ok` | `curso_completado` | `plazo_vencido` | `lecciones_pendientes` (evaluadas en ese orden; exige **todas** las `Leccion` del curso con `EstudianteLeccion` — un curso sin lecciones no bloquea). Incluye `expira_en`, `ultimo_intento` (informativo), `lecciones_total`, `lecciones_completadas` y `total_preguntas` (nº real del examen, ≤35). **Sin espera entre intentos** — se puede reintentar de inmediato; `generate/` responde 403 + `razon` en los tres casos bloqueantes.
- **Gamificación** (`accounts/gamification.py`): solo **XP, racha y logros**. **No existen vidas (hearts)**: se eliminaron el modelo (`hearts`/`next_heart_regen_at`, migración `0020_remove_hearts`), el consumo/gate/regeneración, `FINAL_EXAM_RETRY_HOURS` y los campos de API (`hearts`, `max_hearts`, `next_heart_regen_at`, `corazones_restantes`, `retry_after_seconds`, `proximo_intento`). La choice de notificación `hearts_refilled` se conserva solo para leer registros históricos. No reintroducir vidas ni cooldown sin pedirlo; clientes (webapp y app Expo) dependen de este contrato.

### Auth

JWT via `rest_framework_simplejwt` — `MyTokenObtainPairView` extends the default to embed extra claims (see `accounts.serializers.MyTokenObtainPairSerializer`). Access token TTL is **5 minutes**; refresh 30 days with rotation + blacklist (`SIMPLE_JWT` in `settings.py`). Logout (`POST /api/v1/accounts/logout/`) blacklists the supplied refresh token. Password reset uses Django's `default_token_generator` + base64-encoded uid; confirmation hits `/api/v1/accounts/new_password/<uidb64>/<token>/`. Account activation has its own token flow under `send-activation-email/` and `activate/<token>/`.

**DRF default auth = JWT only, no `DEFAULT_PERMISSION_CLASSES` is set** → every endpoint is open unless a view sets `permission_classes` explicitly. Most viewsets currently don't. Treat this as a known gap, not as intentional.

### URL convention

Spanish resource names in URLs (`pruebas`, `perfil-estudiante`, `ventas`, `cursos`) — keep that style when adding routes. Routers in each app's `urls.py`; the project URLconf only mounts apps under `/api/v1/{accounts,sales,schools}/`.

## Content pipeline (`content_pipeline/`)

Librería (NO app Django, no tiene modelos ni `urls.py`) que genera cursos y ejercicios a partir de PDFs con IA (Anthropic). La usa el endpoint de streaming `schools.views.CourseGenerateView` (`POST /api/v1/schools/cursos/generar/`) y los management commands de abajo (que viven en `schools/management/commands/`).

Módulos clave:
- `services/course_generator.py` — orquestador del stream (eventos NDJSON `step`/`warn`/`lesson`/`done`/`error`).
- `services/course_planning.py` — dimensionar el curso al libro (`resolve_max_lecciones`, `truncation_notes`) + validar el temario como checklist (`extract_temario_topics`, `validate_topics_present`).
- `processors/manifest_from_content.py` — infiere la estructura del **libro completo** (fuente única de estructura; el temario ya NO la dicta). `manifest_llm.py`/`manifest_builder.py` solo parsean el temario para el checklist.
- `processors/llm_lesson_writer.py` — redacta las lecciones ancladas a la fuente (RAG) con detección de truncamiento.
- `processors/faithfulness.py` — auditoría de fidelidad (guard de cifras `unsupported_figures`, anclaje lexical `anchoring_score`, juez LLM `judge_lessons_llm`). REPORTE, no bloquea.
- `taxonomy.py` — **14 categorías canónicas** compartidas por lecciones y banco de exámenes; `resolve()` deduplica variantes → `General` si no encaja.
- `licenses.py` — orientación del curso por licencia (A1–A5): mismo libro sirve a varias clases, el prompt enfoca la licencia objetivo (`orientation_for(codigo, override)`).
- `processors/ejercicio_classifier.py` — clasifica preguntas del cuestionario en la misma taxonomía.

**Modelo del generador (importante):** el curso se arma del **libro completo** y se **auto-dimensiona** (≈1 lección por segmento, techo 100) para no truncar; el **temario es un checklist** que se valida al final (avisa temas faltantes); la **categoría** sale de la taxonomía cerrada; el curso se **orienta a la licencia** del código; la **fidelidad** se audita (cifras/anclaje/juez) sin bloquear; el **ancla de fuente** es el párrafo real con su página exacta (`segment_book.blocks` + `lesson_generator._sources_for_segments`).

### Commands (en `schools/management/commands/`)

Flujo vivo — cursos (libro → JSON local → BD):
```powershell
# 1. Genera el curso desde el CONTENIDO (local, con ANTHROPIC_API_KEY). El temario es opcional
#    (checklist). --orientacion se deriva del código si se omite. --judge = juez LLM (costo extra).
python manage.py generate_course --contenido "Libro.pdf" --temario "Temario A4.pdf" `
  --nombre "Curso Profesional Clase A4" --codigo A4 --costo 49990 --out out/a4.json --is-profesional --modo final
# 2. Importa a la BD (upsert por código; --dry-run, --prune destructivo)
python manage.py import_course --file out/a4.json
# Auditar un JSON ya generado: cifras (triage) / --contenido <pdf> (anclaje real) / --llm (juez)
python manage.py audit_course_json out/a4.json --contenido "Libro.pdf" --llm
```

Flujo vivo — banco de ejercicios (PDF → JSON → BD):
```powershell
python manage.py build_ejercicios_json --pdf "Cuestionario Clase B.pdf" --out out/ejercicios_b.json
python manage.py import_ejercicios --file out/ejercicios_b.json   # idempotente por texto de pregunta
```

Media: `generate_media` — genera el audio (TTS) de un curso ya generado y setea `url_audio`.

Legacy A2 (hardcodeados al Curso Profesional A2, **no** usan el flujo genérico ni taxonomía/orientación/auditoría; solo para ese curso): `extract_a2_book`, `build_a2_lessons`, `build_a2_pipeline`, `validate_a2_course`, `import_a2_course`.

Demo/seed: `seed_catalogo` — datos de ejemplo (dev).

`generate_course` se corre en local (caro/IA) y el JSON se sube con `import_course` en el entorno desplegado (sin IA ni PDFs). Ver la memoria de seed para poblar Railway con `DATABASE_URL` pública.

## Conventions

- Models, fields, and serializers use Spanish identifiers (`Usuario`, `nombre`, `apellido`, `escuela`, `pregunta`, `respuesta`). Match that — don't introduce English names mid-domain.
- `__str__` methods on `Prueba` / `PruebaEjercicio` concatenate an int PK with `+` (`'#'+self.id+...`) — that's a latent bug (`TypeError`); don't copy the pattern. Cast with `str()` or use f-strings if you touch them.
- View files are large (`accounts/views.py` 541 LOC, `sales/views.py` 460 LOC) and mix `APIView`, `ViewSet`, and generics — when extending, follow the existing pattern in the same file rather than refactoring.
- Business logic that crosses models lives in `sales/services.py` (`asignar_llave_y_curso`, `registrar_compra_curso_individual`, `registrar_venta_unificada`, `precio_plan`, `precio_final_producto`, `tiene_acceso_a_curso`, `llaves_para_dias`, …). Prefer adding to `services.py` over inflating views.
