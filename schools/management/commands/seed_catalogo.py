"""Seed de catalogo: cursos, categorias, unidades, lecciones y ejercicios.

Genera contenido realista para 3 cursos (Clase B, Clase A1 motos, Clase A3
servicio publico), cada uno con varias unidades, lecciones de tipos mixtos
(video / audio / quiz / drag / identify) y ejercicios asociados.

Uso:
    python manage.py seed_catalogo
    python manage.py seed_catalogo --reset   # borra catalogo previo

Tras seedear, los estudiantes ya pueden:
  - Ver cursos disponibles en la tienda.
  - Navegar lecciones agrupadas por unidad.
  - Generar pruebas (rapida/completa/categoria) usando los ejercicios.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from schools.models import Curso, Categoria, Unidad, Leccion, Ejercicio, Glosario


# ============================================================
# Catalogo de datos
# ============================================================

CATEGORIAS = [
    ("Senales reglamentarias", "#dc2626"),
    ("Senales preventivas", "#f59e0b"),
    ("Senales informativas", "#2563eb"),
    ("Reglas de prioridad", "#10b981"),
    ("Estacionamiento", "#8b5cf6"),
    ("Velocidades y distancias", "#ec4899"),
    ("Conduccion defensiva", "#0891b2"),
    ("Mecanica basica", "#64748b"),
]


CURSOS = [
    {
        "nombre": "Licencia Clase B - Particular",
        "codigo": "CB",
        "descripcion": "Curso teorico completo para obtener la licencia clase B (vehiculos particulares hasta 3.500 kg).",
        "costo": 35000,
        "is_profesional": False,
    },
    {
        "nombre": "Licencia Clase A1 - Motos",
        "codigo": "A1",
        "descripcion": "Curso teorico para conduccion de motocicletas y motonetas hasta 200 cc.",
        "costo": 28000,
        "is_profesional": False,
    },
    {
        "nombre": "Licencia Clase A3 - Servicio Publico",
        "codigo": "A3",
        "descripcion": "Curso teorico profesional para transporte de pasajeros (taxis, colectivos, buses pequenos).",
        "costo": 75000,
        "is_profesional": True,
    },
]


# Para cada codigo de curso definimos sus unidades + lecciones + ejercicios.
# Estructura: codigo -> [ { unidad, orden, descripcion, lecciones: [...] } ]
# Leccion: { nombre, tipo, posicion, categoria, duracion_min, descripcion, contenido,
#            transcripcion, url_video, url_audio, url_pdf, ejercicios: [...] }
# Ejercicio: { pregunta, opcion_a..f, respuesta }

ESTRUCTURA = {
    "CB": [
        {
            "nombre": "Senales de transito",
            "orden": 1,
            "descripcion": "Reconocer e interpretar las senales reglamentarias, preventivas e informativas.",
            "lecciones": [
                {
                    "nombre": "Que son las senales reglamentarias",
                    "tipo": "video",
                    "posicion": 1,
                    "categoria": "Senales reglamentarias",
                    "duracion_min": 8,
                    "descripcion": "Aprende a identificar las senales que obligan o prohiben acciones.",
                    "contenido": "# Senales reglamentarias\n\nLas senales reglamentarias son las que **obligan, restringen o prohiben** determinada accion. Suelen ser de forma circular con borde rojo (prohibicion) o azul (obligacion).\n\n## Caracteristicas\n- Fondo blanco con simbolo y borde rojo: prohibicion\n- Fondo azul con simbolo blanco: obligacion\n- Octogono rojo: PARE\n- Triangulo invertido: CEDA EL PASO",
                    "transcripcion": "Hola, en esta leccion vamos a aprender que son las senales reglamentarias y como interpretarlas en la via publica...",
                    "url_video": "https://example.com/videos/cb-senales-reglamentarias.mp4",
                    "url_pdf": "https://example.com/pdfs/cb-senales-reglamentarias.pdf",
                    "ejercicios": [
                        {
                            "pregunta": "El octogono rojo con la palabra PARE obliga al conductor a:",
                            "opcion_a": "Disminuir la velocidad y mirar",
                            "opcion_b": "Detenerse completamente antes de la linea",
                            "opcion_c": "Continuar si no hay vehiculos",
                            "opcion_d": "Ceder el paso a la derecha",
                            "respuesta": "Detenerse completamente antes de la linea",
                        },
                        {
                            "pregunta": "Una senal circular con borde rojo y fondo blanco indica:",
                            "opcion_a": "Una obligacion",
                            "opcion_b": "Una prohibicion",
                            "opcion_c": "Una advertencia",
                            "opcion_d": "Una indicacion turistica",
                            "respuesta": "Una prohibicion",
                        },
                        {
                            "pregunta": "Cual es la forma del CEDA EL PASO?",
                            "opcion_a": "Triangulo equilatero con vertice arriba",
                            "opcion_b": "Triangulo invertido con vertice abajo",
                            "opcion_c": "Octogono",
                            "opcion_d": "Cuadrado",
                            "respuesta": "Triangulo invertido con vertice abajo",
                        },
                    ],
                },
                {
                    "nombre": "Senales preventivas",
                    "tipo": "video",
                    "posicion": 2,
                    "categoria": "Senales preventivas",
                    "duracion_min": 7,
                    "descripcion": "Senales amarillas que advierten peligros en la via.",
                    "contenido": "# Senales preventivas\n\nForma de **rombo** con fondo amarillo y simbolos negros. Advierten al conductor sobre situaciones potencialmente peligrosas.",
                    "transcripcion": "Las senales preventivas se reconocen por su forma de rombo amarillo...",
                    "url_video": "https://example.com/videos/cb-senales-preventivas.mp4",
                    "ejercicios": [
                        {
                            "pregunta": "El color de fondo de las senales preventivas en Chile es:",
                            "opcion_a": "Rojo",
                            "opcion_b": "Azul",
                            "opcion_c": "Amarillo",
                            "opcion_d": "Verde",
                            "respuesta": "Amarillo",
                        },
                        {
                            "pregunta": "Una senal preventiva con el dibujo de una curva indica:",
                            "opcion_a": "Calle cortada",
                            "opcion_b": "Curva peligrosa adelante",
                            "opcion_c": "Prohibido virar",
                            "opcion_d": "Estacionamiento permitido",
                            "respuesta": "Curva peligrosa adelante",
                        },
                    ],
                },
                {
                    "nombre": "Audio: senales informativas",
                    "tipo": "audio",
                    "posicion": 3,
                    "categoria": "Senales informativas",
                    "duracion_min": 6,
                    "descripcion": "Senales azules y verdes que entregan informacion util.",
                    "contenido": "Las senales informativas indican servicios, destinos y kilometrajes. Son rectangulares.",
                    "transcripcion": "Las senales informativas se distinguen por su forma rectangular...",
                    "url_audio": "https://example.com/audios/cb-senales-informativas.mp3",
                    "ejercicios": [
                        {
                            "pregunta": "Las senales que indican kilometros a una ciudad son de tipo:",
                            "opcion_a": "Reglamentaria",
                            "opcion_b": "Preventiva",
                            "opcion_c": "Informativa",
                            "opcion_d": "Provisoria",
                            "respuesta": "Informativa",
                        },
                    ],
                },
                {
                    "nombre": "Identifica la senal",
                    "tipo": "identify",
                    "posicion": 4,
                    "categoria": "Senales reglamentarias",
                    "duracion_min": 5,
                    "descripcion": "Ejercicio practico de reconocimiento visual.",
                    "contenido": "Mira la imagen y selecciona el significado correcto.",
                    "ejercicios": [
                        {
                            "pregunta": "Que significa una senal con la silueta de un peaton sobre fondo azul?",
                            "opcion_a": "Prohibido el paso de peatones",
                            "opcion_b": "Paso de peatones obligatorio",
                            "opcion_c": "Zona de escolares",
                            "opcion_d": "Velocidad maxima 30",
                            "respuesta": "Paso de peatones obligatorio",
                        },
                    ],
                },
                {
                    "nombre": "Quiz de senales",
                    "tipo": "quiz",
                    "posicion": 5,
                    "categoria": "Senales reglamentarias",
                    "duracion_min": 10,
                    "descripcion": "Pon a prueba lo aprendido sobre senales.",
                    "ejercicios": [
                        {
                            "pregunta": "Que debes hacer ante un CEDA EL PASO si no viene nadie?",
                            "opcion_a": "Detenerse igualmente",
                            "opcion_b": "Disminuir y continuar si es seguro",
                            "opcion_c": "Acelerar para pasar rapido",
                            "opcion_d": "Tocar la bocina",
                            "respuesta": "Disminuir y continuar si es seguro",
                        },
                    ],
                },
            ],
        },
        {
            "nombre": "Reglas de prioridad",
            "orden": 2,
            "descripcion": "Quien pasa primero en cruces, rotondas y situaciones especiales.",
            "lecciones": [
                {
                    "nombre": "Prioridad en cruces no senalizados",
                    "tipo": "video",
                    "posicion": 1,
                    "categoria": "Reglas de prioridad",
                    "duracion_min": 9,
                    "descripcion": "Quien tiene preferencia cuando no hay senales ni semaforo.",
                    "contenido": "# Prioridad en cruces\n\nEn cruces sin senalizacion, **el vehiculo de la derecha tiene la prioridad**. Esto es valido tambien para peatones que cruzan correctamente.",
                    "transcripcion": "Cuando llegas a un cruce sin senalizacion, debes ceder el paso al vehiculo que se aproxima por tu derecha...",
                    "url_video": "https://example.com/videos/cb-prioridad-cruces.mp4",
                    "ejercicios": [
                        {
                            "pregunta": "En un cruce sin senales, la prioridad es para el vehiculo que viene por la:",
                            "opcion_a": "Izquierda",
                            "opcion_b": "Derecha",
                            "opcion_c": "Calle mas ancha",
                            "opcion_d": "Direccion opuesta",
                            "respuesta": "Derecha",
                        },
                    ],
                },
                {
                    "nombre": "Rotondas",
                    "tipo": "video",
                    "posicion": 2,
                    "categoria": "Reglas de prioridad",
                    "duracion_min": 6,
                    "descripcion": "Como entrar y salir de una rotonda.",
                    "contenido": "En las rotondas la prioridad es de los vehiculos que ya estan circulando dentro.",
                    "url_video": "https://example.com/videos/cb-rotondas.mp4",
                    "ejercicios": [
                        {
                            "pregunta": "Al entrar a una rotonda debes:",
                            "opcion_a": "Tener prioridad sobre los que estan dentro",
                            "opcion_b": "Ceder el paso a los que estan dentro",
                            "opcion_c": "Acelerar para entrar rapido",
                            "opcion_d": "Detenerte siempre",
                            "respuesta": "Ceder el paso a los que estan dentro",
                        },
                    ],
                },
                {
                    "nombre": "Ordena la frase: prioridad",
                    "tipo": "drag",
                    "posicion": 3,
                    "categoria": "Reglas de prioridad",
                    "duracion_min": 4,
                    "descripcion": "Reordena los conceptos de prioridad en un cruce.",
                    "ejercicios": [
                        {
                            "pregunta": "Cual es el orden correcto de prioridad en una via?",
                            "opcion_a": "Peatones, vehiculos de emergencia, vehiculos comunes",
                            "opcion_b": "Vehiculos de emergencia, peatones, vehiculos comunes",
                            "opcion_c": "Vehiculos comunes, peatones, emergencia",
                            "opcion_d": "Todos a la vez",
                            "respuesta": "Vehiculos de emergencia, peatones, vehiculos comunes",
                        },
                    ],
                },
                {
                    "nombre": "Quiz prioridades",
                    "tipo": "quiz",
                    "posicion": 4,
                    "categoria": "Reglas de prioridad",
                    "duracion_min": 8,
                    "descripcion": "Evaluacion de prioridades.",
                    "ejercicios": [
                        {
                            "pregunta": "Una ambulancia con sirena se aproxima por detras. Debes:",
                            "opcion_a": "Acelerar para alejarte",
                            "opcion_b": "Detenerte en el carril del medio",
                            "opcion_c": "Hacerte a un lado y detenerte si es posible",
                            "opcion_d": "Mantener tu velocidad",
                            "respuesta": "Hacerte a un lado y detenerte si es posible",
                        },
                    ],
                },
            ],
        },
        {
            "nombre": "Velocidades y maniobras",
            "orden": 3,
            "descripcion": "Limites de velocidad, estacionamiento y maniobras seguras.",
            "lecciones": [
                {
                    "nombre": "Limites de velocidad urbanos y rurales",
                    "tipo": "video",
                    "posicion": 1,
                    "categoria": "Velocidades y distancias",
                    "duracion_min": 7,
                    "descripcion": "Cuanto se puede ir en cada tipo de via.",
                    "contenido": "# Limites de velocidad\n\n- **Zona urbana**: 50 km/h\n- **Zona escolar**: 30 km/h\n- **Caminos rurales**: 100 km/h\n- **Autopistas**: 120 km/h",
                    "url_video": "https://example.com/videos/cb-velocidades.mp4",
                    "ejercicios": [
                        {
                            "pregunta": "La velocidad maxima en zona urbana en Chile es de:",
                            "opcion_a": "30 km/h",
                            "opcion_b": "50 km/h",
                            "opcion_c": "80 km/h",
                            "opcion_d": "100 km/h",
                            "respuesta": "50 km/h",
                        },
                        {
                            "pregunta": "La velocidad maxima en zonas escolares es:",
                            "opcion_a": "30 km/h",
                            "opcion_b": "50 km/h",
                            "opcion_c": "20 km/h",
                            "opcion_d": "40 km/h",
                            "respuesta": "30 km/h",
                        },
                    ],
                },
                {
                    "nombre": "Estacionamiento permitido y prohibido",
                    "tipo": "audio",
                    "posicion": 2,
                    "categoria": "Estacionamiento",
                    "duracion_min": 5,
                    "descripcion": "Donde puedes y donde no puedes estacionar.",
                    "url_audio": "https://example.com/audios/cb-estacionamiento.mp3",
                    "ejercicios": [
                        {
                            "pregunta": "Cual de los siguientes lugares NO esta permitido estacionar?",
                            "opcion_a": "Frente a tu casa",
                            "opcion_b": "Sobre la vereda",
                            "opcion_c": "En zonas con tarifa",
                            "opcion_d": "En sectores reservados a residentes",
                            "respuesta": "Sobre la vereda",
                        },
                    ],
                },
                {
                    "nombre": "Conduccion defensiva",
                    "tipo": "video",
                    "posicion": 3,
                    "categoria": "Conduccion defensiva",
                    "duracion_min": 12,
                    "descripcion": "Mantener distancias seguras y anticipar riesgos.",
                    "contenido": "La conduccion defensiva consiste en anticipar las acciones de otros y mantener un margen seguro.",
                    "url_video": "https://example.com/videos/cb-defensiva.mp4",
                    "ejercicios": [
                        {
                            "pregunta": "Cual es la distancia recomendada con el vehiculo de adelante?",
                            "opcion_a": "1 metro",
                            "opcion_b": "Regla de los 2 segundos",
                            "opcion_c": "10 metros",
                            "opcion_d": "Tan cerca como se pueda",
                            "respuesta": "Regla de los 2 segundos",
                        },
                    ],
                },
            ],
        },
    ],
    "A1": [
        {
            "nombre": "Equipamiento y revision moto",
            "orden": 1,
            "descripcion": "Casco, espejos, neumaticos y luces antes de salir.",
            "lecciones": [
                {
                    "nombre": "Casco y proteccion personal",
                    "tipo": "video",
                    "posicion": 1,
                    "categoria": "Mecanica basica",
                    "duracion_min": 6,
                    "descripcion": "El casco es obligatorio. Como elegir uno apropiado.",
                    "contenido": "# Casco\n\nEl casco debe estar certificado y abrocharse siempre.",
                    "url_video": "https://example.com/videos/a1-casco.mp4",
                    "ejercicios": [
                        {
                            "pregunta": "El casco para motocicleta en Chile:",
                            "opcion_a": "Es opcional en zonas rurales",
                            "opcion_b": "Es obligatorio siempre",
                            "opcion_c": "Solo en autopistas",
                            "opcion_d": "Solo para el conductor",
                            "respuesta": "Es obligatorio siempre",
                        },
                    ],
                },
                {
                    "nombre": "Inspeccion previa de la moto",
                    "tipo": "audio",
                    "posicion": 2,
                    "categoria": "Mecanica basica",
                    "duracion_min": 4,
                    "descripcion": "Que revisar antes de cada viaje.",
                    "url_audio": "https://example.com/audios/a1-inspeccion.mp3",
                    "ejercicios": [
                        {
                            "pregunta": "Que NO forma parte de la inspeccion basica de la moto?",
                            "opcion_a": "Presion de neumaticos",
                            "opcion_b": "Funcionamiento de luces",
                            "opcion_c": "Color de la pintura",
                            "opcion_d": "Nivel de aceite",
                            "respuesta": "Color de la pintura",
                        },
                    ],
                },
            ],
        },
        {
            "nombre": "Conduccion en ciudad",
            "orden": 2,
            "descripcion": "Tecnica y precauciones en trafico denso.",
            "lecciones": [
                {
                    "nombre": "Visibilidad y angulos ciegos",
                    "tipo": "video",
                    "posicion": 1,
                    "categoria": "Conduccion defensiva",
                    "duracion_min": 7,
                    "descripcion": "Como ser visto y evitar puntos ciegos.",
                    "url_video": "https://example.com/videos/a1-visibilidad.mp4",
                    "ejercicios": [
                        {
                            "pregunta": "Para ser mas visible en moto debes:",
                            "opcion_a": "Usar ropa oscura",
                            "opcion_b": "Apagar las luces de dia",
                            "opcion_c": "Usar ropa reflectante y luces encendidas",
                            "opcion_d": "Circular muy cerca de los autos",
                            "respuesta": "Usar ropa reflectante y luces encendidas",
                        },
                    ],
                },
                {
                    "nombre": "Quiz urbano motos",
                    "tipo": "quiz",
                    "posicion": 2,
                    "categoria": "Conduccion defensiva",
                    "duracion_min": 10,
                    "descripcion": "Repaso de situaciones reales en ciudad.",
                    "ejercicios": [
                        {
                            "pregunta": "Puedes circular entre dos filas de autos detenidos?",
                            "opcion_a": "Si, siempre",
                            "opcion_b": "Solo en autopistas",
                            "opcion_c": "No esta permitido salvo carriles habilitados",
                            "opcion_d": "Solo de noche",
                            "respuesta": "No esta permitido salvo carriles habilitados",
                        },
                    ],
                },
                {
                    "nombre": "Identifica la maniobra",
                    "tipo": "identify",
                    "posicion": 3,
                    "categoria": "Conduccion defensiva",
                    "duracion_min": 5,
                    "descripcion": "Reconoce maniobras seguras vs inseguras.",
                    "ejercicios": [
                        {
                            "pregunta": "Frenar fuerte solo con el freno delantero en mojado es:",
                            "opcion_a": "Recomendable, frena mas",
                            "opcion_b": "Seguro si vas lento",
                            "opcion_c": "Peligroso, puede provocar caida",
                            "opcion_d": "Igual que en seco",
                            "respuesta": "Peligroso, puede provocar caida",
                        },
                    ],
                },
            ],
        },
    ],
    "A3": [
        {
            "nombre": "Transporte de pasajeros",
            "orden": 1,
            "descripcion": "Responsabilidad y normativa especifica para servicio publico.",
            "lecciones": [
                {
                    "nombre": "Marco legal del transporte publico",
                    "tipo": "video",
                    "posicion": 1,
                    "categoria": "Senales reglamentarias",
                    "duracion_min": 14,
                    "descripcion": "Leyes y reglamentos que aplican a taxis y colectivos.",
                    "contenido": "# Marco legal\n\nLa Ley 18.290 regula el transporte de pasajeros.",
                    "url_video": "https://example.com/videos/a3-marco-legal.mp4",
                    "url_pdf": "https://example.com/pdfs/ley-18290.pdf",
                    "ejercicios": [
                        {
                            "pregunta": "La capacidad maxima de pasajeros en un vehiculo de servicio publico la define:",
                            "opcion_a": "El conductor",
                            "opcion_b": "El padron y la autorizacion del Ministerio",
                            "opcion_c": "El cliente",
                            "opcion_d": "El sindicato",
                            "respuesta": "El padron y la autorizacion del Ministerio",
                        },
                    ],
                },
                {
                    "nombre": "Conducta y atencion al pasajero",
                    "tipo": "audio",
                    "posicion": 2,
                    "categoria": "Conduccion defensiva",
                    "duracion_min": 7,
                    "descripcion": "Como tratar al pasajero y resolver conflictos.",
                    "url_audio": "https://example.com/audios/a3-atencion.mp3",
                    "ejercicios": [
                        {
                            "pregunta": "Ante una queja de un pasajero, lo correcto es:",
                            "opcion_a": "Discutir hasta convencerlo",
                            "opcion_b": "Ignorar y seguir manejando",
                            "opcion_c": "Escuchar con respeto y resolver con calma",
                            "opcion_d": "Bajarlo del vehiculo",
                            "respuesta": "Escuchar con respeto y resolver con calma",
                        },
                    ],
                },
            ],
        },
        {
            "nombre": "Conduccion en ruta y autopista",
            "orden": 2,
            "descripcion": "Velocidades, descansos y prevencion de fatiga.",
            "lecciones": [
                {
                    "nombre": "Limites en autopista para servicio publico",
                    "tipo": "video",
                    "posicion": 1,
                    "categoria": "Velocidades y distancias",
                    "duracion_min": 9,
                    "descripcion": "Velocidades maximas en transporte profesional.",
                    "url_video": "https://example.com/videos/a3-autopista.mp4",
                    "ejercicios": [
                        {
                            "pregunta": "La velocidad maxima en autopista para vehiculos de transporte de pasajeros es:",
                            "opcion_a": "120 km/h",
                            "opcion_b": "100 km/h",
                            "opcion_c": "80 km/h",
                            "opcion_d": "140 km/h",
                            "respuesta": "100 km/h",
                        },
                    ],
                },
                {
                    "nombre": "Quiz profesional",
                    "tipo": "quiz",
                    "posicion": 2,
                    "categoria": "Conduccion defensiva",
                    "duracion_min": 12,
                    "descripcion": "Evaluacion de conocimientos profesionales.",
                    "ejercicios": [
                        {
                            "pregunta": "Conducir mas de cuantas horas continuas exige descanso obligatorio?",
                            "opcion_a": "2 horas",
                            "opcion_b": "5 horas",
                            "opcion_c": "8 horas",
                            "opcion_d": "12 horas",
                            "respuesta": "5 horas",
                        },
                    ],
                },
            ],
        },
    ],
}


# ============================================================
# Ejercicios extra: pool grande, sin leccion vinculada.
# Sirven al endpoint de "prueba completa" (necesita ~35 ejercicios) y al
# tipo='categoria' que filtra por Categoria.
# Cada item: (curso_codigo, categoria, pregunta, opcion_a..f, respuesta)
# ============================================================
EJERCICIOS_EXTRA = [
    # ---- CB: senales reglamentarias ----
    ("CB", "Senales reglamentarias",
     "Una senal circular azul con flecha indica:",
     "Prohibido seguir esa direccion", "Sentido obligatorio", "Curva pronunciada",
     "Zona de estacionamiento", None, None,
     "Sentido obligatorio"),
    ("CB", "Senales reglamentarias",
     "Que indica una senal con un auto cruzado por una linea diagonal roja?",
     "Estacionamiento permitido", "Velocidad maxima", "Prohibido el paso de vehiculos motorizados",
     "Zona escolar", None, None,
     "Prohibido el paso de vehiculos motorizados"),

    # ---- CB: senales preventivas ----
    ("CB", "Senales preventivas",
     "Un rombo amarillo con dibujo de ninos jugando indica:",
     "Zona escolar - precaucion", "Parque infantil cerrado", "Prohibido el paso a menores",
     "Hospital cercano", None, None,
     "Zona escolar - precaucion"),
    ("CB", "Senales preventivas",
     "El simbolo de un trabajador con pala sobre fondo amarillo significa:",
     "Carretera cortada", "Trabajos en la via", "Acceso a obra prohibido",
     "Punto de inspeccion", None, None,
     "Trabajos en la via"),

    # ---- CB: senales informativas ----
    ("CB", "Senales informativas",
     "Una senal rectangular verde con flecha y nombre de ciudad indica:",
     "Salida de emergencia", "Direccion y destino", "Velocidad recomendada",
     "Zona militar", None, None,
     "Direccion y destino"),

    # ---- CB: velocidades y distancias ----
    ("CB", "Velocidades y distancias",
     "La distancia minima recomendada al estacionar antes de una esquina es:",
     "1 metro", "5 metros", "10 metros",
     "20 metros", None, None,
     "10 metros"),
    ("CB", "Velocidades y distancias",
     "Si la via esta mojada, la distancia de frenado:",
     "Se mantiene igual", "Disminuye", "Aumenta",
     "Depende del color del auto", None, None,
     "Aumenta"),
    ("CB", "Velocidades y distancias",
     "En autopista, la velocidad maxima para vehiculos particulares es:",
     "100 km/h", "120 km/h", "140 km/h",
     "80 km/h", None, None,
     "120 km/h"),

    # ---- CB: reglas de prioridad ----
    ("CB", "Reglas de prioridad",
     "En un cruce con semaforo apagado debes:",
     "Pasar normalmente", "Aplicar la regla de derecha", "Acelerar para cruzar",
     "Detenerse y esperar 1 minuto", None, None,
     "Aplicar la regla de derecha"),
    ("CB", "Reglas de prioridad",
     "Quien tiene prioridad en una via principal sobre una secundaria?",
     "El que viene de la secundaria", "El que viene de la principal", "El mas rapido",
     "El que llega primero", None, None,
     "El que viene de la principal"),

    # ---- CB: estacionamiento ----
    ("CB", "Estacionamiento",
     "Esta permitido estacionar frente a una salida de emergencia?",
     "Si, siempre", "Solo de dia", "No, esta prohibido",
     "Solo si pagas tarifa", None, None,
     "No, esta prohibido"),
    ("CB", "Estacionamiento",
     "Al estacionar en pendiente bajando con la vereda a la derecha, las ruedas delanteras deben quedar:",
     "Rectas", "Giradas hacia la izquierda", "Giradas hacia la derecha (vereda)",
     "Levantadas", None, None,
     "Giradas hacia la derecha (vereda)"),

    # ---- CB: conduccion defensiva ----
    ("CB", "Conduccion defensiva",
     "Si ves un pelota cruzando la calle, debes esperar que:",
     "Nada, solo continuar", "Un nino aparezca persiguiendola", "El viento la mueva",
     "Otro auto la pise", None, None,
     "Un nino aparezca persiguiendola"),
    ("CB", "Conduccion defensiva",
     "Ante una situacion de fatiga al conducir, lo correcto es:",
     "Aumentar la velocidad para llegar antes", "Detenerse a descansar",
     "Beber alcohol para relajarse", "Subir la musica para mantenerse despierto",
     None, None,
     "Detenerse a descansar"),

    # ---- A1: mecanica basica motos ----
    ("A1", "Mecanica basica",
     "Cual es la presion correcta para los neumaticos de una moto?",
     "La que indica el manual del fabricante", "Lo mas alta posible",
     "Lo mas baja para mejor agarre", "No importa la presion",
     None, None,
     "La que indica el manual del fabricante"),
]


GLOSARIO = [
    ("PARE", "Senal octogonal roja que obliga a detencion completa antes de la linea de detencion."),
    ("CEDA EL PASO", "Senal triangular invertida que indica preferencia para otros vehiculos."),
    ("Conduccion defensiva", "Estilo de conduccion que anticipa errores propios y de terceros para evitar accidentes."),
    ("Punto ciego", "Zona alrededor del vehiculo que no se ve por los espejos."),
    ("Distancia de seguridad", "Espacio minimo necesario para frenar sin colision; se mide en segundos, no en metros."),
    ("Reglamentaria", "Senal que obliga o prohibe (forma circular)."),
    ("Preventiva", "Senal que advierte un peligro (rombo amarillo)."),
    ("Informativa", "Senal que entrega informacion util (rectangular)."),
]


# ============================================================
# Command
# ============================================================

class Command(BaseCommand):
    help = "Pobla cursos, categorias, unidades, lecciones y ejercicios con datos realistas."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Borra catalogo previo (cursos/categorias/unidades/lecciones/ejercicios/glosario).")

    @transaction.atomic
    def handle(self, *args, **opts):
        if opts.get("reset"):
            self._reset()

        # 1. Categorias
        categorias = {}
        for nombre, color in CATEGORIAS:
            cat, created = Categoria.objects.get_or_create(
                nombre=nombre,
                defaults={"color_hex": color},
            )
            categorias[nombre] = cat
            tag = "creada" if created else "ya existia"
            self.stdout.write(f"  Categoria: {nombre} ({tag})")

        # 2. Glosario
        for termino, significado in GLOSARIO:
            Glosario.objects.get_or_create(termino=termino, defaults={"significado": significado})
        self.stdout.write(f"  Glosario: {len(GLOSARIO)} terminos OK")

        # 3. Cursos
        cursos_por_codigo = {}
        for spec in CURSOS:
            curso, created = Curso.objects.get_or_create(
                codigo=spec["codigo"],
                defaults={
                    "nombre": spec["nombre"],
                    "descripcion": spec["descripcion"],
                    "costo": spec["costo"],
                    "is_profesional": spec["is_profesional"],
                    "url_image": "http://placeholder.url",
                    "url_icon": "http://placeholder.url",
                },
            )
            cursos_por_codigo[spec["codigo"]] = curso
            tag = "creado" if created else "ya existia"
            self.stdout.write(f"  Curso: [{spec['codigo']}] {spec['nombre']} ({tag})")

        # 4. Unidades + lecciones + ejercicios
        total_lecciones = 0
        total_ejercicios = 0
        for codigo, unidades in ESTRUCTURA.items():
            curso = cursos_por_codigo[codigo]
            for u in unidades:
                unidad, _ = Unidad.objects.get_or_create(
                    curso=curso,
                    orden=u["orden"],
                    defaults={"nombre": u["nombre"], "descripcion": u["descripcion"]},
                )
                self.stdout.write(f"    Unidad: [{codigo}] U{u['orden']} - {u['nombre']}")

                for lspec in u["lecciones"]:
                    categoria = categorias[lspec["categoria"]]
                    leccion, lcreated = Leccion.objects.get_or_create(
                        curso=curso,
                        unidad=unidad,
                        nombre=lspec["nombre"],
                        defaults={
                            "categoria": categoria,
                            "posicion": lspec["posicion"],
                            "tipo": lspec["tipo"],
                            "descripcion": lspec.get("descripcion", ""),
                            "contenido": lspec.get("contenido", ""),
                            "transcripcion": lspec.get("transcripcion", ""),
                            "duracion_min": lspec.get("duracion_min", 0),
                            "url_video": lspec.get("url_video", "http://placeholder.url"),
                            "url_audio": lspec.get("url_audio", "http://placeholder.url"),
                            "url_pdf": lspec.get("url_pdf", ""),
                        },
                    )
                    if lcreated:
                        total_lecciones += 1

                    for espec in lspec.get("ejercicios", []):
                        if Ejercicio.objects.filter(
                            curso=curso, leccion=leccion, pregunta=espec["pregunta"]
                        ).exists():
                            continue
                        Ejercicio.objects.create(
                            categoria=categoria,
                            curso=curso,
                            leccion=leccion,
                            pregunta=espec["pregunta"],
                            opcion_a=espec.get("opcion_a", ""),
                            opcion_b=espec.get("opcion_b", ""),
                            opcion_c=espec.get("opcion_c", ""),
                            opcion_d=espec.get("opcion_d", ""),
                            opcion_e=espec.get("opcion_e", ""),
                            opcion_f=espec.get("opcion_f", ""),
                            respuesta=espec["respuesta"],
                        )
                        total_ejercicios += 1

        # 5. Ejercicios extra (pool grande para prueba completa)
        extra_creados = 0
        for codigo_curso, cat_nombre, pregunta, a, b, c, d, e, f, respuesta in EJERCICIOS_EXTRA:
            curso = cursos_por_codigo[codigo_curso]
            categoria = categorias[cat_nombre]
            if Ejercicio.objects.filter(curso=curso, leccion__isnull=True, pregunta=pregunta).exists():
                continue
            Ejercicio.objects.create(
                categoria=categoria,
                curso=curso,
                leccion=None,
                pregunta=pregunta,
                opcion_a=a or "",
                opcion_b=b or "",
                opcion_c=c or "",
                opcion_d=d or "",
                opcion_e=e or "",
                opcion_f=f or "",
                respuesta=respuesta,
            )
            extra_creados += 1
        total_ejercicios += extra_creados

        self.stdout.write(self.style.SUCCESS(
            f"\nSeed catalogo OK: {len(CATEGORIAS)} categorias, "
            f"{len(CURSOS)} cursos, "
            f"{Unidad.objects.count()} unidades totales, "
            f"+{total_lecciones} lecciones nuevas, "
            f"+{total_ejercicios} ejercicios nuevos "
            f"(de los cuales {extra_creados} son del pool extra)."
        ))

    def _reset(self):
        deleted = []
        deleted.append(("Ejercicios", Ejercicio.objects.all().delete()[0]))
        deleted.append(("Lecciones", Leccion.objects.all().delete()[0]))
        deleted.append(("Unidades", Unidad.objects.all().delete()[0]))
        deleted.append(("Cursos", Curso.objects.filter(codigo__in=[c["codigo"] for c in CURSOS]).delete()[0]))
        deleted.append(("Categorias", Categoria.objects.filter(nombre__in=[c[0] for c in CATEGORIAS]).delete()[0]))
        deleted.append(("Glosario", Glosario.objects.filter(termino__in=[g[0] for g in GLOSARIO]).delete()[0]))
        for label, n in deleted:
            self.stdout.write(self.style.WARNING(f"  Reset {label}: {n} borrados"))
