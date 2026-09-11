from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from django.db import transaction

from schools.models import Categoria, Curso, Ejercicio, Leccion, LeccionFuente, Unidad


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


def _would_update(model, **lookup: Any) -> bool:
    return model.objects.filter(**lookup).exists()


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
        category_names = {str(unidad.get("categoria")) for unidad in unidades_spec}
        category_names.update(str(lesson.get("categoria")) for lesson in lessons if lesson.get("categoria"))
        for name in category_names:
            summary.add("categoria_update" if _would_update(Categoria, nombre=name) else "categoria_create")
        for lesson in lessons:
            summary.add("leccion_check")
            summary.add("fuente_sync", len(lesson.get("fuentes") or []))
        return summary

    touched_lesson_ids: set[int] = set()
    with transaction.atomic():
        curso, created = Curso.objects.update_or_create(
            codigo=curso_spec["codigo"],
            defaults={
                "nombre": curso_spec["nombre"],
                "descripcion": curso_spec["descripcion"],
                "is_profesional": bool(curso_spec.get("is_profesional", True)),
                "costo": curso_spec.get("costo"),
                "url_image": "http://placeholder.url",
                "url_icon": "http://placeholder.url",
            },
        )
        summary.add("curso_create" if created else "curso_update")

        categorias: dict[str, Categoria] = {}
        for unidad in unidades_spec:
            name = str(unidad.get("categoria", "General"))
            categoria, created = Categoria.objects.get_or_create(nombre=name, defaults={"color_hex": "#545050"})
            categorias[name] = categoria
            summary.add("categoria_create" if created else "categoria_update")

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
            categoria_name = str(lesson.get("categoria") or "General")
            categoria = categorias.get(categoria_name)
            if categoria is None:
                categoria, created = Categoria.objects.get_or_create(nombre=categoria_name, defaults={"color_hex": "#545050"})
                categorias[categoria_name] = categoria
                summary.add("categoria_create" if created else "categoria_update")

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

        if prune:
            stale = Leccion.objects.filter(curso=curso).exclude(id__in=touched_lesson_ids)
            summary.add("leccion_delete", stale.count())
            stale.delete()

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


def import_ejercicios(
    ejercicios: list[dict[str, Any]],
    dry_run: bool = False,
) -> ImportSummary:
    """Crea filas ``Ejercicio`` desde una lista parseada/clasificada.

    Convenciones (ver seed-deployed-db):
      - ``curso``/``leccion`` quedan en NULL (van al pool general + categoría).
      - ``categoria`` se resuelve por nombre (``get_or_create``). Obligatoria: si
        falta, el ítem se omite (el examen por categoría la necesita).
      - Idempotente: dedupe por texto de pregunta normalizado (igual que el
        importador Excel del catálogo). Re-importar no duplica.
      - Soporta multi-respuesta: ``respuestas_correctas`` guarda las keys; para
        respuesta única además se setea ``respuesta`` (texto) por compatibilidad.
      - Se omiten los ítems marcados ``diferir`` (imagen / datos insuficientes).

    Devuelve un ``ImportSummary`` con contadores.
    """
    summary = ImportSummary(dry_run=dry_run)

    # Preguntas ya en BD (normalizadas) para dedupe idempotente.
    existentes = {
        _norm_pregunta(p)
        for p in Ejercicio.objects.values_list("pregunta", flat=True)
    }
    vistas_en_lote: set[str] = set()

    a_crear: list[Ejercicio] = []
    categoria_cache: dict[str, Categoria] = {}

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

        clave = _norm_pregunta(pregunta)
        if clave in existentes or clave in vistas_en_lote:
            summary.add("duplicado")
            continue
        vistas_en_lote.add(clave)

        if dry_run:
            summary.add("ejercicio_create")
            summary.add(f"categoria::{categoria_nombre}")
            continue

        categoria = categoria_cache.get(categoria_nombre)
        if categoria is None:
            categoria, created = Categoria.objects.get_or_create(
                nombre=categoria_nombre, defaults={"color_hex": "#545050"}
            )
            categoria_cache[categoria_nombre] = categoria
            summary.add("categoria_create" if created else "categoria_reuse")

        es_multi = bool(e.get("multi"))
        # Texto de la opción correcta (solo tiene sentido para respuesta única).
        respuesta_texto = ""
        if not es_multi and len(respuestas) == 1:
            respuesta_texto = opciones.get(respuestas[0], "") or respuestas[0]

        a_crear.append(Ejercicio(
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
        ))
        summary.add("ejercicio_create")

    if not dry_run and a_crear:
        with transaction.atomic():
            Ejercicio.objects.bulk_create(a_crear, batch_size=100)

    return summary
