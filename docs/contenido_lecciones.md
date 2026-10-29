# Contenido de lecciones: Markdown y componentes

Contrato del texto de `Leccion.contenido` (lecciones de lectura). Lo producen el redactor del pipeline y el editor en la Brújula; lo muestran la webapp (`autotest_website`), la app (`AutoTestExpoApp`) y la Brújula.

**Implementación de referencia:** `content_pipeline/processors/content_blocks.py`.
**Port en JS:** `content_pipeline/review/contenido_md.js`. La Brújula lo embebe; la webapp y la app lo copian.
**Casos compartidos:** `content_pipeline/review/fixtures_contenido.json`. Cada renderizador debe producir exactamente esos bloques y tokens. Si cambias el contrato, regenera los fixtures con `python -m content_pipeline.review.fixtures_contenido` y actualiza los cuatro renderizadores. `tests/test_content_blocks.py` verifica Python y JS contra los fixtures, y que la Brújula embeba una copia idéntica de `contenido_md.js`.

## Bloques

| Bloque | Sintaxis | Modelo |
|---|---|---|
| Sección | `# Título`, `## Desarrollo` | `heading {level, text}`. Los clientes agrupan por `#`/`##`. |
| Subtítulo | `### Texto` | `heading {level: 3, text}` |
| Párrafo | líneas seguidas; una línea en blanco separa | `paragraph {text}`; las líneas se unen con un espacio |
| Lista | `- `, `* `, `+ ` o `1.` / `1)`; una línea indentada continúa el ítem | `list {ordered, items}`; sin anidar |
| Figura | `{{figura:F-xxxx}}` en su propia línea | `figura {id}`; se resuelve con el `LeccionRecurso` de esa `clave` |
| Recuadro | ver abajo | `callout {tipo, titulo, blocks}` |
| Tabla | ver abajo | `table {titulo, align, header, rows}` |
| Cita | `> texto`, sin etiqueta | `quote {blocks}` |
| Código | entre ```` ``` ```` | `code {text}` |
| Separador | `---` | `hr` |

## Texto de línea

`**negrita**` o `__negrita__`, `*cursiva*` o `_cursiva_`, `` `código` `` y `[texto](https://…)` (solo http/https).

Los tokens son `text | strong | em | code | link` y no se anidan. Un `_` dentro de una palabra (`snake_case`) no abre cursiva.

## Recuadros (alerta estilo GitHub)

```md
> [!DEBES_SABER] Título opcional
> Cuerpo del recuadro, con **negritas** y viñetas:
> - Uno.
> - Dos.
```

- La etiqueta va en la primera línea de la cita. El tipo no distingue mayúsculas.
- El texto que sigue a la etiqueta es el título; sin título, se usa el rótulo del tipo.
- El cuerpo admite párrafos, listas y figuras; **no** admite recuadros ni tablas.
- Un renderizador sin soporte lo muestra como una cita normal: el contenido no se pierde.

| Tipo | Rótulo | Cuándo (solo si el libro lo dice) | Color claro / oscuro | Ícono sugerido |
|---|---|---|---|---|
| `NORMA` | Norma y sanción | Obligaciones legales, prohibiciones, multas, suspensiones | `#C0392B` / `#E57368` | mazo / balanza |
| `ATENCION` | Atención | Riesgos y advertencias de seguridad | `#B7791F` / `#E6B45E` | alerta |
| `DEBES_SABER` | Debes saber | Datos centrales que se evalúan; recuadros «DEBES SABER» del libro | `#005776` / `#5BB2D6` | libro / estrella |
| `DATO` | Dato | Cifras, estadísticas, definiciones | franja `#E8D7BD`, fondo `#F7EFDE` / `#3A3226`, ícono en tinta | gráfico |
| `CONSEJO` | Consejo | Recomendaciones prácticas | `#2E7D57` / `#5EC48F` | foco |

**Estilo** (identidad AutoTest):
- franja izquierda de 5 px del color del tipo;
- fondo con el color al 10-14 %;
- burbuja con ícono;
- rótulo o título en Switzer Semibold, mayúsculas espaciadas;
- cuerpo en Switzer Regular color tinta (`#1C2833`, o `#F1F5F9` en oscuro);
- radio 18;
- sin sombra en modo oscuro.

El color nunca va solo: siempre lleva ícono y rótulo. Un tipo desconocido se muestra como cita.

## Tablas (GFM)

```md
Tabla: Estadísticas en Chile
| Indicador | Valor |
|---|---:|
| Siniestros al año | 82.000 |
```

- Toda fila empieza con `|`. La segunda línea separa el encabezado y marca la alineación (`:---` izquierda, `:---:` centro, `---:` derecha).
- `Tabla: …`, en la línea inmediatamente anterior, es el título (opcional).
- `\|` es una barra literal dentro de una celda. Las celdas admiten texto de línea.
- Máximo 6 columnas. En pantallas angostas la tabla se desplaza horizontalmente.
- Estilo: encabezado en beige `#F7EFDE` (en oscuro `#1E2730`), Switzer Semibold, filas alternadas y bordes `#D8C7A8` (en oscuro `#26323C`).

## Dónde van

Los recuadros y las tablas van solo en las secciones de lectura: **Desarrollo, Introducción, Aplicación práctica y Ejemplo aplicado**. Se recomiendan hasta 4 recuadros por lección (el redactor usa hasta 3).

## Validación (`validate_components`, dentro de `validate_generated_course`)

- **Errores** (bloquean `import_course`): tipo de recuadro desconocido; recuadro vacío, anidado o con una tabla adentro; tabla sin filas, con más de 6 columnas o con filas de otro ancho.
- **Advertencias:** componentes fuera de las secciones de lectura, o más de 4 recuadros.

## Texto plano (`strip_components`)

El guion de audio, el juez y la auditoría de cifras y anclaje reciben el texto sin sintaxis de componentes, a través de `figure_markers.strip_markers`:
- los recuadros quedan como su título más el cuerpo;
- cada fila de tabla queda como una frase «Encabezado: valor; …» (sus cifras se siguen auditando).
