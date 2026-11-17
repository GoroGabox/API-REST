from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from django.db import transaction

from content_pipeline import taxonomy
from schools.models import Categoria, Curso, Ejercicio, Leccion, LeccionFuente, LeccionRecurso, PlanCurso, Unidad
from schools.services import PLACEHOLDER_URL, sync_media_mirror


@dataclass
class ImportSummary:
    dry_run: bool
    counters: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def add(self, key: str, amount: int = 1) -> None:
        self.counters[key] += amount

    def as_lines(self) -> list[str]:
        prefix = "DRY-RUN " if self.dry_run else ""
        keys = sorted(self.counters)
        return [f"{prefix}{key}: {self.counters[key]}" for key in keys]


def _serialize_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _lesson_defaults(lesson: dict[str, Any], categoria: Categoria) -> dict[str, Any]:
    return {
        "categoria": categoria,
        "tipo": lesson.get("tipo", "texto"),
        "descripcion": lesson.get("descripcion", ""),
        "contenido": _serialize_content(lesson.get("contenido", "")),
        "transcripcion": lesson.get("transcripcion", ""),
        "duracion_min": int(lesson.get("duracion_min") or 0),
        "url_video": lesson.get("url_video", "http://placeholder.url") or "http://placeholder.url",
        "url_audio": lesson.get("url_audio", "http://placeholder.url") or "http://placeholder.url",
        "url_pdf": lesson.get("url_pdf", "") or "",
    }


_FIGURA_META = ("pie", "alt", "pagina", "ancho", "alto", "origen", "mapeo", "hash")
_MEDIOS_LEGACY = (("url_audio", "audio", "narracion", "audio"),
                  ("url_video", "video", "principal", "video"),
                  ("url_pdf", "pdf", "descarga", "pdf"))


def _recursos_spec(lesson: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, set[str]]:
    """(recursos, reemplazar_todo, roles_a_reemplazar) desde el JSON de la lección.

    - ``recursos`` (formato actual) → reemplazo completo por clave.
    - JSON previos: ``imagenes`` → figuras (reemplaza solo el rol figura) y
      ``url_audio/url_video/url_pdf`` reales → recursos (upsert sin borrar nada).
    """
    items: list[dict[str, Any]] = []
    roles: set[str] = set()
    todo = isinstance(lesson.get("recursos"), list)
    if todo:
        items.extend(r for r in lesson["recursos"] if isinstance(r, dict) and r.get("clave"))
    elif isinstance(lesson.get("imagenes"), list):
        roles.add("figura")
        for k, img in enumerate(lesson["imagenes"], start=1):
            clave = img.get("clave") or (f"F-{img['hash'][:10]}" if img.get("hash") else f"F-{k}")
            items.append({"tipo": "imagen", "rol": "figura", "clave": clave, "url": img.get("url") or "",
                          "orden": img.get("orden") or k,
                          "meta": {key: img.get(key) for key in _FIGURA_META if img.get(key) is not None}})
    claves = {r["clave"] for r in items}
    for campo, tipo, rol, clave in _MEDIOS_LEGACY:
        url = lesson.get(campo) or ""
        ya_hay = clave in claves or any(r.get("tipo") == tipo for r in items)
        if url and url != PLACEHOLDER_URL and not ya_hay:
            items.append({"tipo": tipo, "rol": rol, "clave": clave, "url": url, "orden": 1000})
    return items, todo, roles


def _sync_recursos(leccion: Leccion, lesson: dict[str, Any], summary: "ImportSummary") -> None:
    """Upsert de ``LeccionRecurso`` por ``(leccion, clave)`` + espejo ``url_*``.

    Recursos sin ``url`` (archivo local aún no publicado con ``publish_media``) no se
    crean, pero tampoco borran el que ya estuviera publicado con esa clave.
    """
    items, todo, roles = _recursos_spec(lesson)
    keep: set[str] = set()
    for r in items:
        keep.add(r["clave"])
        if not r.get("url"):
            summary.add("recurso_sin_url")
            continue
        _obj, created = LeccionRecurso.objects.update_or_create(
            leccion=leccion, clave=str(r["clave"])[:64],
            defaults={
                "tipo": r.get("tipo") or "imagen",
                "rol": r.get("rol") or "figura",
                "url": r["url"],
                "orden": int(r.get("orden") or 0),
                "titulo": str(r.get("titulo") or "")[:255],
                "meta": r.get("meta") or {},
            },
        )
        summary.add("recurso_create" if created else "recurso_update")
    stale = LeccionRecurso.objects.filter(leccion=leccion).exclude(clave__in=keep)
    if not todo:
        stale = stale.filter(rol__in=roles) if roles else stale.none()
    n = stale.count()
    if n:
        for obj in stale:          # uno a uno: dispara la señal del espejo
            obj.delete()
        summary.add("recurso_delete", n)
    sync_media_mirror(leccion)


def _would_update(model, **lookup: Any) -> bool:
    return model.objects.filter(**lookup).exists()


def _get_or_create_categoria(
    name: str, cache: dict[str, Categoria], summary: ImportSummary
) -> Categoria:
    """Resuelve un nombre libre a la categoría canónica y la upserta.

    Deduplica por CLAVE CANÓNICA (sin acentos/casing): todas las variantes de un
    mismo concepto colapsan en una única fila, con su color oficial. Cachea por
    nombre canónico para no repetir consultas dentro de un import.
    """
    canonical = taxonomy.resolve(name)
    cached = cache.get(canonical)
    if cached is not None:
        return cached
    categoria, created = Categoria.objects.get_or_create(
        nombre=canonical, defaults={"color_hex": taxonomy.color_for(canonical)}
    )
    cache[canonical] = categoria
    summary.add("categoria_create" if created else "categoria_update")
    return categoria


def import_a2_course(
    manifest: dict[str, Any],
    lessons: list[dict[str, Any]],
    dry_run: bool = False,
    prune: bool = False,
) -> ImportSummary:
    summary = ImportSummary(dry_run=dry_run)
    curso_spec = manifest["curso"]
    unidades_spec = [unidad for unidad in manifest.get("unidades", []) if isinstance(unidad, dict)]

    if dry_run:
        summary.add("curso_update" if _would_update(Curso, codigo=curso_spec["codigo"]) else "curso_create")
        for unidad in unidades_spec:
            if _would_update(Curso, codigo=curso_spec["codigo"]):
                curso = Curso.objects.get(codigo=curso_spec["codigo"])
                exists = Unidad.objects.filter(curso=curso, orden=unidad["orden"]).exists()
            else:
                exists = False
            summary.add("unidad_update" if exists else "unidad_create")
        # Nombres canónicos: distintas variantes cuentan como una sola categoría.
        category_names = {taxonomy.resolve(unidad.get("categoria")) for unidad in unidades_spec}
        category_names.update(taxonomy.resolve(lesson.get("categoria")) for lesson in lessons)
        for name in category_names:
            summary.add("categoria_update" if _would_update(Categoria, nombre=name) else "categoria_create")
        for lesson in lessons:
            summary.add("leccion_check")
            summary.add("fuente_sync", len(lesson.get("fuentes") or []))
        curso_bd = Curso.objects.filter(codigo=curso_spec["codigo"]).first()
        if prune and curso_bd is not None:
            # Qué borraría --prune: lecciones que no están en el JSON (identidad = unidad +
            # posición + nombre), el avance de alumnos en ellas y las unidades sobrantes.
            from django.apps import apps
            claves = {(int(l["unidad_orden"]), int(l["posicion"]), l["nombre"]) for l in lessons}
            stale = [x.id for x in Leccion.objects.filter(curso=curso_bd).select_related("unidad")
                     if (x.unidad.orden if x.unidad else None, x.posicion, x.nombre) not in claves]
            summary.add("leccion_delete", len(stale))
            avance = apps.get_model("accounts", "EstudianteLeccion").objects.filter(leccion_id__in=stale)
            summary.add("avance_alumnos_delete", avance.count())
            ordenes = {int(u["orden"]) for u in unidades_spec}
            summary.add("unidad_delete", Unidad.objects.filter(curso=curso_bd).exclude(orden__in=ordenes).count())
        return summary

    touched_lesson_ids: set[int] = set()
    with transaction.atomic():
        curso, created = Curso.objects.update_or_create(
            codigo=curso_spec["codigo"],
            defaults={
                "nombre": curso_spec["nombre"],
                "descripcion": curso_spec["descripcion"],
                "is_profesional": bool(curso_spec.get("is_profesional", True)),
                "url_image": "http://placeholder.url",
                "url_icon": "http://placeholder.url",
            },
        )
        summary.add("curso_create" if created else "curso_update")

        # El precio vive en PlanCurso: el valor unitario (`costo` del spec) crea
        # o actualiza el plan de 7 días. Los planes 14/35 se editan en Admin.
        precio_unitario = int(curso_spec.get("costo") or 0)
        if precio_unitario > 0:
            PlanCurso.objects.update_or_create(
                curso=curso, dias=7,
                defaults={"precio": precio_unitario, "activo": True, "orden": 7},
            )

        categorias: dict[str, Categoria] = {}
        for unidad in unidades_spec:
            _get_or_create_categoria(unidad.get("categoria"), categorias, summary)

        unidades: dict[int, Unidad] = {}
        for unidad_spec in unidades_spec:
            unidad, created = Unidad.objects.update_or_create(
                curso=curso,
                orden=int(unidad_spec["orden"]),
                defaults={
                    "nombre": unidad_spec["nombre"],
                    "descripcion": f"{unidad_spec.get('horas_elearning', 0)} horas e-learning",
                },
            )
            unidades[int(unidad_spec["orden"])] = unidad
            summary.add("unidad_create" if created else "unidad_update")

        for lesson in lessons:
            unidad = unidades[int(lesson["unidad_orden"])]
            categoria = _get_or_create_categoria(lesson.get("categoria"), categorias, summary)

            leccion, created = Leccion.objects.get_or_create(
                curso=curso,
                unidad=unidad,
                posicion=int(lesson["posicion"]),
                nombre=lesson["nombre"],
                defaults=_lesson_defaults(lesson, categoria),
            )
            if not created:
                defaults = _lesson_defaults(lesson, categoria)
                for field_name, value in defaults.items():
                    setattr(leccion, field_name, value)
                leccion.save(update_fields=list(defaults.keys()))
            touched_lesson_ids.add(leccion.id)
            summary.add("leccion_create" if created else "leccion_update")

            LeccionFuente.objects.filter(leccion=leccion).delete()
            for source in lesson.get("fuentes") or []:
                LeccionFuente.objects.create(
                    leccion=leccion,
                    fuente_nombre=source.get("fuente_nombre", ""),
                    pagina_inicio=int(source.get("pagina_inicio") or 0),
                    pagina_fin=int(source.get("pagina_fin") or 0),
                    tema_regulatorio=source.get("tema_regulatorio", lesson.get("tema_regulatorio", "")),
                    fragmento_resumen=source.get("fragmento_resumen", ""),
                    hash_fragmento=source.get("hash_fragmento", ""),
                )
                summary.add("fuente_create")

            _sync_recursos(leccion, lesson, summary)

        if prune:
            stale = Leccion.objects.filter(curso=curso).exclude(id__in=touched_lesson_ids)
            summary.add("leccion_delete", stale.count())
            stale.delete()
            # Unidades que ya no están en el JSON (p. ej. un curso que pasa de 8 a 7 unidades).
            stale_u = Unidad.objects.filter(curso=curso).exclude(orden__in=list(unidades))
            summary.add("unidad_delete", stale_u.count())
            stale_u.delete()

    return summary


def import_generated_course(
    manifest: dict[str, Any],
    lessons: list[dict[str, Any]],
    dry_run: bool = False,
    prune: bool = False,
) -> tuple[ImportSummary, Curso]:
    """Persiste un curso generado por el pipeline y devuelve el Curso creado.

    Reutiliza `import_a2_course` (la lógica de upsert es agnóstica al código de
    curso) y además recupera la instancia `Curso` para que el llamador pueda
    reportar su id/nombre/código —lo que el endpoint de streaming necesita para
    el evento ``done``.
    """
    summary = import_a2_course(manifest, lessons, dry_run=dry_run, prune=prune)
    curso = Curso.objects.get(codigo=manifest["curso"]["codigo"])
    return summary, curso


# ---------------------------------------------------------------------------
# Importador de ejercicios (banco de preguntas del cuestionario Clase B)
# ---------------------------------------------------------------------------
_LETRAS = ("a", "b", "c", "d", "e", "f")


def _norm_pregunta(texto: str) -> str:
    return " ".join((texto or "").split()).strip().lower()


def clave_ejercicio(pregunta: str, opciones) -> str:
    """Identidad de un ejercicio: enunciado + alternativas normalizados.

    Las variantes de una pregunta comparten enunciado, así que el enunciado solo
    no basta. ``opciones`` es ``{letra: texto}`` o una lista de textos; el orden
    de las alternativas no importa.
    """
    textos = opciones.values() if isinstance(opciones, dict) else (opciones or [])
    alts = sorted(_norm_pregunta(t) for t in textos if (t or "").strip())
    return _norm_pregunta(pregunta) + " || " + " | ".join(alts)


def clave_de_fila(e: Ejercicio) -> str:
    return clave_ejercicio(e.pregunta, [getattr(e, f"opcion_{k}") for k in _LETRAS])


def _fila_truncada(candidatas: list[Ejercicio], opciones: dict) -> Ejercicio | None:
    """Fila con el mismo enunciado cuyas alternativas son el comienzo de las nuevas
    (mismas letras; al menos una cortada). ``None`` si no hay una sola así."""
    nuevas = {k: _norm_pregunta(v) for k, v in opciones.items() if (v or "").strip()}
    halladas = []
    for fila in candidatas:
        viejas = {k: _norm_pregunta(getattr(fila, f"opcion_{k}")) for k in _LETRAS
                  if (getattr(fila, f"opcion_{k}") or "").strip()}
        if set(viejas) != set(nuevas) or viejas == nuevas:
            continue
        if all(nuevas[k].startswith(v) and len(v) >= 20 or nuevas[k] == v for k, v in viejas.items()):
            halladas.append(fila)
    return halladas[0] if len(halladas) == 1 else None


def import_ejercicios(
    ejercicios: list[dict[str, Any]],
    dry_run: bool = False,
    curso: Curso | None = None,
) -> ImportSummary:
    """Crea filas ``Ejercicio`` desde una lista parseada/clasificada.

    - ``categoria`` (canónica, ``taxonomy.resolve``) es obligatoria.
    - Idempotente: dedupe por enunciado + alternativas (``clave_ejercicio``), así
      las variantes de una misma pregunta se cargan todas y re-importar no duplica.
    - Con ``curso``: cada ítem con ``unidad_orden`` se vincula a esa unidad del
      curso (``EjercicioUnidad(origen='docx')``). Si el ejercicio ya existía
      (p. ej. vino de otra licencia) solo se agrega el vínculo.
    - Guarda ``grupo`` y ``fuente`` si vienen (sin pisar valores existentes).
    - Multi-respuesta: ``respuestas_correctas`` guarda las keys; con respuesta
      única además se setea ``respuesta`` (texto) por compatibilidad.
    - Se omiten los ítems ``diferir``.
    """
    from schools.models import EjercicioUnidad

    summary = ImportSummary(dry_run=dry_run)
    existentes = {clave_de_fila(e): e for e in Ejercicio.objects.all()}
    por_enunciado: dict[str, list[Ejercicio]] = defaultdict(list)
    for fila in existentes.values():
        por_enunciado[_norm_pregunta(fila.pregunta)].append(fila)
    unidades = {u.orden: u for u in Unidad.objects.filter(curso=curso)} if curso else {}
    vistas_en_lote: dict[str, Any] = {}
    categoria_cache: dict[str, Categoria] = {}

    with transaction.atomic():
        for e in ejercicios:
            if e.get("diferir"):
                summary.add("diferido")
                continue
            pregunta = (e.get("pregunta") or "").strip()
            opciones = e.get("opciones") or {}
            respuestas = [str(r).strip().lower() for r in (e.get("respuestas") or [])]
            respuestas = [r for r in respuestas if r in opciones]
            categoria_nombre = (e.get("categoria") or "").strip()
            if not pregunta or not opciones:
                summary.add("omitido_sin_datos")
                continue
            if not respuestas:
                summary.add("omitido_sin_respuesta")
                continue
            if not categoria_nombre:
                summary.add("omitido_sin_categoria")
                continue

            unidad = None
            if curso is not None and e.get("unidad_orden") is not None:
                unidad = unidades.get(int(e["unidad_orden"]))
                if unidad is None:
                    summary.add(f"sin_unidad_en_curso::{e['unidad_orden']}")

            clave = clave_ejercicio(pregunta, opciones)
            ejercicio = existentes.get(clave) or vistas_en_lote.get(clave)
            truncado = None if ejercicio is not None else _fila_truncada(
                por_enunciado.get(_norm_pregunta(pregunta), []), opciones)
            if truncado is not None:
                # Misma pregunta guardada con alternativas cortadas (cargas viejas):
                # se repara con el texto completo en vez de duplicarla.
                summary.add("reparado_truncado")
                ejercicio = truncado
                if not dry_run:
                    for letra in _LETRAS:
                        setattr(ejercicio, f"opcion_{letra}", opciones.get(letra))
                    ejercicio.respuestas_correctas = respuestas
                    ejercicio.multiple = bool(e.get("multi"))
                    ejercicio.respuesta = (opciones.get(respuestas[0], "")
                                           if len(respuestas) == 1 and not ejercicio.multiple else "")
                    ejercicio.save()
                existentes[clave] = ejercicio
            elif ejercicio is not None:
                summary.add("duplicado")
            else:
                canonical = taxonomy.resolve(categoria_nombre)
                summary.add("ejercicio_create")
                summary.add(f"categoria::{canonical}")
                if dry_run:
                    vistas_en_lote[clave] = True
                    if unidad is not None:
                        summary.add("vinculo_unidad_create")
                    continue
                categoria = categoria_cache.get(canonical)
                if categoria is None:
                    categoria, created = Categoria.objects.get_or_create(
                        nombre=canonical, defaults={"color_hex": taxonomy.color_for(canonical)}
                    )
                    categoria_cache[canonical] = categoria
                    summary.add("categoria_create" if created else "categoria_reuse")
                es_multi = bool(e.get("multi"))
                respuesta_texto = ""
                if not es_multi and len(respuestas) == 1:
                    respuesta_texto = opciones.get(respuestas[0], "") or respuestas[0]
                ejercicio = Ejercicio.objects.create(
                    categoria=categoria,
                    curso=None,
                    leccion=None,
                    pregunta=pregunta,
                    imagen="http://placeholder.url",
                    opcion_a=opciones.get("a"),
                    opcion_b=opciones.get("b"),
                    opcion_c=opciones.get("c"),
                    opcion_d=opciones.get("d"),
                    opcion_e=opciones.get("e"),
                    opcion_f=opciones.get("f"),
                    respuesta=respuesta_texto,
                    multiple=es_multi,
                    respuestas_correctas=respuestas,
                    explicacion=e.get("explicacion", "") or "",
                    grupo=(e.get("grupo") or "")[:64],
                    fuente=(e.get("fuente") or "")[:120],
                )
                vistas_en_lote[clave] = ejercicio

            if dry_run:
                # Duplicado en dry-run: `True` = visto antes en este mismo lote.
                if unidad is not None and ejercicio is not True:
                    ya = EjercicioUnidad.objects.filter(ejercicio=ejercicio, unidad=unidad).exists()
                    summary.add("vinculo_unidad_existente" if ya else "vinculo_unidad_create")
                continue
            cambios = []
            for campo, largo in (("grupo", 64), ("fuente", 120)):
                if e.get(campo) and not getattr(ejercicio, campo):
                    setattr(ejercicio, campo, e[campo][:largo])
                    cambios.append(campo)
            if cambios:
                ejercicio.save(update_fields=cambios)
            if unidad is not None:
                _, creado = EjercicioUnidad.objects.get_or_create(
                    ejercicio=ejercicio, unidad=unidad, defaults={"origen": "docx"})
                summary.add("vinculo_unidad_create" if creado else "vinculo_unidad_existente")
        if dry_run:
            transaction.set_rollback(True)

    return summary
