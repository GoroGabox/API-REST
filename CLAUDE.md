# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Dominio

AutoTest es un SaaS de contenido teórico para aprender a conducir vehículos (cursos, lecciones video/audio, ejercicios, pruebas/exámenes, certificados). Tres roles de usuario:

- **admin** — empleado interno de AutoTest. Gestiona catálogo (cursos, lecciones, ejercicios, escuelas, productos). Sin flag dedicado en el modelo; corresponde a `is_staff` / `is_superuser`.
- **director** (`is_director=True`) — cliente empresa. Sobre su `Escuela` compra **llaves** (`basic_key`; un único tipo — cada llave habilita **7 días** de acceso, así que otorgar N días cuesta `ceil(N/7)` llaves) y/o una **suscripción** (`basic_access`, acotada por cupos `basic_seats_max`/`basic_seats_used` y por su vigencia `basic_access_until`, null = sin vencimiento; el `Producto` de suscripción define `cant_seats` y `duracion_dias`: con la suscripción vigente la compra suma cupos y extiende desde el fin, vencida abre un período nuevo con los cupos del producto; los cupos asignados vencen con ella y se extienden al renovar) para distribuirlas a sus trabajadores. No existe un tier profesional (llaves ni cupos); `Curso.is_profesional` sobrevive solo como etiqueta y no afecta acceso ni consumo.
- **estudiante** (`is_estudiante=True`) — usuario final. Adquiere acceso vía (a) compra directa B2C (Webpay) — planes 7/14/35 días cuyo precio vive en `schools.PlanCurso` (no en `Curso`) — o (b) un director le otorga una `AccessKey` desde su escuela.

Esta distinción es la que justifica la lógica condicional `if user.is_director:` en los flujos de pago — director afecta a la `Escuela`, estudiante afecta a `EstudianteCurso`.

## Stack

Django 5.2 + DRF 3.16 + SimpleJWT + drf-yasg (Swagger). Settings is a package `autotestAPI/settings/` (`base.py` + `development.py` / `production.py` / `test.py`), selected by `DJANGO_ENV` (default `development` if `DEBUG=True`, else `production`; `production` enables `SECURE_SSL_REDIRECT` and requires real `ALLOWED_HOSTS`). Database: `DATABASE_URL` via `dj-database-url` when set (Postgres on Railway), otherwise local SQLite `db.sqlite3`. Payment gateway: `transbank-sdk` (Webpay Plus). Virtualenv lives in-repo at `env/` (Windows layout: `env/Scripts/`). Dependencies in `requirements.txt`.

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

# Install / freeze deps
env/Scripts/pip.exe install -r requirements.txt
env/Scripts/pip.exe freeze > requirements.txt
```

Swagger UI: `http://127.0.0.1:8000/api/v1/swagger/` · ReDoc: `/api/v1/redoc/` · Admin: `/admin/`.

## Environment

`.env` (loaded by `python-dotenv` in `autotestAPI/settings/__init__.py`; template in `.env.example`) defines `SECRET_KEY` (required — startup fails without it), `DEBUG`, `ALLOWED_HOSTS` (comma-separated, default `localhost,127.0.0.1`), `CORS_ALLOWED_ORIGINS` (default `http://localhost:3000`), `CORS_ALLOW_ALL_ORIGINS` (default `False`; forced `False` in production), `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `FRONTEND_URL`, `TOTP_ISSUER`, `TBK_ENVIRONMENT`, optional `DATABASE_URL` and `DJANGO_ENV`. Course generation: `ANTHROPIC_API_KEY` (without it the pipeline falls back to extractive / the plan flow refuses to redact), `COURSE_LLM_MODEL`, `COURSE_LLM_MODEL_DRAFT`, `COURSE_LLM_ENABLED`; audio, figuras y PDFs: see the TTS/storage vars under *Content pipeline* (`AUDIO_STORAGE`, `AUDIO_S3_*`, `IMAGES_S3_PREFIX`, `DOCS_S3_PREFIX`).

## Architecture

Three Django apps mounted under `/api/v1/` from `autotestAPI/urls.py`:

- **`accounts/`** — auth, users, student progress, test/exam taking. Custom user model `accounts.Usuario` (`AUTH_USER_MODEL`) keyed on `email`, with mutually-exclusive role flags `is_director` / `is_estudiante` that ALSO auto-add the user to `Directores` / `Estudiantes` `Group` via `UserManager.create_user`. Profile tables (`DirectorProfile`, `EstudianteProfile`) are 1:1 shells — most user data lives on `Usuario` itself. Holds `Prueba` / `PruebaEjercicio` (exam attempts), `EstudianteLeccion` (lesson progress), `Certificado`.
- **`schools/`** — content catalog. `Escuela` → has many `Usuario` (FK from `accounts.Usuario.escuela`). `Curso` → `Leccion` → `Ejercicio`; `Categoria` cross-cuts `Leccion` and `Ejercicio`. `Glosario` is standalone. **`PlanCurso`** (FK → `Curso`, `related_name="planes"`) holds B2C pricing per duration: one row per `(curso, dias)` (7/14/35) with `precio`, `precio_referencia`, `etiqueta`, `activo` — **fuente única de precio del curso; `Curso` ya NO tiene campo `costo`**. El plan de 7 días es el valor unitario (`precio_unitario`, expuesto por `CursoSerializer`). CRUD admin en `PlanCursoViewSet` (router `plan-cursos`). **`LeccionRecurso`** (FK → `Leccion`, `related_name="recursos"`): todo lo que acompaña al texto de una lección — `tipo` imagen|audio|video|pdf, `rol` figura|narracion|principal|paginas_libro|descarga, `clave` (única por lección; para figuras = id del marcador `{{figura:<clave>}}` que el texto lleva donde va la figura; `audio`, `paginas`…), `url`, `orden`, `titulo`, `meta` JSON (pie, alt, pagina, ancho, alto, duracion_seg, mapeo, hash…). El texto sigue en `Leccion.contenido` (Markdown); `Leccion.tipo` no cambia (texto = lectura con recursos; quiz/drag/identify = evaluación). **Espejo de compatibilidad**: `url_audio`/`url_video`/`url_pdf` se rellenan desde los recursos (`schools/services.sync_media_mirror`, señales en `schools/signals.py`; nunca pisa una URL puesta a mano si no hay recurso de ese tipo) — quitar en una versión posterior. `LeccionDetalleSerializer` expone `recursos` + `imagenes` (derivado, forma previa). La relación inversa de la biblioteca (`Recurso.leccion`) es `recursos_biblioteca`. Migraciones `0025`-`0027` (crea la tabla, copia `LeccionImagen` y `url_*` reales, borra `LeccionImagen`).
- **`sales/`** — commerce. `Producto` (type: `llave` or `suscripcion`), `Venta`, `AccessKey` (UUID PK, auto-generates 12-char hex `key` on save, status `active/used/revoked` — el acceso exige `active`; el canje deja la llave `active` ligada a su inscripción y una llave ya ligada no se puede canjear de nuevo; time-bounded by `valid_from`/`valid_until`), `EstudianteCurso` (the join giving a student access to a course via one `AccessKey`), `TransbankTransaction` (1:1 with `Venta`).

### Cross-app coupling to know

- `accounts.Usuario.escuela` → `schools.Escuela` (FK, nullable).
- `accounts.EstudianteLeccion.{leccion,curso}` → `schools.{Leccion,Curso}`.
- `accounts.Certificado.curso` → `schools.Curso`; `Prueba.estudiante` / `PruebaEjercicio.ejercicio` reach across.
- `sales.EstudianteCurso` joins `accounts.Usuario` × `schools.Curso` × `sales.AccessKey` — this is the canonical "student has access to course" check; don't infer enrollment from `Usuario.escuela` alone.
- Because of these FKs, `schools` is the lowest-level app — avoid importing `accounts`/`sales` from it.

### Payment flow (sales)

Two payment surfaces coexist:

1. Legacy Webpay-only: `POST /api/v1/sales/webpay_init/` → `SaleInitiationViewSet` (Transbank `Transaction.create`) → user redirects to Transbank → `POST /api/v1/sales/webpay_confirm/` → `PaymentConfirmationView` calls `Transaction.commit(token)`, marks `Venta.payment_status`, persists `TransbankTransaction`, and on success calls `sales.services.registrar_venta_transbank` (products only).
2. Unified (newer): `pay_init/` + `pay_confirm/` — same idea but pluggable payment system; `payment_status` on `Venta` is the source of truth.

**`buy_order` convention (estricto, sin fallbacks):** producto → `order_{producto_id}_{student_id}` (3 segmentos); compra individual de curso (`item_type='curso'`) → `order_{curso_id}_{student_id}_{dias}` (**4 segmentos, siempre lleva `dias`**). Parseado con `sales.utils.extract_ids_from_buy_order` (ids) y `extract_dias_from_buy_order` (días del curso). Preservar ese formato.

**Compra individual de curso (B2C, estudiante):** `pay_init`/`pay_confirm` con `item_type='curso'`. El precio autoritativo es `sales.services.precio_plan(curso, dias)` = `PlanCurso.precio` del plan activo; el monto pagado debe coincidir EXACTO (anti-tampering) y los días se derivan del `buy_order` (sin `dias` → 400). `sales.services.registrar_compra_curso_individual` crea la `AccessKey(origen='purchase')` + `EstudianteCurso` + `Venta(curso)` y maneja renovación de compra vencida. Crear/generar un curso exige `precio_unitario` > 0 (write-only en `schools.serializers.CursoSerializer` y en `CourseGenerateView`), que crea el `PlanCurso` de 7 días.

**Códigos canjeables (director → estudiante):** `POST /sales/codigos/ {cantidad 1–100, dias múltiplo de 7}` convierte llaves del saldo en `AccessKey` con `escuela`, `dias` y `llaves` (cobra `cantidad × llaves_para_dias(dias)`; admin pasa `escuela` y genera sin descontar). Sin canjear, `valid_until` es el vencimiento del código (90 días). `canjear_access_key` con `dias` reinicia la vigencia desde el canje (`now + dias`) y vincula al estudiante a la escuela emisora si no tenía. `GET /sales/codigos/` lista estado (disponible/canjeado/vencido/anulado); `POST /sales/codigos/<uuid>/anular/` solo si no está canjeado y devuelve las llaves. Servicios: `generar_codigos`, `anular_codigo`.

Productos (llaves/suscripción) solo los compra un **director con escuela** (`pay_init`/`webpay_init` → 403 a otro rol; admin puede pagar por un director). En la confirmación el producto, el comprador y el monto se derivan del `buy_order` **que devuelve Transbank** (`_resolver_compra_producto`), nunca del `product_id`/`user_id` del cliente. Token duplicado (carrera) → respuesta idempotente, no 500.

Manual activation (school-admin path): `POST /api/v1/sales/activar_curso/` → `ActivarCursoView` → `sales.services.asignar_por_source` (cupo o llave; `days` entero > 0, destino estudiante, 409 si ya tiene acceso vigente; una inscripción revocada/vencida se reactiva con la llave nueva). Journeys e2e de alta de usuarios, compras y llaves: `sales/tests_e2e.py`.

### Pruebas, examen final y gamificación (accounts)

- `POST accounts/tests/generate/` crea una `Prueba` (`tipo` rapida/categoria/completa = 10/15/35 preguntas, `services.SIZES`; el 15 de la Temática es `schools.models.PREGUNTAS_SESION_TEMATICA`, que también define `disponible_tematica` en `CategoriaSerializer` — flag booleano, sin exponer conteos; `modalidad` practica/evaluacion). El **Simulacro Oficial** del Gimnasio es `completa` + `practica` (banco general, 70%, sin certificado) — no confundir con el examen final. El **examen final** es `tipo='completa'` + `modalidad='evaluacion'` + `curso_id`: usa solo preguntas del curso, aprueba con 80% y al aprobar emite `Certificado` (signal → `services.emitir_certificado_si_corresponde`). `generate_free/` + `grade_free/` = práctica pública sin login (no persiste). Las preguntas nunca exponen la clave (`services.serializar_preguntas_publicas`).
- **Nivel por tema** (`services.niveles_por_tema`, `GET me/temas/`, auth): por cada tema con `disponible_tematica`, usa las últimas `NIVEL_VENTANA`=20 respuestas corregidas del estudiante (pruebas entregadas); desde `NIVEL_MIN_RESPUESTAS`=5: <60% `reforzar`, <80% `progreso`, si no `dominado` (menos de 5 → `null`). `recomendada` = tema con menos aciertos bajo 80% (`motivo: 'mas_dificil'`), si no el primero sin practicar (`'sin_practicar'`), si domina todo `null`. **Nunca expone % ni conteos** — solo `{temas: [{categoria_id, nivel}], recomendada}`.
- `services.submit_prueba` corrige (set exacto de keys; acepta `'a'`, `['a','b']` o `'a,b'`) y devuelve `{aprobado, score, total_correctas, total, detalles, xp_ganado, streak_actual, logros_nuevos}` (+ `certificado` en la vista).
- **Elegibilidad del examen final** (`services.elegibilidad_examen_final`, `GET me/courses/<id>/final-exam/`): `razon` ∈ `ok` | `curso_completado` | `plazo_vencido` | `lecciones_pendientes` (evaluadas en ese orden; exige **todas** las `Leccion` del curso con `EstudianteLeccion` — un curso sin lecciones no bloquea). Incluye `expira_en`, `ultimo_intento` (informativo), `lecciones_total`, `lecciones_completadas` y `total_preguntas` (nº real del examen, ≤35). **Sin espera entre intentos** — se puede reintentar de inmediato; `generate/` responde 403 + `razon` en los tres casos bloqueantes.
- **Gamificación** (`accounts/gamification.py`): solo **XP, racha y logros**. **No existen vidas (hearts)**: se eliminaron el modelo (`hearts`/`next_heart_regen_at`, migración `0020_remove_hearts`), el consumo/gate/regeneración, `FINAL_EXAM_RETRY_HOURS` y los campos de API (`hearts`, `max_hearts`, `next_heart_regen_at`, `corazones_restantes`, `retry_after_seconds`, `proximo_intento`). La choice de notificación `hearts_refilled` se conserva solo para leer registros históricos. No reintroducir vidas ni cooldown sin pedirlo; clientes (webapp y app Expo) dependen de este contrato.

### Auth

JWT via `rest_framework_simplejwt` — `MyTokenObtainPairView` extends the default to embed extra claims (see `accounts.serializers.MyTokenObtainPairSerializer`). Access token TTL is **15 minutes**; refresh 30 days with rotation + blacklist (`SIMPLE_JWT` in `settings/base.py`). Logout (`POST /api/v1/accounts/logout/`) blacklists the supplied refresh token. Password reset uses Django's `default_token_generator` + base64-encoded uid; confirmation hits `/api/v1/accounts/new_password/<uidb64>/<token>/`. Account activation has its own token flow under `send-activation-email/` and `activate/<token>/`.

**DRF defaults: JWT authentication + `DEFAULT_PERMISSION_CLASSES = IsAuthenticated`** → every endpoint requires login unless the view overrides `permission_classes` (public endpoints use `AllowAny`, e.g. login/registro, práctica libre; back-office uses `accounts.permissions.IsAdmin` / `IsDirector` / …). When adding a view, set its permissions explicitly.

### URL convention

Spanish resource names in URLs (`pruebas`, `perfil-estudiante`, `ventas`, `cursos`) — keep that style when adding routes. Routers in each app's `urls.py`; the project URLconf only mounts apps under `/api/v1/{accounts,sales,schools}/`.

## Content pipeline (`content_pipeline/`)

Librería (NO app Django, no tiene modelos ni `urls.py`) que genera cursos y ejercicios a partir de PDFs con IA (Anthropic). La usan los endpoints web (solo admin) y los management commands de abajo (que viven en `schools/management/commands/`).

**Endpoints web (flujo PLAN → GENERAR, consumidos por `CourseGenerator.js` del webapp):**
- `POST /api/v1/schools/courses/plan/` (`CoursePlanView`, multipart: `contenido` PDF/DOCX, `nombre`, `codigo`, `largo`, `ia`) → `{plan, resumen, ia, estructura}`. Con `ANTHROPIC_API_KEY` los cortes de lección los decide la IA (`estructura` = costo; `null` = cortes por palabras). No redacta ni persiste.
- `POST /api/v1/schools/courses/generate/` (`CourseGenerateView`): con JSON `{plan, nombre, codigo, precio_unitario, is_profesional?, juez?=true, modo?=draft|final, guardar?=true, fuente_nombre?, orientacion?}` redacta desde el plan aprobado (`services/plan_generation.generate_course_from_plan_stream`: lecciones, quiz con evidencia, cifras, juez por lección con progreso, juez de quiz, validación) y transmite NDJSON (`step/warn/lesson/done/error` + **`bloqueado`**). Si `import_blockers` encuentra bloqueos **no importa**: `bloqueado` trae `bloqueos` y `curso_json` para descargarlo (Brújula) o importarlo explícitamente. `done` también trae `curso_json` (descargable). Con **`guardar: false`** ("solo generar") no toca la BD: si no hay bloqueos emite **`generado`** con `curso_json`, para auditarlo en la Brújula o importarlo en otro entorno (p. ej. producción con `import_course`). Multipart con `temario`+`contenido` sigue disparando el **flujo legacy** (compatibilidad).
- `POST /api/v1/schools/courses/import/` (`CourseImportView`, JSON `{curso, forzar?}`): mismo bloqueo que `import_course`; 409 con `bloqueos` sin `forzar`, 201 con `forzar`.

### Flujo RECOMENDADO: PLAN → GENERAR (dirigido por el índice del libro)

Los capítulos salen del libro/índice o de los archivos (`--dir`); los **cortes de lección los decide la IA por tema y densidad** (las guías llegan en Word sin formato, no hay señal tipográfica de subtemas), devolviendo solo **rangos de párrafos** → la procedencia sigue siendo 1:1; y hay un **plan editable** que el operador aprueba antes de pagar la redacción.

**Fase 1 — `plan_course` (sin redacción; escribe `plan.json` editable):**
1. **Extracción estructurada** (`extractors/structured_extractor.py`): conserva tamaño de fuente/negrita/estilos (PDF vía `get_text("dict")`, DOCX vía estilos Word) y limpia ruido (headers/pies por posición, nº de página, líneas de índice). **Reconstrucción de párrafos** (`outline.lines_to_paras`): el PDF entrega líneas físicas; se unen por continuidad de oración + interlineado (también entre páginas), se quitan guiones blandos (conservando guiones reales), el rótulo de apertura y el título del capítulo repetido como encabezado de página, y los rótulos de figura que cortan una palabra se mueven tras la oración. Así las lecciones no empiezan ni terminan a mitad de oración (plan B: 32→0 lecciones cortadas al inicio).
2. **Outline dirigido por índice** (`processors/outline.py`): capítulo → unidad, en el orden del libro. Fuentes por prioridad: (a) **carpeta de capítulos** (`--dir`, un archivo = capítulo); (b) **tier de fuente** de los divisores de capítulo (`_outline_by_font_tier`, robusto ante índices de 2 columnas); (c) **parseo del índice/TOC** (fallback); (d) headings `level 1`.
3. **Tema y densidad → lecciones** (`services/plan_structure.py` + `services/course_planner.py`): la IA (modelo principal, Sonnet; `--structure-model`) lee los párrafos numerados de cada capítulo y agrupa párrafos **consecutivos** en lecciones de **un tema/subtema coherente**; contenido denso (listas normativas, cifras, señales) → lecciones más acotadas; nunca separa un `…:` de la lista que introduce; recuadros ("DEBES SABER"…) van con su tema. `--largo corta|media|larga` = **granularidad** (≈1 subtema / 2-4 subtemas afines / tema completo; referencia 150-400 / 300-700 / 600-1200 palabras); las bandas de palabras (700 / 1200 / 1800) quedan solo como **tope de seguridad** (si una lección lo excede se sub-parte por párrafos, `corte: tope`). Devuelve solo `{desde, hasta, titulo, temas, densidad, motivo}`; validación estricta (contiguos, sin huecos/solapes), reintento con el error, presupuesto doble si la respuesta se trunca (el modelo razona antes de escribir), reparación de bordes y reintento del capítulo ante error de red. Post-proceso determinista: `merge_tiny` une lecciones bajo la mitad del mínimo y `fix_colon_borders` mueve un `…:` final a la lección siguiente. Capítulos >8k palabras se procesan por ventanas cosidas. Si el libro único no trae capítulos detectables, la IA propone también las **unidades** (`segment_units_llm` sobre `outline.book_as_single_chapter`). Sin API key, con `--sin-ia-estructura` o si la IA falla en un capítulo → corte por palabras como antes (`corte: palabras`, motivo en `resumen.estructura.errores`). Cada lección lleva su **texto fuente exacto + páginas** y `corte`/`temas`/`densidad`/`motivo_corte`; `resumen.estructura = {modo, modelo, capitulos_ia, unidades_ia, fallback, errores}`.
4. **Enriquecimiento IA opcional** (`--ia`, `services/plan_enrich.py`, Haiku por defecto): clasifica la **categoría** de cada unidad en la taxonomía cerrada y **nombra** las lecciones que no traen título de la IA de estructura (`corte` ≠ `ia`/`tope`). Best-effort (ante fallo quedan los valores previos).
5. **Revisión visual** (`processors/visual_pages.py`): con un PDF único mide por página imágenes relevantes (≥0,4% del área) y caracteres; una página es `visual` si tiene ≥4 imágenes y <60% del texto mediano, ≥25% de área de imagen, o ≥2 imágenes y <45% del texto (las divisorias sin cuerpo se ignoran). Cada lección recibe `visual {nivel, paginas_visuales, paginas_con_figuras, imagenes, referencias, motivo}`: **alta** si ≥30% de sus páginas son visuales (p. ej. el capítulo de señales), **media** si tiene figuras + referencias tipo "ver imagen superior" (con carpetas/DOCX solo cuentan las referencias). `resumen.revision_visual` = conteos. La marca viaja a la lección generada (`revision_visual`), avisa al redactor que no describa imágenes, aparece en la validación (advertencia, **no bloquea**), en el stream web y en la Brújula (alerta del plan + nota "Revisión visual" en el contenido).
6. **Figuras del libro** (`services/plan_figures.attach_figures_to_plan`, por defecto; `--sin-figuras` lo apaga): extrae y ubica cada figura en la lección **y el párrafo** del plan donde aparece en el libro (ver «Figuras del libro» más abajo), guarda los archivos en `figuras_<codigo>/` junto al plan y deja `figuras: [{id: "F-<sha10>", archivo, pagina, parrafo (tras el cual va; -1 = al inicio; null = solo por página), pie, alt, ancho, alto, mapeo, hash}]` por lección + `resumen.figuras`. `--describir-figuras` (Haiku visión) escribe pie/alt y descarta decorativas. El endpoint web `courses/plan/` aún no extrae figuras.
7. Escribe `plan.json` (unidades → lecciones con `texto`/`paginas`/`palabras_objetivo`/`visual`/`figuras`). **El operador lo revisa/edita.**

**Fase 2 — `generate_course --from-plan plan.json` (redacción):**
- Cada lección se redacta **desde su texto fuente exacto y COMPLETO** (`llm_lesson_writer.generate_lessons_from_plan`, sin truncar) → **procedencia 1:1 por construcción** (sin `map_topics`/`llm_mapper`); degradación segura ante fallo (stub marcado).
- **Prompt fiel a la fuente** (`FAITHFUL_LESSON_SYSTEM`): todo dato/regla/cifra/recomendación debe estar en el extracto (nada de "sentido común" ni conocimiento general); definiciones, normas, cifras, plazos y sanciones con la redacción del libro; sin describir imágenes. Secciones **núcleo obligatorias** (`CORE_SECTIONS`: Objetivo, Desarrollo, Puntos clave, Resumen) y **opcionales** (Introducción, Aplicación práctica, Ejemplo aplicado, Errores frecuentes, Actividad breve) que se omiten si el extracto no las respalda. **Largo proporcional** a la fuente (`lesson_length`: máx ≈1,1× palabras de la fuente, piso 250, techo = `palabras_objetivo`). temperatura 0.3. (El flujo legacy conserva su prompt de 9 secciones.)
- **Quiz por lección con evidencia** (`write_quiz_from_plan`): cada lección aporta `ceil(5/n_lecciones)` preguntas (≥1) desde SU fuente completa; cada pregunta trae `evidencia` (cita literal) que **debe aparecer en la fuente** o se descarta (`validate_quiz_question`: 4 opciones distintas, `correct_index` válido, sin "todas/ninguna de las anteriores"); opciones barajadas de forma determinista; un reintento por lección. Sin preguntas válidas → quiz extractivo marcado `fuente_debil` (bloquea el import). Cobertura en `quiz_meta` por quiz y `auditoria.quiz`. El juez ve fuente y lección completas (`_JUDGE_SOURCE_CHARS` = 30.000).
- **Figuras con marcadores** (`processors/figure_markers.py`): el redactor recibe la lista de figuras de la lección (pie/alt + «tras el párrafo N…») e inserta `{{figura:<id>}}` en una línea propia junto al contenido que ilustran; `sanitize_markers` quita ids desconocidos y duplicados y agrega al final de "## Desarrollo" las que no usó (`meta.insertada: auto`, advertencia de validación). La lección lleva `recursos` (imagen/figura con `archivo_local` relativo al JSON del curso; `generate_course` las re-basa desde la carpeta del plan). Cifras, anclaje y narración ven el texto **sin** marcadores (`strip_markers`); el juez ve `[Figura: pie]`. Validación: marcador sin recurso = error; `import_blockers` bloquea figuras referenciadas sin publicar.
- Auditoría: cifras contra la **fuente propia de cada lección**, anclaje lexical y, con `--judge`, juez de fidelidad + cobertura por lección y juez de claves de quiz (ver «Auditoría de fidelidad» del flujo legacy, compartida). Resultado en `auditoria` (`figuras`, `anclaje_bajo`, `quiz`, `juez`, `juez_quiz`) y `validacion` en el JSON.

Módulos del flujo: `extractors/structured_extractor.py`, `processors/outline.py` (+ reconstrucción de párrafos), `processors/visual_pages.py`, `services/plan_structure.py`, `services/course_planner.py`, `services/plan_enrich.py`, `processors/llm_lesson_writer.py` (prompt fiel + quiz por lección), `processors/faithfulness.py`, `processors/validators.py` (`validate_generated_course`, `import_blockers`), `services/plan_generation.py` (stream web + `manifest_from_plan`, compartido con el comando), comandos `plan_course` / `generate_course --from-plan`.

### Flujo LEGACY: estructura inferida del contenido (fallback)

`generate_course --contenido` **sin** `--from-plan` usa el flujo viejo (la IA infiere estructura/densidad desde un digest). Se conserva como fallback; para cursos nuevos preferí PLAN → GENERAR. Pasos:

1. **Estructura desde el LIBRO COMPLETO** (`manifest_from_content.build_manifest_from_content_llm`): el temario **ya NO dicta la estructura**. La estructura se infiere del contenido; los temas se piden como **strings** (JSON simple/robusto). `manifest_llm`/`manifest_builder` solo se usan para parsear el temario como checklist. Se filtran temas "meta" que el LLM cuela (`Quiz Unidad N`, `Evaluación del módulo N` → `_is_meta_tema`).
2. **Auto-dimensionado** (`course_planning.resolve_max_lecciones`): ≈1 lección por segmento, piso 8 / techo 100. Sin `--max-lecciones` se auto-dimensiona; con él, es techo. Avisa si el libro excede el tope (`truncation_notes`).
3. **Orientación por licencia** (`licenses.orientation_for(codigo, override)`): el mismo libro sirve a A1–A5; el prompt de estructura y de redacción **enfoca la licencia objetivo** (deriva del `codigo`, override con `--orientacion`).
4. **Procedencia (mapeo tema→segmentos) en llamada aparte** (`llm_mapper.map_topics_llm`): el LLM devuelve, por índice, los `segment_id` fuente de cada tema — JSON plano, con **reintentos + parser tolerante por regex** (embeber esto en el JSON de estructura lo rompía). `map_topics.map_topics_to_segments(provenance=…)` usa esos segmentos como fuente primaria; **fallback lexical/tfidf** por-tema cuando no hay procedencia válida. `coverage_alert` marca temas con fuente débil.
5. **Redacción anclada a la fuente** (`llm_lesson_writer`, RAG) con detección de truncamiento. Si un tema tiene **fuente débil** (sin segmentos o solo matches bajo umbral) la lección **se degrada al extractivo neutral** (no alucina) y se marca `fuente_debil`. Toda la construcción va en try/except: una lección nunca tumba el curso.
6. **Ancla de fuente a nivel de párrafo**: `segment_book` guarda `blocks` (texto + **página exacta**) por segmento; `lesson_generator._sources_for_segments` elige los párrafos más relevantes → `fuentes` con página precisa + extracto real.
7. **Categoría canónica** (`taxonomy`): 14 categorías compartidas por lecciones y banco de exámenes; `resolve()` deduplica variantes → `General`.
8. **Temario como checklist** (`course_planning.validate_topics_present`): valida que los temas del anexo estén representados, matcheando contra los **nombres generados Y el contenido fuente** del curso (corpus), para no dar falsos negativos con umbrellas ("Primeros auxilios" cubierto por RCP/Hemorragias).
9. **Auditoría de fidelidad** (`faithfulness`, **REPORTE, no bloquea**):
   - **Guard de cifras** (`_flag_figures`): compara cada cifra como (valor canónico, unidad): reconoce %, km/h, **g/l y mg/l (alcoholemia)**, **metros/cm/mm/km**, **segundos**, min/horas/días/meses/años, kg/t, litros/cc, °, PSI/bar, UTM/UF/$ y números **escritos con palabras** ("tres segundos"); decimales canónicos ("0,3" ≠ "3"). Marca la cifra si su valor no está en la fuente o si la fuente solo la trae con **otra unidad**. En el flujo legacy compara contra TODO el libro; desde plan, contra la **fuente propia de la lección**.
   - **Anclaje lexical** (`anchoring_score`): por-lección, marca lecciones poco conectadas con su fuente (`< anchor_min`, def. 0.28).
   - **Juez LLM opcional** (`judge_lessons_llm`, con `--judge`): puntúa **fidelidad** 0–1 revisando TODAS las afirmaciones (también ejemplos, consejos, errores frecuentes e introducción: un consejo "de sentido común" que el extracto no trae cuenta como no respaldado) y **cobertura** 0–1 con la lista de `omissions` (datos importantes del extracto que la lección no incluye). Separa **críticas** (`score < judge_min`, def. 0.7; bloquean el import), **reparos menores** y **cobertura_baja** (`coverage < 0.6`; se reporta, no bloquea). Desde plan, además `judge_quiz_llm` revisa que la clave de cada pregunta se desprenda de su evidencia y ningún distractor también sea correcto; pide el análisis antes del veredicto, ignora `ok:false` sin motivo concreto y **reconfirma** cada pregunta marcada con una segunda consulta (solo los confirmados van a `auditoria.juez_quiz.problemas`, que bloquean el import; el resto a `descartados`).

Módulos clave: `services/course_generator.py` (orquestador del stream LEGACY, eventos NDJSON `step`/`warn`/`lesson`/`done`/`error`; el web usa `plan_generation` salvo que reciba temario+contenido), `services/course_planning.py`, `processors/{manifest_from_content,llm_mapper,map_topics,llm_lesson_writer,segment_book,faithfulness,ejercicio_classifier}.py`, `taxonomy.py`, `licenses.py`.

### Modelos LLM

Defaults por `.env`: `COURSE_LLM_MODEL` (principal, def. `claude-sonnet-5`) y `COURSE_LLM_MODEL_DRAFT` (def. `claude-haiku-4-5-*`). **Ruteo por rol** (flags que sobrescriben por corrida): `--structure-model` (cortes de lección del plan, def. principal/Sonnet), `--plan-model` (enriquecimiento del plan, def. Haiku), `--lesson-model` (redacción; def. Haiku en draft / Sonnet en final), `--judge-model` (**desacoplado** del modelo de lecciones, def. Sonnet). Regla óptima: **Sonnet piensa/juzga · Haiku escribe** — Sonnet decide la estructura (cortes por tema) y juzga; la redacción (desde fuente fija) no necesita razonar. En el flujo legacy, la estructura/mapeo usan siempre el modelo principal; `--modo draft|final` es un atajo de `--lesson-model`.

### Commands (en `schools/management/commands/`)

Flujo RECOMENDADO — PLAN → GENERAR:
```powershell
# 1. Plan editable desde el libro (sin redacción). --contenido libro único  o  --dir carpeta de capítulos.
#    La IA (Sonnet) corta las lecciones por tema/densidad (~US$0,8 el libro B); --sin-ia-estructura = por palabras.
#    --largo corta|media|larga (granularidad) · --ia enriquece categorías (+ nombres sin título) con Haiku.
python manage.py plan_course --contenido "libro_b.pdf" --nombre "Curso Clase B" --codigo B --largo media --ia --out out/plan_b.json
# 2. (revisá/editá out/plan_b.json a mano)
# 3. Redacción DESDE el plan (procedencia 1:1; Haiku escribe, Sonnet juzga).
python manage.py generate_course --from-plan out/plan_b.json --nombre "Curso Clase B" --codigo B --costo 19990 --out out/b.json --judge
# 4. (opcional) Audio: generate_media (ver abajo) → agrega el recurso audio/narracion.
# 5. Publicar medios: sube figuras (y PDFs/audios locales) al storage y rellena URLs;
#    --paginas-libro arma por lección el PDF con SUS páginas del libro (recurso pdf/paginas_libro).
python manage.py publish_media --file out/b.json --out out/b_pub.json --storage s3 --paginas-libro --contenido libro_b.pdf
# 6. Importar (upsert por código; --dry-run, --prune destructivo). Bloquea si hay stubs, críticas del juez,
#    claves de quiz dudosas, figuras sin publicar o errores de validación; --forzar solo tras revisión humana.
python manage.py import_course --file out/b_pub.json
```

Flujo LEGACY — estructura inferida (fallback):
```powershell
# Estructura/densidad inferidas por IA desde el contenido. --temario opcional (checklist).
# Flags: --orientacion, --max-lecciones (auto), --judge, --judge-min 0.7, --anchor-min 0.28, --modo draft|final.
python manage.py generate_course --contenido "Libro.pdf" --temario "Temario A4.pdf" `
  --nombre "Curso Profesional Clase A4" --codigo A4 --costo 49990 --out out/a4.json --is-profesional --modo final --judge
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

Figuras del libro (PDF/Word → lección). Para cursos nuevos se extraen en `plan_course` (fase 1, paso 6) y el redactor las inserta con marcadores; la subida es `publish_media`. Para cursos **ya generados** (sin marcadores) `extract_images` las agrega como recursos (galería de no referenciadas):
```powershell
python manage.py extract_images --plan out/plan_b.json --course out/b.json --contenido libro.pdf --out out/b_img.json [--describir]
python manage.py publish_media --file out/b_img.json --out out/b_pub.json --storage s3
python manage.py import_course --file out/b_pub.json
```
- Módulo `content_pipeline/media/images.py`. **PDF** (PyMuPDF): imágenes con área ≥0,4% de la página; descarta logos/bandas repetidos (mismo xref **o** misma posición/tamaño en >30% de páginas, cubre imágenes inline), divisorias (imagen ≥70% de la página sin cuerpo), diminutas y duplicadas; une imágenes contiguas en UNA figura y la **amplía con sus anotaciones vectoriales** (trazos que tocan la imagen + rótulos cortos pegados a esos trazos: tableros con callouts, tarjetas de señales con su nombre); se **renderiza el recorte** (`get_pixmap(clip=…)`, 170 dpi, PNG o JPEG si >250 KB). **Word**: recorre `document.xml` en orden; cada `a:blip`/`v:imagedata` → `word/media/*` (emf/wmf se descartan), anclada exactamente entre párrafos.
- **Anclas y mapeo** (`map_figures_to_plan`): hasta 3 bloques de cuerpo antes/después de la figura (≥6 palabras, sin cabeceras/pies de página — texto repetido en ≥3 páginas, dígitos normalizados) → ventanas de 8 palabras buscadas en el `texto` de cada lección del plan (`mapeo: texto`); respaldo por página si el plan trae páginas informativas (`pagina`, ≤3 candidatas) o primera lección de la unidad del archivo con `--dir` (`unidad`); si no, `sin_leccion` (nunca a ciegas). Enlace al curso por `plan_id` (respaldo: unidad + nombre). Pie: bloque corto pegado bajo la figura o rótulo interno (1-2 bloques); con `--describir` la IA da `pie`/`alt` (solo lo visible) y descarta decorativas (`relevante:false`).
- Storage compartido con el audio (`media/storage.get_storage(name, kind="audio"|"images"|"docs")`): local `MEDIA_ROOT/course_{audio,images,docs}/` o S3 con prefijos `AUDIO_S3_PREFIX`/`IMAGES_S3_PREFIX`/`DOCS_S3_PREFIX` (mismo bucket y credenciales `AUDIO_S3_*`). `publish_media` nombra `{codigo}/{sha1[:16]}.{ext}` (figuras) y `{codigo}/paginas_AAA-BBB.pdf` → re-correr no re-sube. Mapeo por **párrafo**: el ancla antes de la figura ubica tras el párrafo donde termina; el ancla posterior, antes del párrafo donde empieza.
- `import_course` (`exporters/django_importer._sync_recursos`): `lessons[].recursos` → upsert de `LeccionRecurso` por `(leccion, clave)`; con `recursos` en el JSON se reemplaza todo (las claves ausentes se borran); JSON previos: `imagenes` reemplaza solo el rol figura y `url_*` reales se agregan como recursos sin borrar nada. Recursos sin `url` no se crean ni borran el publicado. Medido: `libro.pdf` 351 figuras (246 por texto / 105 por página, 0 sin lección), `libro_c.pdf` 235, `libro_pro.pdf` 676 (~90 s); redacción de prueba (2 lecciones, Haiku): 9/9 figuras insertadas por el redactor, 0 automáticas.

Legacy A2 (hardcodeados al Curso Profesional A2, **no** usan el flujo genérico ni taxonomía/orientación/auditoría; solo para ese curso): `extract_a2_book`, `build_a2_lessons`, `build_a2_pipeline`, `validate_a2_course`, `import_a2_course`.

Demo/seed: `seed_catalogo` — datos de ejemplo (dev).

### Notas operativas

- Dos vías para crear un curso: (a) **CLI** — `plan_course` + `generate_course` en local (caro/IA), `publish_media --storage s3` sube figuras/páginas del libro a R2 y el JSON publicado se sube con `import_course` en el entorno desplegado (sin IA ni PDFs; ver la memoria de seed para poblar Railway con `DATABASE_URL` pública); (b) **web** — el generador del panel admin (`autotest_website/src/components/CourseGenerator.js`) llama a `courses/plan/` → revisión del plan → `courses/generate/` (requiere `ANTHROPIC_API_KEY` en el backend que atiende) → `courses/import/` si hubo bloqueos aceptados o para "Guardar en esta BD". Con el dashboard local y "Guardar al terminar" apagado se genera sin tocar la BD local y se descarga el JSON para llevarlo a producción con `import_course` (siempre hay botón "Descargar curso (JSON)"). Ambas vías aplican el mismo bloqueo de fidelidad.
- **Paralelismo**: `generate_course` NO toca la BD → se puede correr en paralelo (varios terminales, cada uno su `--out`; el límite es el rate-limit de Anthropic). `import_course` NO en paralelo (SQLite bloquea la BD; y comparten categorías canónicas) — importar en serie.
- **Iterar barato**: para bajar reparos, preferir ajuste de prompt/pipeline cuando el hallazgo es **sistémico** (se amortiza en todos los cursos) o parche manual del JSON cuando es **puntual**; NO regenerar el curso completo solo para perseguir reparos (caro y no determinista). Diagnosticar en `draft`, correr `final` una sola vez al cierre.
- El JSON generado incluye `auditoria` (cifras/anclaje/juez) cuando se corre con `--judge`; la Brújula lo muestra por lección (ver abajo).

### Revisión humana — Brújula del Libro (`content_pipeline/review/`)

Herramienta HTML de una sola página (`content_pipeline/review/brujula.html`, fuente única; también publicada como artifact) con 3 fases: **Estructura** (plan.json editable + aprobación), **Contenido** (curso generado vs fuente + hallazgos del juez) y **Revisión** (observaciones de auditores humanos). Dos roles: **Auditor** (sus acciones quedan como *propuestas*; nunca cambian el plan/curso) y **Editor** (dueño: acepta/rechaza; lo aceptado se aplica). Sin backend: estado en localStorage; el intercambio es por archivos.

Ciclo:
```powershell
# 1. ZIP offline para auditores (brujula.html autocontenido + LEEME.txt; abre con doble clic)
python manage.py build_brujula --plan out/plan_b.json [--course out/b.json] --out out/brujula_b.zip
# 2. Cada auditor devuelve review_<codigo>_<autor>.json → el Editor los carga en la Brújula, decide y exporta review_<codigo>.json
# 3. Aplicar lo ACEPTADO (sin IA; conflictos se reportan y no se aplican)
python manage.py apply_review --review out/review_b.json --plan out/plan_b.json --out out/plan_b_rev.json     # estructura
python manage.py apply_review --review out/review_b.json --course out/b.json --out out/b_rev.json [--dry-run]  # contenido
```
- **Ids estables** (`review/ids.py`): lección de plan `L-`+sha256(texto)[:10], unidad `U-`+sha256(nombre|primer id)[:8] (`plan_course` los emite; `build_brujula` rellena planes viejos); dividir/unir derivan `-a`/`-b` y `a+b`. Las lecciones de `generate_course --from-plan` llevan `plan_id`. Lecciones de curso se ubican por `plan_id` o clave `U{unidad_orden}.{posicion}`. **La Brújula (JS) y `review/apply.py` implementan las mismas ops** (`rename, texto, categoria, cap, reorder, split, merge, move, new_unit, delete, move_unit, merge_unit, figura_quitar, figura_pie, figura_mover`; al dividir/unir, las figuras viajan con su párrafo): si cambiás una, cambiá la otra.
- **Figuras en la Brújula**: `build_brujula` copia las figuras del plan/curso al ZIP (`figuras/`). En Estructura, bajo la lectura, cada figura con su ubicación («tras el párrafo N…»), página y mapeo, y acciones Pie · ↑/↓ párrafo · lección anterior/siguiente · Quitar (auditor propone, editor aplica). En Contenido los `{{figura:…}}` se ven como la imagen con su pie.
- **Anotaciones ancladas**: el revisor selecciona texto (fuente del plan, contenido, enunciado/opciones/explicación del quiz) y elige *Resaltar · Comentar · Sugerir cambio / Corregir fuente · Error · Pedir fuente · Dividir aquí*. Cada nota guarda `anclaje {campo, q?, i?, cita, occ}` (n-ésima aparición de `cita` en el campo crudo) y se muestra al margen, vinculada a la marca; admite respuestas (`respuestas[]`). Layout: índice · página de lectura · notas. La estructura se cambia desde una **barra de herramientas fija** sobre la página (misma para toda lección: renombrar, ↑/↓, mover, dividir, unir, nueva unidad, tope, quitar, comentar); al hacer clic en una unidad del índice se abre su **vista de unidad** (renombrar, categoría, ↑/↓, unir, quitar, dividir la unidad entre lecciones, comentar). El auditor siempre propone (no hay modo que activar); el editor aplica directo mientras la estructura no esté aprobada.
- **Correcciones** = reemplazo acotado `{campo, buscar, reemplazar}` (`buscar` se amplía con contexto hasta ser único; debe aparecer exactamente 1 vez) o `{campo, valor}` (quiz: `pregunta|opcion|correcta|explicacion`). Sobre la fuente del plan: op `texto {buscar, reemplazar}` (limpia ruido de extracción; el id de la lección no cambia). Renombrar una lección cambia su identidad en `import_course` (curso+unidad+posición+nombre).
- `apply_review` agrega un registro a `revision_humana` en el JSON de salida (aplicadas, conflictos, autores).

### Bloqueo de importación (fidelidad)

- `generate_course` (y el stream web) guarda `validacion` en el JSON (`validators.validate_generated_course`: secciones **núcleo** obligatorias, fuentes/páginas, quiz válido, lecciones stub o de fuente débil, quizzes sin preguntas válidas; `revision_visual` lista las lecciones de fuente gráfica como advertencia) y la imprime.
- Si una lección no se pudo redactar, queda como **stub marcado** (`fuente_debil: true`) — nunca como contenido normal.
- `import_course` **bloquea** (`validators.import_blockers`) si hay errores de validación, lecciones stub/fuente débil, **críticas del juez** o **claves de quiz dudosas** (`juez_quiz`); `--dry-run` solo las lista. `--forzar` importa igual (solo tras revisión humana, p. ej. en la Brújula). Los validadores A2 (`validate_lessons`) siguen siendo exclusivos del curso A2.

## Conventions

- Models, fields, and serializers use Spanish identifiers (`Usuario`, `nombre`, `apellido`, `escuela`, `pregunta`, `respuesta`). Match that — don't introduce English names mid-domain.
- `__str__` methods on `Prueba` / `PruebaEjercicio` concatenate an int PK with `+` (`'#'+self.id+...`) — that's a latent bug (`TypeError`); don't copy the pattern. Cast with `str()` or use f-strings if you touch them.
- View files are large (`accounts/views.py` ~1.3k LOC, `sales/views.py` and `schools/views.py` ~1.5k LOC each) and mix `APIView`, `ViewSet`, and generics — when extending, follow the existing pattern in the same file rather than refactoring.
- Business logic that crosses models lives in `sales/services.py` (`asignar_por_source`, `activar_curso_para_estudiante`, `canjear_access_key`, `generar_codigos`, `anular_codigo`, `registrar_compra_curso_individual`, `registrar_venta_unificada`, `precio_plan`, `precio_final_producto`, `tiene_acceso_a_curso`, `llaves_para_dias`, …). Prefer adding to `services.py` over inflating views.
