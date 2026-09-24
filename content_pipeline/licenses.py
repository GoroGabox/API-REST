"""Orientación del curso según la licencia de conducir objetivo.

Los cursos profesionales A1–A5 se generan a partir del MISMO libro fuente (el
manual del conductor profesional cubre todas las clases). Sin una orientación
explícita, el generador produce un curso genérico y el auditor no puede
distinguir si un curso —aunque su contenido sea correcto— está mal orientado
(p. ej. un A4 de carga con énfasis en transporte de pasajeros).

Aquí se define, por código de licencia, una descripción cualitativa del ámbito
que se inyecta en los prompts de estructura y de redacción para ENFOCAR el curso
hacia esa licencia. Es cualitativa a propósito (tipo de vehículo y operación),
sin cifras legales exactas; el operador puede sobrescribirla con el texto
autoritativo vía ``--orientacion`` / parámetro ``orientacion``.
"""
from __future__ import annotations

# Ámbito por código (Chile). Cualitativo: qué conduce y qué operación realiza.
ORIENTATION: dict[str, str] = {
    "A1": "licencia profesional para el transporte remunerado de pasajeros en taxis.",
    "A2": "licencia profesional para el transporte remunerado de pasajeros en vehículos "
          "menores como taxis y transporte escolar.",
    "A3": "licencia profesional para el transporte público de pasajeros en buses y "
          "vehículos de mayor capacidad (incluye lo de A2).",
    "A4": "licencia profesional para el transporte de carga en camiones simples.",
    "A5": "licencia profesional para el transporte de carga en vehículos articulados "
          "(camión con remolque o semirremolque; incluye lo de A4).",
    "B": "licencia no profesional para vehículos particulares livianos "
         "(automóviles y camionetas).",
    "C": "licencia no profesional para motocicletas y vehículos motorizados de dos "
         "o tres ruedas.",
}


def orientation_for(codigo: str, override: str | None = None) -> str | None:
    """Descripción de orientación para la licencia. ``override`` gana si viene.

    Devuelve None si no hay orientación conocida ni override (el prompt entonces
    no agrega la sección de orientación).
    """
    if override and override.strip():
        return override.strip()
    return ORIENTATION.get((codigo or "").strip().upper())
