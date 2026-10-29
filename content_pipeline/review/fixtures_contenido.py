"""Regenera content_pipeline/review/fixtures_contenido.json desde content_blocks.py.

Uso (desde la raíz del repo): python -m content_pipeline.review.fixtures_contenido
Correr al cambiar el contrato (docs/contenido_lecciones.md) y actualizar los renderizadores.
"""
import json

from content_pipeline.processors.content_blocks import parse_blocks, parse_inline, strip_components

BLOQUES = {
    "parrafo_multilinea": "Primera línea\nsigue el párrafo.\n\nOtro párrafo.",
    "titulos": "# Título\n\n## Desarrollo\n\n### Subtítulo\nTexto.",
    "listas": "- uno\n- dos\n  continúa\n\n1. primero\n2) segundo",
    "recuadro_simple": "> [!DEBES_SABER]\n> Toda infracción gravísima conlleva multa de 1,5 a 3 UTM.",
    "recuadro_titulo_y_lista": "> [!NORMA] Ley Emilia\n> Sanciona con cárcel efectiva.\n>\n> - Reincidencia.\n> - Licencia cancelada.",
    "recuadro_minusculas": "> [!consejo]\n> Mira lejos.",
    "recuadro_tipo_desconocido": "> [!RARO]\n> Texto.",
    "cita_normal": "> Una cita sin etiqueta.\n> Sigue.",
    "tabla_con_titulo": ("Tabla: Estadísticas en Chile\n| Indicador | Valor |\n|---|---:|\n"
                         "| Siniestros al año | 82.000 |\n| Fallecidos | 1.600 |"),
    "tabla_alineaciones_y_barra": "| A | B | C |\n|:---|:---:|---|\n| x \\| y | **z** | 1 |",
    "tabla_separador_corto": "Texto previo\n| A | B |\n|:-|-:|\n| 1 | 2 |",
    "tabla_sin_barras_externas": "Col 1 | Col 2\n--- | ---\nno es tabla",
    "figura": "Texto.\n\n{{figura:F-abc123}}\n\nMás texto.",
    "codigo_y_separador": "```\nliteral **no**\n```\n\n---\n\nFin.",
    "leccion_completa": (
        "# Suspensión de licencia\n\n## Desarrollo\n\nLa licencia se suspende en estos casos:\n\n"
        "Tabla: Suspensión de licencia\n| Infracción | Suspensión |\n|---|---|\n"
        "| Bajo la influencia del alcohol | 3 meses |\n| Estado de ebriedad | 2 años |\n\n"
        "> [!ATENCION]\n> Estos períodos aumentan en caso de reincidencia.\n\n"
        "## Puntos clave\n\n- Las sanciones aumentan por reincidencia.\n"
    ),
}
LINEA = {
    "negrita_cursiva": "Hola **negrita**, __otra__, *cursiva* y _también_.",
    "codigo_y_enlace": "Usa `ABS` y visita [CONASET](https://www.conaset.cl).",
    "guion_bajo_en_palabra": "snake_case_var no es cursiva.",
    "enlace_no_http": "[local](/ruta) queda como texto.",
}
STRIP = {
    "recuadro": "> [!NORMA] Ley Emilia\n> Cárcel efectiva.",
    "tabla": "Tabla: Datos\n| Indicador | Valor |\n|---|---|\n| Siniestros | 82.000 |",
}

data = {
    "version": 1,
    "descripcion": ("Casos del contrato de contenido de lecciones (docs/contenido_lecciones.md). "
                    "Generados con content_pipeline/processors/content_blocks.py; cada renderizador "
                    "(webapp, app Expo, Brújula) debe producir los mismos bloques y tokens."),
    "bloques": [{"nombre": k, "md": v, "esperado": parse_blocks(v)} for k, v in BLOQUES.items()],
    "linea": [{"nombre": k, "texto": v, "esperado": parse_inline(v)} for k, v in LINEA.items()],
    "texto_plano": [{"nombre": k, "md": v, "esperado": strip_components(v)} for k, v in STRIP.items()],
}
with open("content_pipeline/review/fixtures_contenido.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")
print(len(data["bloques"]), len(data["linea"]), len(data["texto_plano"]))
