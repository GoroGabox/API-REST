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

### Modelo del generador (el flujo actual, en orden)

1. **Estructura desde el LIBRO COMPLETO** (`manifest_from_content.build_manifest_from_content_llm`): el temario **ya NO dicta la estructura**. La estructura se infiere del contenido; los temas se piden como **strings** (JSON simple/robusto). `manifest_llm`/`manifest_builder` solo se usan para parsear el temario como checklist. Se filtran temas "meta" que el LLM cuela (`Quiz Unidad N`, `Evaluación del módulo N` → `_is_meta_tema`).
2. **Auto-dimensionado** (`course_planning.resolve_max_lecciones`): ≈1 lección por segmento, piso 8 / techo 100. Sin `--max-lecciones` se auto-dimensiona; con él, es techo. Avisa si el libro excede el tope (`truncation_notes`).
3. **Orientación por licencia** (`licenses.orientation_for(codigo, override)`): el mismo libro sirve a A1–A5; el prompt de estructura y de redacción **enfoca la licencia objetivo** (deriva del `codigo`, override con `--orientacion`).
4. **Procedencia (mapeo tema→segmentos) en llamada aparte** (`llm_mapper.map_topics_llm`): el LLM devuelve, por índice, los `segment_id` fuente de cada tema — JSON plano, con **reintentos + parser tolerante por regex** (embeber esto en el JSON de estructura lo rompía). `map_topics.map_topics_to_segments(provenance=…)` usa esos segmentos como fuente primaria; **fallback lexical/tfidf** por-tema cuando no hay procedencia válida. `coverage_alert` marca temas con fuente débil.
5. **Redacción anclada a la fuente** (`llm_lesson_writer`, RAG) con detección de truncamiento. Si un tema tiene **fuente débil** (sin segmentos o solo matches bajo umbral) la lección **se degrada al extractivo neutral** (no alucina) y se marca `fuente_debil`. Toda la construcción va en try/except: una lección nunca tumba el curso.
6. **Ancla de fuente a nivel de párrafo**: `segment_book` guarda `blocks` (texto + **página exacta**) por segmento; `lesson_generator._sources_for_segments` elige los párrafos más relevantes → `fuentes` con página precisa + extracto real.
7. **Categoría canónica** (`taxonomy`): 14 categorías compartidas por lecciones y banco de exámenes; `resolve()` deduplica variantes → `General`.
8. **Temario como checklist** (`course_planning.validate_topics_present`): valida que los temas del anexo estén representados, matcheando contra los **nombres generados Y el contenido fuente** del curso (corpus), para no dar falsos negativos con umbrellas ("Primeros auxilios" cubierto por RCP/Hemorragias).
9. **Auditoría de fidelidad** (`faithfulness`, **REPORTE, no bloquea**):
   - **Guard de cifras** (`_flag_figures`): marca números del contenido que **no aparecen en TODO el libro** (posible dato inventado).
   - **Anclaje lexical** (`anchoring_score`): por-lección, marca lecciones poco conectadas con su fuente (`< anchor_min`, def. 0.28).
   - **Juez LLM opcional** (`judge_lessons_llm`, con `--judge`): puntúa fidelidad 0–1 por lección y separa **críticas** (`score < judge_min`, def. 0.7) de **reparos menores** (fiel pero con matices no respaldados).

Módulos clave: `services/course_generator.py` (orquestador del stream, eventos NDJSON `step`/`warn`/`lesson`/`done`/`error`), `services/course_planning.py`, `processors/{manifest_from_content,llm_mapper,map_topics,llm_lesson_writer,segment_book,faithfulness,ejercicio_classifier}.py`, `taxonomy.py`, `licenses.py`.

### Modelos LLM

Configurables por `.env`: `COURSE_LLM_MODEL` (principal, def. `claude-sonnet-5`) y `COURSE_LLM_MODEL_DRAFT` (def. `claude-haiku-4-5-*`). **La estructura y el mapeo usan SIEMPRE el modelo principal** (Sonnet); `--modo` solo cambia el modelo de **redacción de lecciones**: `draft`=Haiku (barato), `final`=Sonnet. El juez sigue al modelo de lecciones. Para verificar estructura/mapeo/temario, `draft` basta (misma calidad ahí); reservar `final` para el pase de redacción final.

### Commands (en `schools/management/commands/`)

Flujo vivo — cursos (libro → JSON local → BD):
```powershell
# 1. Genera el curso desde el CONTENIDO (local, con ANTHROPIC_API_KEY). El temario es opcional
#    (checklist). Flags: --orientacion (deriva del código si se omite), --max-lecciones (auto si se omite),
#    --judge (juez LLM, costo extra), --judge-min 0.7, --anchor-min 0.28, --modo draft|final.
python manage.py generate_course --contenido "Libro.pdf" --temario "Temario A4.pdf" `
  --nombre "Curso Profesional Clase A4" --codigo A4 --costo 49990 --out out/a4.json --is-profesional --modo final --judge
# 2. Importa a la BD (upsert por código; --dry-run, --prune destructivo)
python manage.py import_course --file out/a4.json
# Auditar un JSON ya generado: triage de cifras / --contenido <pdf> (anclaje real) / --llm (juez) / --judge-min / --anchor-min / --limit
python manage.py audit_course_json out/a4.json --contenido "Libro.pdf" --llm
```

Flujo vivo — banco de ejercicios (PDF → JSON → BD):
```powershell
python manage.py build_ejercicios_json --pdf "Cuestionario Clase B.pdf" --out out/ejercicios_b.json
python manage.py import_ejercicios --file out/ejercicios_b.json   # idempotente por texto de pregunta
```

Flujo vivo — audio de lecciones (JSON del curso → guion → TTS → storage). `generate_media` no toca la BD; guarda el JSON **tras cada lección** (re-correr retoma donde quedó; no rehace guiones ni audios existentes salvo `--rehacer-guion` / `--overwrite`):
```powershell
# Fase 1 — guiones (Haiku, barato, sin TTS): `transcripcion` + `audio_meta` por lección
#   (auditoría REPORTE: cifras_nuevas vs la lección, truncado con reintento a 8k tokens, ratio_largo/corto)
#   + resumen top-level `auditoria_audio`. Revisar/editar `transcripcion` a mano antes de pagar TTS.
python manage.py generate_media --file out/a4.json --out out/a4_audio.json --solo-guion
# Fase 2 — TTS solo sobre guiones existentes (--estricto salta los que tienen hallazgos)
python manage.py generate_media --file out/a4_audio.json --solo-audio --provider openai --storage s3
python manage.py import_course --file out/a4_audio.json
```
- Módulos: `content_pipeline/media/{narration,tts,storage}.py`. Sin `--solo-*` hace ambas fases en una pasada.
- **Providers TTS** (`--provider` / `TTS_PROVIDER`): `elevenlabs` (mejor voz; `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`) y `openai` (barato, para iterar; `OPENAI_API_KEY`, `OPENAI_TTS_MODEL`=gpt-4o-mini-tts, `OPENAI_TTS_VOICE`=coral). Reintentos con backoff ante 429/5xx.
- **Storage** (`--storage` / `AUDIO_STORAGE`): `local` (MEDIA_ROOT + `--media-dir`/`--base-url`, dev) o `s3` (S3/Cloudflare R2 vía boto3: `AUDIO_S3_BUCKET`, `AUDIO_S3_ENDPOINT_URL`, `AUDIO_S3_REGION`, `AUDIO_S3_ACCESS_KEY_ID`, `AUDIO_S3_SECRET_ACCESS_KEY`, `AUDIO_S3_PUBLIC_BASE_URL`, `AUDIO_S3_PREFIX`). Producción = `s3` (Railway tiene FS efímero).
- Volumen: un curso A4 ≈ 76 lecciones × ~7k chars ≈ **520k caracteres de TTS** — el comando imprime la estimación antes de sintetizar. Probar voz con `--limit 1`.

Legacy A2 (hardcodeados al Curso Profesional A2, **no** usan el flujo genérico ni taxonomía/orientación/auditoría; solo para ese curso): `extract_a2_book`, `build_a2_lessons`, `build_a2_pipeline`, `validate_a2_course`, `import_a2_course`.

Demo/seed: `seed_catalogo` — datos de ejemplo (dev).

### Notas operativas

- `generate_course` se corre en local (caro/IA) y el JSON se sube con `import_course` en el entorno desplegado (sin IA ni PDFs). Ver la memoria de seed para poblar Railway con `DATABASE_URL` pública.
- **Paralelismo**: `generate_course` NO toca la BD → se puede correr en paralelo (varios terminales, cada uno su `--out`; el límite es el rate-limit de Anthropic). `import_course` NO en paralelo (SQLite bloquea la BD; y comparten categorías canónicas) — importar en serie.
- **Iterar barato**: para bajar reparos, preferir ajuste de prompt/pipeline cuando el hallazgo es **sistémico** (se amortiza en todos los cursos) o parche manual del JSON cuando es **puntual**; NO regenerar el curso completo solo para perseguir reparos (caro y no determinista). Diagnosticar en `draft`, correr `final` una sola vez al cierre.
- El JSON generado incluye `auditoria` (cifras/anclaje/juez) cuando se corre con `--judge`. La herramienta de revisión humana ("Brújula del Libro", artifact fuera del repo) lee ese JSON y muestra por lección el ancla, el contenido y los hallazgos del juez.

## Conventions

- Models, fields, and serializers use Spanish identifiers (`Usuario`, `nombre`, `apellido`, `escuela`, `pregunta`, `respuesta`). Match that — don't introduce English names mid-domain.
- `__str__` methods on `Prueba` / `PruebaEjercicio` concatenate an int PK with `+` (`'#'+self.id+...`) — that's a latent bug (`TypeError`); don't copy the pattern. Cast with `str()` or use f-strings if you touch them.
- View files are large (`accounts/views.py` 541 LOC, `sales/views.py` 460 LOC) and mix `APIView`, `ViewSet`, and generics — when extending, follow the existing pattern in the same file rather than refactoring.
- Business logic that crosses models lives in `sales/services.py` (`asignar_llave_y_curso`, `registrar_compra_curso_individual`, `registrar_venta_unificada`, `precio_plan`, `precio_final_producto`, `tiene_acceso_a_curso`, `llaves_para_dias`, …). Prefer adding to `services.py` over inflating views.
