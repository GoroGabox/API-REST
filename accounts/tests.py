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


class GamificationTests(APITestCase):
    """Hearts/energía/XP/streak/logros."""

    def setUp(self):
        from schools.models import Categoria, Curso, Ejercicio
        self.user = make_user("g@x.com", is_estudiante=True)
        self.client.force_authenticate(self.user)
        self.curso = Curso.objects.create(nombre="Conducir", descripcion="d")
        cat = Categoria.objects.create(nombre="C")
        for i in range(40):
            Ejercicio.objects.create(
                categoria=cat, curso=self.curso, pregunta=f"q{i}",
                opcion_a="OK", opcion_b="MAL", opcion_c="MAL", opcion_d="MAL",
                respuesta="OK",
            )

    def _generar(self, modalidad='practica', tipo='rapida'):
        r = self.client.post(reverse('generate_test'), {
            "tipo": tipo, "modalidad": modalidad, "curso_id": self.curso.id,
        }, format='json')
        return r

    def _submit(self, prueba_id, preguntas, respuesta='a'):
        respuestas = {str(p['id']): respuesta for p in preguntas}
        return self.client.post(reverse('submit_test', args=[prueba_id]),
                                {"respuestas": respuestas}, format='json')

    def test_iniciar_prueba_no_consume_recursos(self):
        """Energy removida del modelo: iniciar no consume nada."""
        from accounts.gamification import MAX_HEARTS
        hearts_before = self.user.hearts
        r = self._generar(modalidad='practica')
        self.assertEqual(r.status_code, 201, r.data)
        self.user.refresh_from_db()
        # Sin energy: lo que importa es que hearts no cambien y el modelo
        # ya no expone `energy`.
        self.assertEqual(self.user.hearts, hearts_before)
        self.assertFalse(hasattr(self.user, 'energy'))

    def test_evaluacion_completa_resta_corazones_por_incorrecta(self):
        from accounts.gamification import MAX_HEARTS
        # Para tipo='completa' necesitamos 35+ ejercicios. Creamos extras.
        from schools.models import Ejercicio, Categoria
        cat = Categoria.objects.first()
        for i in range(40):
            Ejercicio.objects.create(
                categoria=cat, curso=self.curso, pregunta=f"extra-q{i}",
                opcion_a="OK", opcion_b="MAL", opcion_c="MAL", opcion_d="MAL",
                respuesta="OK",
            )
        r = self._generar(modalidad='evaluacion', tipo='completa')
        preguntas = r.data['preguntas']
        # Responder todo incorrecto (b en vez de a)
        r2 = self._submit(r.data['prueba_id'], preguntas, respuesta='b')
        self.assertEqual(r2.status_code, 200, r2.data)
        # 35 incorrectas -> hearts saturado en 0
        self.user.refresh_from_db()
        self.assertEqual(self.user.hearts, 0)
        self.assertEqual(r2.data['corazones_restantes'], 0)

    def test_practica_no_resta_corazones(self):
        from accounts.gamification import MAX_HEARTS
        r = self._generar(modalidad='practica')
        preguntas = r.data['preguntas']
        self._submit(r.data['prueba_id'], preguntas, respuesta='b')
        self.user.refresh_from_db()
        self.assertEqual(self.user.hearts, MAX_HEARTS)

    def test_evaluacion_rapida_no_resta_corazones(self):
        """Regla nueva: prueba rapida nunca consume vidas."""
        from accounts.gamification import MAX_HEARTS
        r = self._generar(modalidad='evaluacion', tipo='rapida')
        preguntas = r.data['preguntas']
        self._submit(r.data['prueba_id'], preguntas, respuesta='b')
        self.user.refresh_from_db()
        self.assertEqual(self.user.hearts, MAX_HEARTS)

    def test_xp_por_correcta_evaluacion(self):
        from accounts.gamification import XP_POR_CORRECTA_EVALUACION, XP_BONUS_APROBAR_EVALUACION
        r = self._generar(modalidad='evaluacion', tipo='rapida')
        preguntas = r.data['preguntas']
        r2 = self._submit(r.data['prueba_id'], preguntas, respuesta='a')
        self.user.refresh_from_db()
        esperado = 10 * XP_POR_CORRECTA_EVALUACION + XP_BONUS_APROBAR_EVALUACION
        self.assertEqual(self.user.xp, esperado)
        self.assertEqual(r2.data['xp_ganado'], esperado)

    def test_streak_se_actualiza_en_primera_submit(self):
        r = self._generar()
        self._submit(r.data['prueba_id'], r.data['preguntas'])
        self.user.refresh_from_db()
        self.assertEqual(self.user.streak_current, 1)

    def test_logro_pleno_y_primera_aprobacion(self):
        r = self._generar(modalidad='evaluacion', tipo='rapida')
        r2 = self._submit(r.data['prueba_id'], r.data['preguntas'], respuesta='a')
        nuevos = r2.data['logros_nuevos']
        self.assertIn('primera-aprobacion', nuevos)
        self.assertIn('pleno', nuevos)


class MeEndpointsTests(APITestCase):
    def setUp(self):
        self.user = make_user("me@x.com", is_estudiante=True)
        self.client.force_authenticate(self.user)

    def test_get_me_devuelve_perfil_y_stats(self):
        r = self.client.get(reverse('me_profile'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['email'], self.user.email)
        self.assertIn('stats', r.data)
        self.assertIn('hearts', r.data['stats'])
        self.assertIn('level', r.data['stats'])

    def test_patch_me_actualiza_campos_de_perfil(self):
        r = self.client.patch(reverse('me_profile'), {
            'rut': '12.345.678-9', 'telefono': '+56911111111', 'direccion': 'Av X',
        }, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.rut, '12.345.678-9')

    def test_patch_me_no_permite_autoelevar_a_admin(self):
        r = self.client.patch(reverse('me_profile'), {
            'is_staff': True, 'is_superuser': True, 'is_director': True,
        }, format='json')
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_director)

    def test_get_me_stats(self):
        r = self.client.get(reverse('me_stats'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['hearts'], 5)
        # energy fue removido del modelo, no debe aparecer en la respuesta.
        self.assertNotIn('energy', r.data)
        self.assertNotIn('max_energy', r.data)
        self.assertEqual(r.data['xp'], 0)

    def test_achievements_lista_catalogo_con_earned_false(self):
        r = self.client.get(reverse('me_achievements'))
        self.assertEqual(r.status_code, 200)
        slugs = {a['slug'] for a in r.data}
        self.assertIn('racha-7', slugs)
        self.assertIn('pleno', slugs)
        for a in r.data:
            self.assertFalse(a['earned'])


class LeccionDetalleYUnidadesTests(APITestCase):
    def setUp(self):
        from schools.models import Curso, Leccion, Unidad
        self.user = make_user("ld@x.com", is_estudiante=True)
        self.client.force_authenticate(self.user)
        self.curso = Curso.objects.create(nombre="C", descripcion="d")
        self.u1 = Unidad.objects.create(curso=self.curso, nombre="U1", orden=1)
        self.u2 = Unidad.objects.create(curso=self.curso, nombre="U2", orden=2)
        self.leccion = Leccion.objects.create(
            curso=self.curso, unidad=self.u1, nombre="L1", posicion=1,
            tipo='video', contenido='# Markdown', transcripcion='lorem',
            duracion_min=15,
        )

    def test_leccion_detalle_incluye_contenido_y_transcripcion(self):
        r = self.client.get(f'/api/v1/schools/lessons/{self.leccion.id}/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['tipo'], 'video')
        self.assertIn('contenido', r.data)
        self.assertIn('transcripcion', r.data)

    def test_listado_lecciones_no_incluye_contenido(self):
        r = self.client.get('/api/v1/schools/lessons/')
        self.assertEqual(r.status_code, 200)
        data = r.data['results'] if isinstance(r.data, dict) and 'results' in r.data else r.data
        for l in data:
            self.assertNotIn('contenido', l)
            self.assertNotIn('transcripcion', l)

    def test_curso_units_endpoint(self):
        r = self.client.get(f'/api/v1/schools/courses/{self.curso.id}/units/')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(len(r.data), 2)
        u1 = next(u for u in r.data if u['orden'] == 1)
        self.assertEqual(len(u1['lecciones']), 1)


class LeaderboardTests(APITestCase):
    def setUp(self):
        from schools.models import Escuela
        self.escuela = Escuela.objects.create(nombre="E", direccion="x", email="e@e.com", telefono="1")
        # Top 3 con XP escalonado
        self.u1 = make_user("u1@x.com", is_estudiante=True, escuela=self.escuela)
        self.u1.xp = 1000; self.u1.save()
        self.u2 = make_user("u2@x.com", is_estudiante=True, escuela=self.escuela)
        self.u2.xp = 500; self.u2.save()
        self.u3 = make_user("u3@x.com", is_estudiante=True, escuela=self.escuela)
        self.u3.xp = 100; self.u3.save()
        # Usuario fuera del top
        self.outsider = make_user("o@x.com", is_estudiante=True, escuela=self.escuela)
        self.outsider.xp = 10; self.outsider.save()

    def test_leaderboard_global_devuelve_top_ordenado(self):
        self.client.force_authenticate(self.u1)
        r = self.client.get(reverse('leaderboard'))
        self.assertEqual(r.status_code, 200)
        xps = [e['xp'] for e in r.data['top']]
        self.assertEqual(xps, sorted(xps, reverse=True))
        self.assertEqual(r.data['top'][0]['usuario_id'], self.u1.id)

    def test_leaderboard_devuelve_mi_rank_si_fuera_del_top(self):
        self.client.force_authenticate(self.outsider)
        r = self.client.get(reverse('leaderboard') + '?limit=2')
        self.assertEqual(r.status_code, 200)
        ids_top = [e['usuario_id'] for e in r.data['top']]
        self.assertNotIn(self.outsider.id, ids_top)
        self.assertEqual(r.data['me']['rank'], 4)

    def test_leaderboard_scope_escuela(self):
        from schools.models import Escuela
        otra = Escuela.objects.create(nombre="X", direccion="x", email="x@e.com", telefono="1")
        intruso = make_user("int@x.com", is_estudiante=True, escuela=otra)
        intruso.xp = 99999; intruso.save()

        self.client.force_authenticate(self.u1)
        r = self.client.get(reverse('leaderboard') + '?scope=escuela')
        self.assertEqual(r.status_code, 200)
        ids = {e['usuario_id'] for e in r.data['top']}
        self.assertNotIn(intruso.id, ids)


class PushTokenTests(APITestCase):
    def setUp(self):
        self.user = make_user("p@x.com", is_estudiante=True)
        self.client.force_authenticate(self.user)

    def test_registrar_push_token(self):
        r = self.client.post(reverse('me_push_token'), {
            "token": "ExponentPushToken[abc]", "platform": "expo",
        }, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.user.push_tokens.count(), 1)

    def test_registrar_mismo_token_es_idempotente(self):
        for _ in range(3):
            self.client.post(reverse('me_push_token'),
                             {"token": "tok", "platform": "expo"}, format='json')
        self.assertEqual(self.user.push_tokens.count(), 1)

    def test_eliminar_push_token(self):
        self.client.post(reverse('me_push_token'),
                         {"token": "del-me", "platform": "expo"}, format='json')
        r = self.client.delete(reverse('me_push_token_delete', args=['del-me']))
        self.assertEqual(r.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self.user.push_tokens.count(), 0)


class NotificacionesTests(APITestCase):
    def setUp(self):
        from accounts.models import Notificacion
        self.user = make_user("n@x.com", is_estudiante=True)
        self.client.force_authenticate(self.user)
        Notificacion.objects.create(usuario=self.user, tipo='info', titulo='A')
        Notificacion.objects.create(usuario=self.user, tipo='info', titulo='B')

    def test_listar(self):
        r = self.client.get(reverse('me_notifications'))
        self.assertEqual(r.status_code, 200)
        data = r.data['results'] if isinstance(r.data, dict) and 'results' in r.data else r.data
        self.assertEqual(len(data), 2)

    def test_filtro_unread(self):
        from accounts.models import Notificacion
        from django.utils import timezone
        n = Notificacion.objects.filter(usuario=self.user).first()
        n.read_at = timezone.now()
        n.save()
        r = self.client.get(reverse('me_notifications') + '?unread=true')
        data = r.data['results'] if isinstance(r.data, dict) and 'results' in r.data else r.data
        self.assertEqual(len(data), 1)

    def test_marcar_leida(self):
        from accounts.models import Notificacion
        n = Notificacion.objects.filter(usuario=self.user).first()
        r = self.client.post(reverse('me_notification_read', args=[n.id]))
        self.assertEqual(r.status_code, 200)
        n.refresh_from_db()
        self.assertIsNotNone(n.read_at)

    def test_marcar_todas(self):
        r = self.client.post(reverse('me_notifications_read_all'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['marcadas'], 2)


class NotificacionesAutoTests(APITestCase):
    """Signals emiten notificaciones en eventos."""

    def test_aprobar_evaluacion_genera_notif_y_otra_por_cert(self):
        from accounts.models import Notificacion, Prueba, Certificado
        from schools.models import Curso, Categoria, Ejercicio
        user = make_user("auto@x.com", is_estudiante=True)
        self.client.force_authenticate(user)

        curso = Curso.objects.create(nombre="C", descripcion="d")
        cat = Categoria.objects.create(nombre="X")
        for i in range(40):
            Ejercicio.objects.create(categoria=cat, curso=curso, pregunta=f"q{i}",
                                     opcion_a="OK", opcion_b="MAL",
                                     respuesta="OK")
        r = self.client.post(reverse('generate_test'), {
            "tipo": "completa", "modalidad": "evaluacion", "curso_id": curso.id,
        }, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        preguntas = r.data['preguntas']
        respuestas = {str(p['id']): 'a' for p in preguntas}
        self.client.post(reverse('submit_test', args=[r.data['prueba_id']]),
                         {"respuestas": respuestas}, format='json')

        tipos = list(Notificacion.objects.filter(usuario=user).values_list('tipo', flat=True))
        self.assertIn('test_passed', tipos)
        self.assertIn('certificate_issued', tipos)


class CanjearLlaveTests(APITestCase):
    def setUp(self):
        from schools.models import Curso, Escuela
        from sales.models import AccessKey
        self.user = make_user("canje@x.com", is_estudiante=True)
        self.client.force_authenticate(self.user)
        self.curso = Curso.objects.create(nombre="C", descripcion="d")
        self.key = AccessKey.objects.create()  # active por default, key auto-gen

    def test_canjear_llave_valida_crea_inscripcion(self):
        from sales.models import EstudianteCurso
        r = self.client.post('/api/v1/sales/canjear_llave/', {
            "access_key": self.key.key, "curso_id": self.curso.id,
        }, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(EstudianteCurso.objects.filter(estudiante_id=self.user).count(), 1)
        self.key.refresh_from_db()
        self.assertEqual(self.key.status, 'used')

    def test_canjear_dos_veces_misma_llave_falla(self):
        self.client.post('/api/v1/sales/canjear_llave/', {
            "access_key": self.key.key, "curso_id": self.curso.id,
        }, format='json')
        r2 = self.client.post('/api/v1/sales/canjear_llave/', {
            "access_key": self.key.key, "curso_id": self.curso.id,
        }, format='json')
        self.assertEqual(r2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r2.data['code'], 'key_inactive')

    def test_canjear_llave_inexistente_404(self):
        r = self.client.post('/api/v1/sales/canjear_llave/', {
            "access_key": "NO-EXISTE", "curso_id": self.curso.id,
        }, format='json')
        self.assertEqual(r.status_code, 404)

    def test_no_se_puede_canjear_mismo_curso_dos_veces(self):
        from sales.models import AccessKey
        self.client.post('/api/v1/sales/canjear_llave/', {
            "access_key": self.key.key, "curso_id": self.curso.id,
        }, format='json')
        otra = AccessKey.objects.create()
        r = self.client.post('/api/v1/sales/canjear_llave/', {
            "access_key": otra.key, "curso_id": self.curso.id,
        }, format='json')
        self.assertEqual(r.status_code, status.HTTP_409_CONFLICT)


class SocialLoginTests(APITestCase):
    """Login con Google/Apple usando verifiers mockeados vía settings."""

    def setUp(self):
        from django.conf import settings
        self._orig = getattr(settings, 'SOCIAL_AUTH_VERIFIERS', None)

    def tearDown(self):
        from django.conf import settings
        if self._orig is None:
            if hasattr(settings, 'SOCIAL_AUTH_VERIFIERS'):
                del settings.SOCIAL_AUTH_VERIFIERS
        else:
            settings.SOCIAL_AUTH_VERIFIERS = self._orig

    def _set_verifier(self, provider, fn):
        from django.conf import settings
        settings.SOCIAL_AUTH_VERIFIERS = {**getattr(settings, 'SOCIAL_AUTH_VERIFIERS', {}), provider: fn}

    def test_login_google_crea_usuario_y_devuelve_jwt(self):
        from accounts.models import Usuario
        self._set_verifier('google', lambda tok: {
            'email': 'g@x.com', 'nombre': 'G', 'apellido': 'X', 'sub': 'sub-1',
        })
        r = self.client.post(reverse('social_google'), {"id_token": "FAKE"}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn('access', r.data)
        self.assertIn('refresh', r.data)
        u = Usuario.objects.get(email='g@x.com')
        self.assertTrue(u.is_estudiante)
        self.assertTrue(u.is_active)

    def test_login_google_segunda_vez_no_duplica_usuario(self):
        from accounts.models import Usuario
        self._set_verifier('google', lambda tok: {
            'email': 'g2@x.com', 'nombre': 'G', 'apellido': 'X', 'sub': 'sub-2',
        })
        self.client.post(reverse('social_google'), {"id_token": "FAKE"}, format='json')
        self.client.post(reverse('social_google'), {"id_token": "FAKE"}, format='json')
        self.assertEqual(Usuario.objects.filter(email='g2@x.com').count(), 1)

    def test_login_google_token_invalido_401(self):
        from accounts.social import SocialAuthError
        def bad(tok):
            raise SocialAuthError("Bad token")
        self._set_verifier('google', bad)
        r = self.client.post(reverse('social_google'), {"id_token": "FAKE"}, format='json')
        self.assertEqual(r.status_code, 401)

    def test_login_apple_funciona_con_verifier_mock(self):
        self._set_verifier('apple', lambda tok: {
            'email': 'a@x.com', 'nombre': 'A', 'apellido': 'X', 'sub': 'sub-a',
        })
        r = self.client.post(reverse('social_apple'), {"identity_token": "FAKE"}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn('access', r.data)


class BibliotecaTests(APITestCase):
    def setUp(self):
        from schools.models import Curso, Recurso, Categoria
        from sales.models import AccessKey, EstudianteCurso
        self.cat = Categoria.objects.create(nombre="Señales")
        self.curso = Curso.objects.create(nombre="C", descripcion="d")
        self.curso_otro = Curso.objects.create(nombre="C2", descripcion="d")

        self.pub = Recurso.objects.create(
            titulo="Manual de conducción", tipo='pdf',
            url='https://files.example.com/manual.pdf', categoria=self.cat,
            requires_owned_course=False,
        )
        self.priv = Recurso.objects.create(
            titulo="Guía pro", tipo='pdf',
            url='https://files.example.com/pro.pdf', curso=self.curso,
            requires_owned_course=True,
        )
        self.priv_otro = Recurso.objects.create(
            titulo="Guía otra", tipo='pdf',
            url='https://files.example.com/otra.pdf', curso=self.curso_otro,
            requires_owned_course=True,
        )

        self.estudiante = make_user("est@x.com", is_estudiante=True)
        # Inscripción al curso
        key = AccessKey.objects.create()
        EstudianteCurso.objects.create(estudiante_id=self.estudiante, curso_id=self.curso, access_key_id=key)

    def _list(self):
        r = self.client.get('/api/v1/schools/library/')
        self.assertEqual(r.status_code, 200, r.data)
        data = r.data['results'] if isinstance(r.data, dict) and 'results' in r.data else r.data
        return {x['titulo'] for x in data}

    def test_estudiante_inscrito_ve_priv_solo_de_su_curso(self):
        self.client.force_authenticate(self.estudiante)
        titulos = self._list()
        self.assertIn("Manual de conducción", titulos)
        self.assertIn("Guía pro", titulos)
        self.assertNotIn("Guía otra", titulos)

    def test_estudiante_no_inscrito_solo_ve_publicos(self):
        otro = make_user("nope@x.com", is_estudiante=True)
        self.client.force_authenticate(otro)
        titulos = self._list()
        self.assertEqual(titulos, {"Manual de conducción"})

    def test_admin_ve_todos(self):
        admin = make_user("ad@x.com", is_admin=True)
        self.client.force_authenticate(admin)
        titulos = self._list()
        self.assertEqual(titulos, {"Manual de conducción", "Guía pro", "Guía otra"})

    def test_admin_puede_crear_recurso(self):
        admin = make_user("ad2@x.com", is_admin=True)
        self.client.force_authenticate(admin)
        r = self.client.post('/api/v1/schools/library/', {
            "titulo": "Nuevo", "tipo": "pdf",
            "url": "https://example.com/x.pdf",
        }, format='json')
        self.assertEqual(r.status_code, 201, r.data)

    def test_estudiante_no_puede_crear_recurso(self):
        self.client.force_authenticate(self.estudiante)
        r = self.client.post('/api/v1/schools/library/', {
            "titulo": "X", "tipo": "pdf", "url": "https://example.com/x.pdf",
        }, format='json')
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_filter_search_por_q(self):
        self.client.force_authenticate(self.estudiante)
        r = self.client.get('/api/v1/schools/library/?q=Manual')
        self.assertEqual(r.status_code, 200)
        data = r.data['results'] if isinstance(r.data, dict) and 'results' in r.data else r.data
        self.assertEqual(len(data), 1)


class ExpoPushDispatchTests(APITestCase):
    """_dispatch_push permanece deshabilitado por default; envía si EXPO_PUSH_ENABLED."""

    def test_push_no_se_envia_si_disabled(self):
        from accounts.models import Notificacion, PushToken
        from accounts.notifications import _dispatch_push
        user = make_user("nopush@x.com", is_estudiante=True)
        PushToken.objects.create(usuario=user, token="t1", platform='expo')
        notif = Notificacion.objects.create(usuario=user, tipo='info', titulo='X')
        # No exception, no network call (settings.EXPO_PUSH_ENABLED no set).
        _dispatch_push(user, notif)

    def test_push_intenta_enviar_si_habilitado(self):
        from accounts.models import Notificacion, PushToken
        from accounts.notifications import _dispatch_push, EXPO_PUSH_URL
        from django.conf import settings
        from unittest.mock import patch

        user = make_user("yespush@x.com", is_estudiante=True)
        PushToken.objects.create(usuario=user, token="ExponentPushToken[abc]", platform='expo')
        notif = Notificacion.objects.create(usuario=user, tipo='info', titulo='Hola')

        with patch.object(settings, 'EXPO_PUSH_ENABLED', True, create=True), \
             patch('requests.post') as post:
            post.return_value.status_code = 200
            _dispatch_push(user, notif)
            post.assert_called_once()
            args, kwargs = post.call_args
            self.assertEqual(args[0], EXPO_PUSH_URL)
            payloads = kwargs['json']
            self.assertEqual(payloads[0]['to'], "ExponentPushToken[abc]")
            self.assertEqual(payloads[0]['title'], 'Hola')


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


class EstudianteLeccionIdempotenteTests(APITestCase):
    """POST /accounts/estudiante-leccion/ debe ser idempotente:
    primera vez crea (201), siguientes devuelven existente (200), nunca duplica.
    """

    def setUp(self):
        from schools.models import Curso, Leccion
        self.user = make_user("luki@x.com", is_estudiante=True)
        self.client.force_authenticate(self.user)
        self.curso = Curso.objects.create(nombre="C", descripcion="d")
        self.leccion = Leccion.objects.create(
            curso=self.curso, nombre="L1", posicion=1, tipo="video",
        )

    def _post(self, leccion=None, curso=None):
        return self.client.post(
            "/api/v1/accounts/estudiante-leccion/",
            {
                "estudiante": self.user.id,
                "leccion": (leccion or self.leccion).id,
                "curso": (curso or self.curso).id,
            },
            format="json",
        )

    def test_primer_post_crea_201(self):
        from accounts.models import EstudianteLeccion
        r = self._post()
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(EstudianteLeccion.objects.count(), 1)

    def test_segundo_post_devuelve_200_no_duplica(self):
        from accounts.models import EstudianteLeccion
        r1 = self._post()
        r2 = self._post()
        self.assertEqual(r2.status_code, status.HTTP_200_OK, r2.data)
        self.assertEqual(EstudianteLeccion.objects.count(), 1)
        self.assertEqual(r1.data["id"], r2.data["id"])

    def test_multiples_posts_no_duplican(self):
        from accounts.models import EstudianteLeccion
        for _ in range(5):
            self._post()
        self.assertEqual(EstudianteLeccion.objects.count(), 1)

    def test_estudiante_no_puede_registrar_progreso_ajeno(self):
        """Si pasa otro `estudiante` en el body, se ignora y se usa request.user."""
        from accounts.models import EstudianteLeccion
        otro = make_user("otro@x.com", is_estudiante=True)
        r = self.client.post(
            "/api/v1/accounts/estudiante-leccion/",
            {
                "estudiante": otro.id,
                "leccion": self.leccion.id,
                "curso": self.curso.id,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        # El registro debe pertenecer al usuario autenticado, no a `otro`.
        rec = EstudianteLeccion.objects.get()
        self.assertEqual(rec.estudiante_id, self.user.id)

    def test_distintas_lecciones_si_se_registran(self):
        from accounts.models import EstudianteLeccion
        from schools.models import Leccion
        l2 = Leccion.objects.create(curso=self.curso, nombre="L2", posicion=2, tipo="video")
        self._post()
        self._post(leccion=l2)
        self.assertEqual(EstudianteLeccion.objects.count(), 2)
