"""Taxonomía canónica ÚNICA de categorías (lecciones de curso + banco de exámenes).

Antes cada curso inventaba su categoría (= nombre del módulo) y el banco de
exámenes usaba su propia lista granular: resultado, decenas de filas
casi-duplicadas (mismo concepto, distinto casing/acento/redacción) y lecciones
que quedaban sin categoría al borrar los duplicados (FK ``on_delete=SET_NULL``).

Aquí se define UNA lista cerrada compartida por todo el pipeline:
  - el generador de cursos clasifica cada unidad en una de estas etiquetas
    (``manifest_llm`` / ``manifest_builder``),
  - el clasificador del banco de exámenes clasifica cada pregunta en la misma
    lista (``ejercicio_classifier``),
  - el importador deduplica por CLAVE CANÓNICA (sin acentos, sin casing) y
    asigna el color oficial (``exporters.django_importer``).

Cualquier nombre libre que no encaje cae a ``FALLBACK`` ("General"), de modo que
ninguna lección/ejercicio queda sin categoría y no se crean etiquetas nuevas.
"""
from __future__ import annotations

import unicodedata

# (nombre canónico, color_hex). El orden es el de presentación en el frontend.
CATEGORIES: list[tuple[str, str]] = [
    ("Legislación y Normativa de Tránsito", "#dc2626"),
    ("Señales de Tránsito", "#f59e0b"),
    ("Conducción Defensiva", "#0891b2"),
    ("Velocidad y Distancias de Seguridad", "#ec4899"),
    ("Prioridad y Derecho de Paso", "#10b981"),
    ("Adelantamiento y Maniobras", "#3b82f6"),
    ("Estacionamiento y Detención", "#8b5cf6"),
    ("Alcohol, Drogas y Fatiga", "#b91c1c"),
    ("Condiciones Ambientales y Visibilidad", "#0d9488"),
    ("Mecánica y Mantención del Vehículo", "#64748b"),
    ("Primeros Auxilios", "#e11d48"),
    ("Prevención de Riesgos e Incendios", "#ea580c"),
    ("Aspectos Psicológicos y Relaciones Humanas", "#7c3aed"),
    ("Transporte Profesional (Carga y Pasajeros)", "#475569"),
]

FALLBACK = "General"
FALLBACK_COLOR = "#545050"

CATEGORY_NAMES: list[str] = [name for name, _ in CATEGORIES]
_COLOR_BY_NAME: dict[str, str] = {name: color for name, color in CATEGORIES}
_COLOR_BY_NAME[FALLBACK] = FALLBACK_COLOR


def canonical_key(name: str) -> str:
    """Clave de comparación: sin acentos, sin casing, espacios colapsados."""
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.split()).casefold()


# Alias -> categoría canónica. Absorbe las variantes históricas de los cursos
# A/B/C y las del banco de exámenes Clase B. Se comparan por ``canonical_key``.
_ALIASES: dict[str, str] = {}


def _register(canonical: str, *variants: str) -> None:
    _ALIASES[canonical_key(canonical)] = canonical
    for variant in variants:
        _ALIASES[canonical_key(variant)] = canonical


# Cada canónica se auto-mapea; luego se agregan sus variantes conocidas.
_register(FALLBACK)
_register(
    "Legislación y Normativa de Tránsito",
    "Legislación de Tránsito",
    "Legislación",
    "Normativa vigente sobre el uso de infraestructura vial",
    "Infraestructura vial",
    "Normativa, documentación y seguridad",
)
_register(
    "Señales de Tránsito",
    "Señales de tránsito",
    "Señales reglamentarias",
    "Señales preventivas",
    "Señales informativas",
    "Senales reglamentarias",
    "Senales preventivas",
    "Senales informativas",
)
_register(
    "Conducción Defensiva",
    "Conducción defensiva y riesgos",
    "Conduccion defensiva",
    "Introducción a la seguridad vial y el vehículo",
    "Principios de la conducción segura",
)
_register(
    "Velocidad y Distancias de Seguridad",
    "Velocidad y distancias",
    "Velocidades y distancias",
)
_register(
    "Prioridad y Derecho de Paso",
    "Prioridad y derecho de paso",
    "Reglas de prioridad",
)
_register("Adelantamiento y Maniobras", "Adelantamiento y maniobras")
_register(
    "Estacionamiento y Detención",
    "Estacionamiento y detención",
    "Estacionamiento",
)
_register(
    "Alcohol, Drogas y Fatiga",
    "Alcohol, drogas y estado del conductor",
)
_register(
    "Condiciones Ambientales y Visibilidad",
    "Condiciones ambientales y visibilidad",
)
_register(
    "Mecánica y Mantención del Vehículo",
    "Mecánica y mantención",
    "Mecánica y Mantención Preventiva",
    "Mecánica y mantención preventiva",
    "Mecánica",
    "Mecanica basica",
)
_register("Primeros Auxilios", "Primeros auxilios")
_register(
    "Prevención de Riesgos e Incendios",
    "Prevención de Riesgos y Combate de Incendios",
    "Prevención de riesgos",
)
_register(
    "Aspectos Psicológicos y Relaciones Humanas",
    "Aspectos Psicológicos del Conductor, Relaciones Humanas y Comunicación",
    "Psicología del conductor",
)
_register(
    "Transporte Profesional (Carga y Pasajeros)",
    "Reglamentación aplicada al transporte de pasajeros",
    "Reglamentación aplicada al transporte de carga",
    "Transporte de carga",
    "Transporte de pasajeros",
)


def resolve(name: str) -> str:
    """Mapea un nombre libre a la categoría canónica; ``FALLBACK`` si no encaja."""
    if not name or not str(name).strip():
        return FALLBACK
    return _ALIASES.get(canonical_key(name), FALLBACK)


def color_for(name: str) -> str:
    """Color oficial de una categoría canónica (gris del fallback si se desconoce)."""
    return _COLOR_BY_NAME.get(name, FALLBACK_COLOR)
