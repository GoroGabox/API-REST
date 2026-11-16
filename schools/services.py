"""Lógica de catálogo que cruza modelos (mismo patrón que ``sales/services.py``)."""
from __future__ import annotations

from .models import Leccion, LeccionRecurso

PLACEHOLDER_URL = "http://placeholder.url"

# Espejo de compatibilidad: campo de Leccion ← (tipo de recurso, roles en orden de preferencia).
_ESPEJO = {
    "url_audio": ("audio", ("narracion", "principal", "descarga")),
    "url_video": ("video", ("principal", "descarga")),
    "url_pdf": ("pdf", ("descarga", "paginas_libro", "principal")),
}
_VACIO = {"url_audio": PLACEHOLDER_URL, "url_video": PLACEHOLDER_URL, "url_pdf": ""}


def _elegir(recursos: list[LeccionRecurso], tipo: str, roles: tuple[str, ...]) -> LeccionRecurso | None:
    del_tipo = [r for r in recursos if r.tipo == tipo and r.url]
    for rol in roles:
        for r in del_tipo:
            if r.rol == rol:
                return r
    return del_tipo[0] if del_tipo else None


def sync_media_mirror(leccion: Leccion, *, url_quitada: str | None = None) -> dict[str, str]:
    """Rellena ``url_audio/url_video/url_pdf`` desde los recursos de la lección.

    Solo escribe un campo si hay un recurso de ese tipo (no pisa URLs puestas a mano en
    el admin). Si se borró el recurso cuya URL estaba en el espejo (``url_quitada``),
    el campo vuelve a su valor vacío. Devuelve los cambios aplicados.
    """
    recursos = list(leccion.recursos.all())
    cambios: dict[str, str] = {}
    for campo, (tipo, roles) in _ESPEJO.items():
        actual = getattr(leccion, campo) or ""
        elegido = _elegir(recursos, tipo, roles)
        if elegido is not None:
            if actual != elegido.url:
                cambios[campo] = elegido.url
        elif url_quitada and actual == url_quitada:
            cambios[campo] = _VACIO[campo]
    if cambios:
        Leccion.objects.filter(pk=leccion.pk).update(**cambios)
        for campo, valor in cambios.items():
            setattr(leccion, campo, valor)
    return cambios


# ---------------------------------------------------------------------------
# Banco de ejercicios por unidad (quiz de unidad)
# ---------------------------------------------------------------------------

def ejercicios_de_unidad(unidad, categoria_id=None):
    """Ejercicios del banco vinculados a la unidad (M2M ``Ejercicio.unidades``).

    Sin las de selección múltiple: el quiz de lección de los clientes es de una
    sola respuesta (siguen disponibles en el Gimnasio y el examen final)."""
    from .models import Ejercicio
    qs = Ejercicio.objects.filter(unidades=unidad, multiple=False)
    if categoria_id:
        qs = qs.filter(categoria_id=categoria_id)
    return qs.distinct()


def grupos_de_ejercicios(qs) -> dict[str, list[int]]:
    """``{clave_grupo: [ids]}``; sin ``grupo``, cada ejercicio es su propio grupo."""
    grupos: dict[str, list[int]] = {}
    for row in qs.values("id", "grupo"):
        grupos.setdefault(row["grupo"] or f"id:{row['id']}", []).append(row["id"])
    return grupos


def quiz_desde_banco(leccion, categoria_id=None) -> bool:
    """True si el quiz de ``leccion`` se arma desde el banco de su unidad
    (al menos ``PREGUNTAS_QUIZ_UNIDAD`` grupos de variantes)."""
    from .models import PREGUNTAS_QUIZ_UNIDAD
    if getattr(leccion, "tipo", None) != "quiz" or not leccion.unidad_id:
        return False
    return len(grupos_de_ejercicios(ejercicios_de_unidad(leccion.unidad, categoria_id))) >= PREGUNTAS_QUIZ_UNIDAD
