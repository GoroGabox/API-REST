from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Usuario
from schools.models import Categoria, Ejercicio


class EjercicioSerializerTests(APITestCase):
    """El serializer público de Ejercicio NO debe exponer la respuesta correcta."""

    def setUp(self):
        self.user = Usuario.objects.create_user(
            email="r@s.com", nombre="R", apellido="S",
            password="Abcdef12!@#", is_estudiante=True,
        )
        self.user.is_active = True
        self.user.save(update_fields=['is_active'])
        self.client.force_authenticate(self.user)
        cat = Categoria.objects.create(nombre="C")
        Ejercicio.objects.create(categoria=cat, pregunta="¿2+2?", respuesta="4", opcion_a="4")

    def test_lista_ejercicios_oculta_respuesta(self):
        r = self.client.get('/api/v1/schools/exercices/')
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        data = r.data['results'] if isinstance(r.data, dict) and 'results' in r.data else r.data
        for ej in data:
            self.assertNotIn('respuesta', ej)
