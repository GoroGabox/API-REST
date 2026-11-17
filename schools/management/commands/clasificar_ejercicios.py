"""Clasifica el banco de ejercicios en las unidades y categorías de un curso.

Fase 1 (LLM, offline, no escribe en la BD):

    python manage.py clasificar_ejercicios --curso B --out out/ejercicios/clasif_b.json

    Por cada ejercicio: ``{unidad_orden | null, categoria, confianza, motivo}``.
    ``null`` = no pertenece a este curso (queda en el pool común). La categoría es
    siempre una de la taxonomía canónica. Se omiten los ejercicios que ya tienen
    una unidad de este curso (p. ej. los del cuestionario del curso).

Fase 1b (lecciones, por lección):

    python manage.py clasificar_ejercicios --curso B --lecciones \
        --course-json out/b_pub_v4.json --out out/b_pub_v5.json

    Categoría por lección de texto según título + «Puntos clave»; el quiz de la
    unidad toma la categoría más frecuente de sus lecciones. Escribe el JSON del
    curso (``import_course`` lo lleva a la BD).

Fase 2 (sin LLM, también en prod):

    python manage.py clasificar_ejercicios --aplicar out/ejercicios/clasif_b.json [--dry-run]

    Ubica cada ejercicio por clave (enunciado + alternativas), escribe la
    categoría y agrega el vínculo ``EjercicioUnidad(origen='clasificacion')`` a la
    unidad del curso, sin tocar vínculos de otros cursos.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from content_pipeline import taxonomy
from content_pipeline.exporters.django_importer import clave_de_fila
from content_pipeline.llm.client import LLMClient, LLMError, draft_model, parse_json_object
from schools.models import Categoria, Curso, Ejercicio, EjercicioUnidad, Leccion

_LOTE = 25
_CATEGORIAS = list(taxonomy.CATEGORY_NAMES)


def _puntos_clave(contenido: str, limite: int = 500) -> str:
    m = re.search(r"## Puntos clave\s*\n(.*?)(?:\n## |\Z)", contenido or "", re.S)
    return " ".join((m.group(1) if m else "").split())[:limite]


def contexto_curso(curso: Curso) -> str:
    """Unidades del curso con sus lecciones y puntos clave (para el prompt)."""
    partes = []
    for u in curso.unidades.order_by("orden"):
        lecciones = Leccion.objects.filter(unidad=u, tipo="texto").order_by("posicion")
        detalle = "\n".join(
            f"   - {l.nombre}: {_puntos_clave(l.contenido, 300)}" for l in lecciones
        )
        partes.append(f"Unidad {u.orden}: {u.nombre}\n{detalle}")
    return "\n".join(partes)


def _system_ejercicios(curso: Curso, contexto: str) -> str:
    return (
        f"Clasificas preguntas de examen teórico de conducción para el curso «{curso.nombre}» "
        f"(licencia {curso.codigo}, Chile).\n\nUnidades del curso:\n{contexto}\n\n"
        "Para CADA pregunta decide:\n"
        "- unidad: el número de la unidad cuyo contenido evalúa la pregunta, o null si la pregunta "
        "no corresponde a este curso (p. ej. transporte profesional de pasajeros o carga en un curso "
        "de licencia B) o no encaja en ninguna unidad.\n"
        "- categoria: exactamente una de esta lista cerrada (texto exacto):\n"
        + "\n".join(f"  - {c}" for c in _CATEGORIAS)
        + "\n- confianza: 0 a 1.\n- motivo: máximo 12 palabras.\n\n"
        'Responde SOLO JSON: {"<id>": {"unidad": 3, "categoria": "...", "confianza": 0.9, "motivo": "..."}}'
    )


def _render(ejercicios: list[Ejercicio]) -> str:
    out = []
    for e in ejercicios:
        ops = " | ".join(
            f"{k}) {getattr(e, f'opcion_{k}')}" for k in "abcdef" if getattr(e, f"opcion_{k}"))
        out.append(f"#{e.id}: {e.pregunta}\n   Opciones: {ops}")
    return "Clasifica estas preguntas:\n\n" + "\n\n".join(out)


def _llamar(cli: LLMClient, system: str, user: str, ids: list[str]) -> dict:
    """Una llamada con un reintento si faltan ids. Los errores del LLM cortan
    (no se clasifica a ciegas)."""
    ultimo: dict = {}
    for _ in range(2):
        raw = cli.complete(system=system, user=user, max_tokens=4000, temperature=0.0)
        try:
            # El modelo a veces repite el «#» del prompt en las claves ("#12").
            ultimo = {str(k).lstrip("#").strip(): v for k, v in parse_json_object(raw).items()}
        except Exception:
            ultimo = {}
        if all(i in ultimo for i in ids):
            return ultimo
    return ultimo


class Command(BaseCommand):
    help = "Clasifica ejercicios (y lecciones) en unidades/categorías de un curso; aplica el resultado."

    def add_arguments(self, parser):
        parser.add_argument("--curso", help="Código del curso (p. ej. B).")
        parser.add_argument("--out", help="JSON de salida (fase 1) o JSON del curso (fase 1b).")
        parser.add_argument("--model", default="", help="Modelo (default: draft/Haiku).")
        parser.add_argument("--limit", type=int, default=0, help="Solo los primeros N ejercicios (prueba).")
        parser.add_argument("--lecciones", action="store_true", help="Clasifica las lecciones del curso.")
        parser.add_argument("--course-json", help="JSON del curso a reescribir con --lecciones.")
        parser.add_argument("--aplicar", help="JSON de la fase 1 a aplicar en la BD.")
        parser.add_argument("--min-confianza", type=float, default=0.5)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **o):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
        if o["aplicar"]:
            return self._aplicar(Path(o["aplicar"]), o["min_confianza"], o["dry_run"])
        if not o["curso"] or not o["out"]:
            raise CommandError("Usa --curso y --out (o --aplicar <json>).")
        curso = Curso.objects.filter(codigo=o["curso"]).first()
        # --lecciones solo trabaja sobre el JSON del curso: no exige que esté en la BD.
        if curso is None and not o["lecciones"]:
            raise CommandError(f"No existe el curso {o['curso']!r}.")
        if not LLMClient.is_available():
            raise CommandError("Falta ANTHROPIC_API_KEY.")
        cli = LLMClient(model=o["model"] or draft_model())
        try:
            if o["lecciones"]:
                self._lecciones(curso, cli, o["course_json"], Path(o["out"]))
            else:
                self._ejercicios(curso, cli, Path(o["out"]), o["limit"])
        except LLMError as exc:
            raise CommandError(f"La IA no respondió ({exc}); no se escribió nada.") from exc
        self.stdout.write(f"Costo ~USD {cli.meter.cost_usd:.3f}")

    # -- fase 1: ejercicios ------------------------------------------------
    def _ejercicios(self, curso, cli, out: Path, limit: int):
        ya = set(EjercicioUnidad.objects.filter(unidad__curso=curso).values_list("ejercicio_id", flat=True))
        qs = list(Ejercicio.objects.exclude(id__in=ya).order_by("id"))
        if limit:
            qs = qs[:limit]
        # Solo unidades con quiz (un anexo sin evaluación no recibe ejercicios).
        ordenes = set(curso.unidades.filter(lecciones__tipo="quiz").values_list("orden", flat=True))
        system = _system_ejercicios(curso, contexto_curso(curso))
        self.stdout.write(f"Clasificando {len(qs)} ejercicios ({len(ya)} ya tienen unidad de {curso.codigo}) …")
        items = []
        for i in range(0, len(qs), _LOTE):
            lote = qs[i:i + _LOTE]
            ids = [str(e.id) for e in lote]
            res = _llamar(cli, system, _render(lote), ids)
            for e in lote:
                r = res.get(str(e.id)) or {}
                unidad = r.get("unidad")
                unidad = int(unidad) if isinstance(unidad, (int, float)) or str(unidad).isdigit() else None
                if unidad is not None and unidad not in ordenes:
                    unidad = None
                items.append({
                    "id_origen": e.id,
                    "clave": clave_de_fila(e),
                    "pregunta": e.pregunta,
                    "unidad_orden": unidad,
                    "categoria": taxonomy.resolve(r.get("categoria")) if r else None,
                    "confianza": float(r.get("confianza") or 0) if r else 0.0,
                    "motivo": r.get("motivo") or ("sin respuesta del modelo" if not r else ""),
                })
            self.stdout.write(f"  {min(i + _LOTE, len(qs))}/{len(qs)}")
        dist = Counter(it["unidad_orden"] for it in items)
        self.stdout.write("Por unidad: " + ", ".join(f"U{k}={v}" if k else f"sin unidad={v}" for k, v in sorted(dist.items(), key=lambda x: (x[0] is None, x[0] or 0))))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"curso": curso.codigo, "modelo": cli.model, "items": items},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Escrito {out} · {len(items)} ejercicios."))

    # -- fase 1b: lecciones ------------------------------------------------
    def _lecciones(self, curso, cli, course_json, out: Path):
        if not course_json:
            raise CommandError("--lecciones requiere --course-json (el JSON vigente del curso).")
        data = json.loads(Path(course_json).read_text(encoding="utf-8"))
        texto = [l for l in data["lessons"] if l.get("tipo") == "texto" and not l.get("anexo")]
        system = (
            "Asignas a cada lección de un curso teórico de conducción exactamente UNA categoría de esta "
            "lista cerrada (texto exacto):\n" + "\n".join(f"- {c}" for c in _CATEGORIAS)
            + '\n\nResponde SOLO JSON: {"<n>": "<categoría>"}'
        )
        user = "\n\n".join(
            f"#{i}: {l['nombre']} (unidad: {l.get('unidad_nombre')})\n   Puntos clave: {_puntos_clave(l.get('contenido'))}"
            for i, l in enumerate(texto))
        res = _llamar(cli, system, user, [str(i) for i in range(len(texto))])
        cambios = 0
        for i, l in enumerate(texto):
            nueva = taxonomy.resolve(res.get(str(i)))
            if nueva == taxonomy.FALLBACK and l.get("categoria"):
                continue
            if nueva != l.get("categoria"):
                self.stdout.write(f"  U{l['unidad_orden']}.{l['posicion']} {l['nombre']}: {l.get('categoria')} → {nueva}")
                l["categoria"] = nueva
                cambios += 1
        # Quiz y anexos: la categoría más frecuente de las lecciones de su unidad
        # (un anexo de señales, como el del curso B, va a «Señales de Tránsito»).
        for x in data["lessons"]:
            if x.get("tipo") == "quiz" or x.get("anexo"):
                if x.get("anexo") and "señal" in (x.get("nombre") or "").lower():
                    x["categoria"] = "Señales de Tránsito"
                    continue
                cats = Counter(l["categoria"] for l in texto if str(l["unidad_orden"]) == str(x["unidad_orden"]))
                if cats:
                    x["categoria"] = cats.most_common(1)[0][0]
        data.setdefault("revision_humana", []).append({
            "fase": "categorias", "fuente": f"clasificar_ejercicios --lecciones ({cli.model})",
            "aplicadas": cambios,
        })
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Escrito {out} · {cambios} lecciones con categoría nueva."))

    # -- fase 2: aplicar ---------------------------------------------------
    def _aplicar(self, path: Path, min_conf: float, dry: bool):
        data = json.loads(path.read_text(encoding="utf-8"))
        curso = Curso.objects.filter(codigo=data.get("curso")).first()
        if curso is None:
            raise CommandError(f"No existe el curso {data.get('curso')!r} en esta BD.")
        unidades = {u.orden: u for u in curso.unidades.all()}
        por_clave = {clave_de_fila(e): e for e in Ejercicio.objects.all()}
        categorias = {}
        c = Counter()
        with transaction.atomic():
            for it in data["items"]:
                e = por_clave.get(it["clave"])
                if e is None:
                    c["no_encontrado"] += 1
                    continue
                cat = it.get("categoria")
                if cat and cat in _CATEGORIAS:
                    if cat not in categorias:
                        categorias[cat], _ = Categoria.objects.get_or_create(
                            nombre=cat, defaults={"color_hex": taxonomy.color_for(cat)})
                    if e.categoria_id != categorias[cat].id:
                        e.categoria = categorias[cat]
                        e.save(update_fields=["categoria"])
                        c["categoria_actualizada"] += 1
                orden = it.get("unidad_orden")
                if orden is None:
                    c["sin_unidad"] += 1
                    continue
                if (it.get("confianza") or 0) < min_conf:
                    c["baja_confianza"] += 1
                    continue
                if orden not in unidades:
                    c["unidad_inexistente"] += 1
                    continue
                _, creado = EjercicioUnidad.objects.get_or_create(
                    ejercicio=e, unidad=unidades[orden],
                    defaults={"origen": "clasificacion", "confianza": it.get("confianza")})
                c["vinculo_creado" if creado else "vinculo_existente"] += 1
            if dry:
                transaction.set_rollback(True)
        for k, v in sorted(c.items()):
            self.stdout.write(f"  {'DRY-RUN ' if dry else ''}{k}: {v}")
        sin_json = Ejercicio.objects.exclude(vinculos_unidad__unidad__curso=curso).count()
        self.stdout.write(f"Ejercicios sin unidad de {curso.codigo} en esta BD: {sin_json}")
        self.stdout.write(self.style.SUCCESS("Dry-run listo." if dry else "Clasificación aplicada."))
