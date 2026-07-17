from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Usuario
from schools.models import Categoria, Curso, Leccion, Unidad


class LessonApiOrderingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = Usuario.objects.create_user(
            email="student@example.com",
            nombre="Ada",
            apellido="Lovelace",
            password="secret123",
            is_estudiante=True,
        )
        self.user.is_active = True
        self.user.save(update_fields=["is_active"])
        self.client.force_authenticate(self.user)

        self.curso = Curso.objects.create(
            nombre="Curso Profesional Clase A2",
            codigo="A2T",
            descripcion="Curso de prueba",
            is_profesional=True,
        )
        self.categoria = Categoria.objects.create(nombre="Legislacion")
        self.unidad_1 = Unidad.objects.create(curso=self.curso, nombre="Unidad 1", orden=1)
        self.unidad_2 = Unidad.objects.create(curso=self.curso, nombre="Unidad 2", orden=2)

        # Creacion deliberadamente desordenada por ID para probar el order_by real.
        self.u2_p1 = self._lesson(self.unidad_2, 1, "U2 P1", "Contenido U2 P1")
        self.u1_p2 = self._lesson(self.unidad_1, 2, "U1 P2", "Contenido U1 P2")
        self.u1_p1 = self._lesson(self.unidad_1, 1, "U1 P1", "Contenido U1 P1")
        self.u2_p2 = self._lesson(self.unidad_2, 2, "U2 P2", "Contenido U2 P2")

        # El contenido premium está gateado por acceso al curso: damos acceso
        # vigente al estudiante para poder verificar orden y forma del contenido.
        from django.utils import timezone
        from datetime import timedelta
        from sales.models import AccessKey, EstudianteCurso
        ak = AccessKey.objects.create(
            valid_until=timezone.now() + timedelta(days=30), origen="purchase", status="active",
        )
        EstudianteCurso.objects.create(
            estudiante_id=self.user, curso_id=self.curso, access_key_id=ak,
        )

    def _lesson(self, unidad, posicion, nombre, contenido):
        return Leccion.objects.create(
            curso=self.curso,
            unidad=unidad,
            categoria=self.categoria,
            nombre=nombre,
            posicion=posicion,
            tipo="texto",
            descripcion=f"Descripcion {nombre}",
            contenido=contenido,
            transcripcion=f"Transcripcion {nombre}",
            duracion_min=10,
        )

    def test_course_lessons_are_unpaginated_ordered_and_include_content(self):
        response = self.client.get(f"/api/v1/schools/lessons/?curso={self.curso.id}", secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.data, list)
        self.assertNotIn("results", response.data if isinstance(response.data, dict) else {})
        self.assertEqual(
            [lesson["id"] for lesson in response.data],
            [self.u1_p1.id, self.u1_p2.id, self.u2_p1.id, self.u2_p2.id],
        )
        self.assertEqual(response.data[0]["contenido"], "Contenido U1 P1")
        self.assertEqual(response.data[0]["transcripcion"], "Transcripcion U1 P1")
        self.assertEqual(response.data[0]["unidad"], self.unidad_1.id)
        self.assertEqual(response.data[0]["unidad_orden"], 1)
        self.assertEqual(response.data[0]["unidad_nombre"], "Unidad 1")

    def test_course_units_embed_lessons_ordered_by_position(self):
        response = self.client.get(f"/api/v1/schools/courses/{self.curso.id}/units/", secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual([unit["id"] for unit in response.data], [self.unidad_1.id, self.unidad_2.id])
        self.assertEqual(
            [lesson["id"] for lesson in response.data[0]["lecciones"]],
            [self.u1_p1.id, self.u1_p2.id],
        )
        self.assertEqual(
            [lesson["id"] for lesson in response.data[1]["lecciones"]],
            [self.u2_p1.id, self.u2_p2.id],
        )