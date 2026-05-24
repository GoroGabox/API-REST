from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Usuario, Prueba, PruebaEjercicio
from schools.models import Categoria, Ejercicio, Escuela


def make_user(email, **kwargs):
    """Factory: crea usuario activo con rol opcional."""
    is_admin_flag = kwargs.pop('is_admin', False)
    u = Usuario.objects.create_user(
        email=email,
        nombre=kwargs.pop('nombre', 'N'),
        apellido=kwargs.pop('apellido', 'A'),
        password="Abcdef12!@#",
        **kwargs,
    )
    if is_admin_flag:
        u.is_staff = True
        u.is_superuser = True
    u.is_active = True
    u.save()
    return u


class RegistroPublicoTests(APITestCase):
    def test_no_permite_autoelevar_a_director(self):
        url = reverse('user_register_view')
        payload = {
            "nombre": "X", "apellido": "Y", "email": "x@y.com",
            "password": "Abcdef12!@#", "password2": "Abcdef12!@#",
            "is_director": True, "is_active": True,
        }
        r = self.client.post(url, payload, format='json')
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        user = Usuario.objects.get(email="x@y.com")
        self.assertFalse(user.is_director)
        self.assertTrue(user.is_estudiante)
        self.assertFalse(user.is_active)
        self.assertIsNotNone(user.activation_token)

    def test_passwords_distintas_falla(self):
        url = reverse('user_register_view')
        payload = {
            "nombre": "X", "apellido": "Y", "email": "z@y.com",
            "password": "Abcdef12!@#", "password2": "otraDistinta!1",
        }
        r = self.client.post(url, payload, format='json')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)


class ActivacionTokenTests(APITestCase):
    def test_activacion_marca_usuario_activo_y_consume_token(self):
        user = Usuario.objects.create_user(
            email="a@b.com", nombre="A", apellido="B",
            password="Abcdef12!@#", is_estudiante=True,
        )
        user.is_active = False
        user.save(update_fields=['is_active'])
        token = user.activation_token

        r = self.client.get(reverse('activate_account', args=[str(token)]))
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertIsNone(user.activation_token)


class GenerarPruebaServiceTests(APITestCase):
    def setUp(self):
        self.user = make_user("s@t.com", is_estudiante=True)
        cat = Categoria.objects.create(nombre="Cat1")
        for i in range(15):
            Ejercicio.objects.create(categoria=cat, pregunta=f"q{i}", respuesta="a")

    def test_genera_prueba_rapida_no_expone_respuesta(self):
        self.client.force_authenticate(self.user)
        r = self.client.post(reverse('generate_test'), {"tipo": "rapida"}, format='json')
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(r.data["total"], 10)
        for q in r.data["preguntas"]:
            self.assertNotIn("respuesta", q)
        self.assertEqual(PruebaEjercicio.objects.filter(prueba_id=r.data["prueba_id"]).count(), 10)

    def test_gratis_no_persiste_prueba(self):
        prev = Prueba.objects.count()
        r = self.client.post(reverse('generate_free_test'), {"tipo": "rapida"}, format='json')
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(Prueba.objects.count(), prev)


class RegistrarEscuelaDirectorTests(APITestCase):
    """Solo admin puede crear el par escuela+director."""

    def test_admin_crea_escuela_y_director(self):
        admin = make_user("admin@a.com", is_admin=True)
        self.client.force_authenticate(admin)
        payload = {
            "escuela_nombre": "AutoEscuela X",
            "escuela_direccion": "Av 1",
            "escuela_email": "ax@ax.com",
            "escuela_telefono": "+56999999999",
            "director_nombre": "Diego",
            "director_apellido": "Rector",
            "director_email": "diego@ax.com",
            "director_password": "Abcdef12!@#",
        }
        r = self.client.post(reverse('registrar_escuela_director'), payload, format='json')
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)

        escuela = Escuela.objects.get(email="ax@ax.com")
        director = Usuario.objects.get(email="diego@ax.com")
        self.assertTrue(director.is_director)
        self.assertFalse(director.is_estudiante)
        self.assertTrue(director.is_active)
        self.assertEqual(director.escuela_id, escuela.id)

    def test_director_no_puede_registrar(self):
        escuela = Escuela.objects.create(nombre="E", direccion="d", email="e@e.com", telefono="1")
        director = make_user("d@d.com", is_director=True, escuela=escuela)
        self.client.force_authenticate(director)
        r = self.client.post(reverse('registrar_escuela_director'), {}, format='json')
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonimo_no_puede_registrar(self):
        r = self.client.post(reverse('registrar_escuela_director'), {}, format='json')
        self.assertIn(r.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))


class UsuarioListScopingTests(APITestCase):
    def setUp(self):
        self.escuela_a = Escuela.objects.create(nombre="A", direccion="x", email="a@a.com", telefono="1")
        self.escuela_b = Escuela.objects.create(nombre="B", direccion="y", email="b@b.com", telefono="2")
        self.admin = make_user("admin@a.com", is_admin=True)
        self.dir_a = make_user("dira@a.com", is_director=True, escuela=self.escuela_a)
        self.dir_b = make_user("dirb@b.com", is_director=True, escuela=self.escuela_b)
        self.est_a = make_user("esta@a.com", is_estudiante=True, escuela=self.escuela_a)
        self.est_b = make_user("estb@b.com", is_estudiante=True, escuela=self.escuela_b)

    def _list_emails(self, response):
        data = response.data['results'] if isinstance(response.data, dict) and 'results' in response.data else response.data
        return {u['email'] for u in data}

    def test_admin_ve_todos(self):
        self.client.force_authenticate(self.admin)
        r = self.client.get(reverse('list_users_view'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self._list_emails(r)), 5)

    def test_director_solo_ve_estudiantes_de_su_escuela(self):
        self.client.force_authenticate(self.dir_a)
        r = self.client.get(reverse('list_users_view'))
        self.assertEqual(r.status_code, 200)
        emails = self._list_emails(r)
        self.assertIn(self.est_a.email, emails)
        self.assertNotIn(self.est_b.email, emails)
        self.assertNotIn(self.dir_b.email, emails)

    def test_estudiante_no_ve_nada(self):
        self.client.force_authenticate(self.est_a)
        r = self.client.get(reverse('list_users_view'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._list_emails(r), set())


class SubmitPruebaTests(APITestCase):
    """Submit de respuestas + emisión automática de certificado."""

    def setUp(self):
        from schools.models import Categoria, Curso, Ejercicio
        self.user = make_user("submitter@x.com", is_estudiante=True)
        self.client.force_authenticate(self.user)
        self.curso = Curso.objects.create(nombre="Conducción A", descripcion="d")
        cat = Categoria.objects.create(nombre="C")
        # 40 ejercicios (cubre tipo='completa' que requiere 35); respuesta correcta = opcion_a.
        self.ejercicios = []
        for i in range(40):
            e = Ejercicio.objects.create(
                categoria=cat, curso=self.curso, pregunta=f"q{i}",
                opcion_a="OK", opcion_b="MAL", opcion_c="MAL", opcion_d="MAL",
                respuesta="OK",
            )
            self.ejercicios.append(e)

    def _generar_prueba(self, modalidad='practica', tipo='rapida'):
        payload = {"tipo": tipo, "modalidad": modalidad, "curso_id": self.curso.id}
        r = self.client.post(reverse('generate_test'), payload, format='json')
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        return r.data['prueba_id'], r.data['preguntas']

    def test_submit_todas_correctas_aprueba(self):
        prueba_id, preguntas = self._generar_prueba()
        respuestas = {str(p['id']): 'a' for p in preguntas}
        r = self.client.post(reverse('submit_test', args=[prueba_id]),
                             {"respuestas": respuestas}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data['aprobado'])
        self.assertEqual(r.data['score'], 100.0)
        self.assertEqual(r.data['total_correctas'], 10)

    def test_submit_no_emite_cert_en_modalidad_practica(self):
        prueba_id, preguntas = self._generar_prueba(modalidad='practica', tipo='completa')
        respuestas = {str(p['id']): 'a' for p in preguntas}
        r = self.client.post(reverse('submit_test', args=[prueba_id]),
                             {"respuestas": respuestas}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('certificado', r.data)

    def test_submit_evaluacion_completa_aprobada_emite_cert(self):
        prueba_id, preguntas = self._generar_prueba(modalidad='evaluacion', tipo='completa')
        respuestas = {str(p['id']): 'a' for p in preguntas}
        r = self.client.post(reverse('submit_test', args=[prueba_id]),
                             {"respuestas": respuestas}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn('certificado', r.data)
        from accounts.models import Certificado
        self.assertEqual(Certificado.objects.filter(estudiante=self.user, curso=self.curso).count(), 1)

    def test_submit_doble_no_duplica_cert(self):
        # Una segunda Prueba aprobada sobre el mismo curso no crea otro cert.
        prueba_id, preguntas = self._generar_prueba(modalidad='evaluacion', tipo='completa')
        respuestas = {str(p['id']): 'a' for p in preguntas}
        self.client.post(reverse('submit_test', args=[prueba_id]),
                         {"respuestas": respuestas}, format='json')

        prueba_id2, preguntas2 = self._generar_prueba(modalidad='evaluacion', tipo='completa')
        respuestas2 = {str(p['id']): 'a' for p in preguntas2}
        self.client.post(reverse('submit_test', args=[prueba_id2]),
                         {"respuestas": respuestas2}, format='json')

        from accounts.models import Certificado
        self.assertEqual(Certificado.objects.filter(estudiante=self.user, curso=self.curso).count(), 1)

    def test_submit_no_se_puede_reenviar(self):
        prueba_id, preguntas = self._generar_prueba()
        respuestas = {str(p['id']): 'a' for p in preguntas}
        self.client.post(reverse('submit_test', args=[prueba_id]),
                         {"respuestas": respuestas}, format='json')
        r = self.client.post(reverse('submit_test', args=[prueba_id]),
                             {"respuestas": respuestas}, format='json')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_estudiante_no_puede_submitear_prueba_ajena(self):
        prueba_id, preguntas = self._generar_prueba()
        otro = make_user("o@x.com", is_estudiante=True)
        self.client.force_authenticate(otro)
        r = self.client.post(reverse('submit_test', args=[prueba_id]),
                             {"respuestas": {}}, format='json')
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)


class CertificadoFlowTests(APITestCase):
    def setUp(self):
        from schools.models import Curso
        from accounts.models import Certificado, Prueba
        self.user = make_user("cert@x.com", is_estudiante=True)
        self.curso = Curso.objects.create(nombre="C", descripcion="d")
        self.prueba = Prueba.objects.create(
            estudiante=self.user, curso=self.curso,
            tipo='completa', modalidad='evaluacion', aprobado=True,
        )
        self.cert = Certificado.objects.get(estudiante=self.user, curso=self.curso)

    def test_listar_mis_certificados(self):
        self.client.force_authenticate(self.user)
        r = self.client.get(reverse('mis_certificados'))
        self.assertEqual(r.status_code, 200)
        data = r.data['results'] if isinstance(r.data, dict) and 'results' in r.data else r.data
        self.assertEqual(len(data), 1)

    def test_verify_codigo_publico(self):
        # Sin autenticar.
        r = self.client.get(reverse('verify_certificado', args=[str(self.cert.codigo)]))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data['valid'])
        self.assertEqual(r.data['curso_nombre'], "C")

    def test_verify_codigo_invalido(self):
        import uuid as _uuid
        r = self.client.get(reverse('verify_certificado', args=[str(_uuid.uuid4())]))
        self.assertEqual(r.status_code, 404)


class ProgresoEstudiantesScopingTests(APITestCase):
    def setUp(self):
        self.escuela_a = Escuela.objects.create(nombre="A", direccion="x", email="a@a.com", telefono="1")
        self.escuela_b = Escuela.objects.create(nombre="B", direccion="y", email="b@b.com", telefono="2")
        self.dir_a = make_user("dira@a.com", is_director=True, escuela=self.escuela_a)
        self.dir_b = make_user("dirb@b.com", is_director=True, escuela=self.escuela_b)
        self.estudiante = make_user("e@e.com", is_estudiante=True, escuela=self.escuela_a)

    def test_director_otro_colegio_obtiene_403(self):
        self.client.force_authenticate(self.dir_b)
        r = self.client.get(reverse('progreso-estudiantes', args=[self.escuela_a.id]))
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_director_de_la_escuela_pasa(self):
        self.client.force_authenticate(self.dir_a)
        r = self.client.get(reverse('progreso-estudiantes', args=[self.escuela_a.id]))
        self.assertEqual(r.status_code, status.HTTP_200_OK)

    def test_estudiante_obtiene_403(self):
        self.client.force_authenticate(self.estudiante)
        r = self.client.get(reverse('progreso-estudiantes', args=[self.escuela_a.id]))
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
