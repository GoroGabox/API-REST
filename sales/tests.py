from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Usuario
from accounts.tests import make_user
from schools.models import Curso, Escuela
from sales.models import AccessKey, EstudianteCurso
from sales.views import MercadoPagoPaymentStrategy


class MercadoPagoStubTests(APITestCase):
    def test_create_transaction_raises(self):
        with self.assertRaises(NotImplementedError):
            MercadoPagoPaymentStrategy().create_transaction(100, "order", "session")

    def test_confirm_transaction_raises(self):
        with self.assertRaises(NotImplementedError):
            MercadoPagoPaymentStrategy().confirm_transaction({})


class UnifiedSaleInitiationTests(APITestCase):
    def setUp(self):
        self.user = make_user("t@u.com", is_estudiante=True)
        self.client.force_authenticate(self.user)

    def test_mercadopago_devuelve_501(self):
        r = self.client.post('/api/v1/sales/pay_init/', {
            "amount": 1000,
            "session_id": "sess",
            "buy_order": f"order_1_{self.user.id}",
            "payment_method": "mercadopago",
        }, format='json')
        self.assertEqual(r.status_code, status.HTTP_501_NOT_IMPLEMENTED)


class ActivarCursoTests(APITestCase):
    def setUp(self):
        self.escuela_a = Escuela.objects.create(
            nombre="A", direccion="x", email="a@a.com", telefono="1",
            basic_key=2, professional_key=1,
        )
        self.escuela_b = Escuela.objects.create(
            nombre="B", direccion="y", email="b@b.com", telefono="2",
        )
        self.admin = make_user("ad@a.com", is_admin=True)
        self.dir_a = make_user("da@a.com", is_director=True, escuela=self.escuela_a)
        self.dir_b = make_user("db@b.com", is_director=True, escuela=self.escuela_b)
        self.est_a = make_user("ea@a.com", is_estudiante=True, escuela=self.escuela_a)
        self.curso_basico = Curso.objects.create(nombre="Básico", descripcion="d", is_profesional=False)
        self.curso_pro = Curso.objects.create(nombre="Pro", descripcion="d", is_profesional=True)

    def _payload(self, user_id, curso_id, days=30):
        return {"user_id": user_id, "curso_id": curso_id, "days": days}

    def test_estudiante_no_puede_activar(self):
        self.client.force_authenticate(self.est_a)
        r = self.client.post('/api/v1/sales/activar_curso/',
                             self._payload(self.est_a.id, self.curso_basico.id),
                             format='json')
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_director_otra_escuela_obtiene_403(self):
        self.client.force_authenticate(self.dir_b)
        r = self.client.post('/api/v1/sales/activar_curso/',
                             self._payload(self.est_a.id, self.curso_basico.id),
                             format='json')
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_director_sin_saldo_basico_falla(self):
        self.escuela_a.basic_key = 0
        self.escuela_a.save()
        self.client.force_authenticate(self.dir_a)
        r = self.client.post('/api/v1/sales/activar_curso/',
                             self._payload(self.est_a.id, self.curso_basico.id),
                             format='json')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_director_con_saldo_activa_y_decrementa(self):
        self.client.force_authenticate(self.dir_a)
        r = self.client.post('/api/v1/sales/activar_curso/',
                             self._payload(self.est_a.id, self.curso_basico.id),
                             format='json')
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.escuela_a.refresh_from_db()
        self.assertEqual(self.escuela_a.basic_key, 1)
        self.assertEqual(EstudianteCurso.objects.filter(estudiante_id=self.est_a).count(), 1)

    def test_admin_activa_sin_consumir_saldo(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post('/api/v1/sales/activar_curso/',
                             self._payload(self.est_a.id, self.curso_pro.id),
                             format='json')
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.escuela_a.refresh_from_db()
        # admin no consume saldo de la escuela
        self.assertEqual(self.escuela_a.professional_key, 1)


class PaymentOwnershipTests(APITestCase):
    """user_id en payloads de pago debe coincidir con request.user (excepto admin)."""

    def setUp(self):
        self.user = make_user("buyer@x.com", is_estudiante=True)
        self.other = make_user("other@x.com", is_estudiante=True)
        self.admin = make_user("ad@x.com", is_admin=True)

    def test_init_rechaza_buy_order_de_otro_usuario(self):
        self.client.force_authenticate(self.user)
        # buy_order codifica student_id = self.other.id
        r = self.client.post('/api/v1/sales/webpay_init/', {
            "amount": 1000,
            "session_id": "sess",
            "buy_order": f"order_5_{self.other.id}",
        }, format='json')
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_init_acepta_buy_order_propio(self):
        self.client.force_authenticate(self.user)
        # No llegamos a llamar Transbank porque amount/auth se valida primero.
        # Si pasa el guard, falla luego en Transbank (500). Lo importante:
        # NO debe ser 403.
        r = self.client.post('/api/v1/sales/webpay_init/', {
            "amount": 1000,
            "session_id": "sess",
            "buy_order": f"order_5_{self.user.id}",
        }, format='json')
        self.assertNotEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_init_rechaza_buy_order_malformado(self):
        self.client.force_authenticate(self.user)
        r = self.client.post('/api/v1/sales/webpay_init/', {
            "amount": 1000,
            "session_id": "sess",
            "buy_order": "malformed",
        }, format='json')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_confirm_rechaza_user_id_ajeno(self):
        self.client.force_authenticate(self.user)
        r = self.client.post('/api/v1/sales/webpay_confirm/', {
            "token_ws": "tok",
            "product_id": 1,
            "user_id": self.other.id,
        }, format='json')
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_puede_pagar_por_otro(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post('/api/v1/sales/webpay_init/', {
            "amount": 1000,
            "session_id": "sess",
            "buy_order": f"order_5_{self.user.id}",
        }, format='json')
        self.assertNotEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_unified_confirm_rechaza_user_id_ajeno(self):
        self.client.force_authenticate(self.user)
        r = self.client.post('/api/v1/sales/pay_confirm/', {
            "payment_method": "transbank",
            "token_ws": "tok",
            "product_id": 1,
            "user_id": self.other.id,
        }, format='json')
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)


class CatalogoLecturaTests(APITestCase):
    def test_estudiante_lee_productos_pero_no_escribe(self):
        est = make_user("e@x.com", is_estudiante=True)
        self.client.force_authenticate(est)
        r = self.client.get('/api/v1/sales/productos/')
        self.assertEqual(r.status_code, 200)
        r = self.client.post('/api/v1/sales/productos/', {"nombre": "X", "tipo": "llave", "descripcion": "d"}, format='json')
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_escribe_productos(self):
        admin = make_user("a@x.com", is_admin=True)
        self.client.force_authenticate(admin)
        r = self.client.post('/api/v1/sales/productos/', {
            "nombre": "X", "tipo": "llave", "descripcion": "d", "valor_neto": "100",
        }, format='json')
        self.assertEqual(r.status_code, 201, r.data)


class ExtenderLlaveTests(APITestCase):
    """Extensión de llaves: director descuenta saldo de su escuela atómicamente."""

    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        from schools.models import Curso, Escuela
        from sales.models import AccessKey, EstudianteCurso

        self.escuela_a = Escuela.objects.create(
            nombre="A", direccion="x", email="a@a.com", telefono="1",
            basic_key=3, professional_key=2,
        )
        self.escuela_b = Escuela.objects.create(
            nombre="B", direccion="y", email="b@b.com", telefono="2",
            basic_key=5, professional_key=5,
        )
        self.admin = make_user("ad_ext@a.com", is_admin=True)
        self.dir_a = make_user("da_ext@a.com", is_director=True, escuela=self.escuela_a)
        self.dir_b = make_user("db_ext@b.com", is_director=True, escuela=self.escuela_b)
        self.est_a = make_user("ea_ext@a.com", is_estudiante=True, escuela=self.escuela_a)
        self.curso_basico = Curso.objects.create(nombre="B", descripcion="d", is_profesional=False)
        self.curso_pro = Curso.objects.create(nombre="P", descripcion="d", is_profesional=True)

        # Llave activa (no expirada)
        now = timezone.now()
        self.key_activa = AccessKey.objects.create(
            valid_from=now - timedelta(days=10),
            valid_until=now + timedelta(days=5),
        )
        EstudianteCurso.objects.create(
            estudiante_id=self.est_a, curso_id=self.curso_basico, access_key_id=self.key_activa,
        )

    def _post(self, key_id, days=30):
        return self.client.post(
            '/api/v1/sales/extender_llave/',
            {"access_key_id": str(key_id), "days": days},
            format='json',
        )

    def test_director_extiende_y_descuenta_saldo(self):
        from django.utils import timezone
        from datetime import timedelta
        self.client.force_authenticate(self.dir_a)
        valid_until_before = self.key_activa.valid_until

        r = self._post(self.key_activa.id, days=30)
        self.assertEqual(r.status_code, 200, r.data)

        self.escuela_a.refresh_from_db()
        self.assertEqual(self.escuela_a.basic_key, 2)  # 3 -> 2

        self.key_activa.refresh_from_db()
        # Se sumaron 30 días sobre la fecha previa.
        delta = self.key_activa.valid_until - valid_until_before
        self.assertGreaterEqual(delta, timedelta(days=29, hours=23))

    def test_director_sin_saldo_falla(self):
        self.escuela_a.basic_key = 0
        self.escuela_a.save()
        self.client.force_authenticate(self.dir_a)
        r = self._post(self.key_activa.id)
        self.assertEqual(r.status_code, 400)

    def test_director_otra_escuela_obtiene_403(self):
        self.client.force_authenticate(self.dir_b)
        r = self._post(self.key_activa.id)
        self.assertEqual(r.status_code, 403)

    def test_admin_extiende_sin_consumir_saldo(self):
        self.client.force_authenticate(self.admin)
        r = self._post(self.key_activa.id)
        self.assertEqual(r.status_code, 200, r.data)
        self.escuela_a.refresh_from_db()
        self.assertEqual(self.escuela_a.basic_key, 3)  # sin cambio

    def test_estudiante_obtiene_403(self):
        self.client.force_authenticate(self.est_a)
        r = self._post(self.key_activa.id)
        self.assertEqual(r.status_code, 403)

    def test_extender_llave_expirada_reinicia_desde_hoy(self):
        from datetime import timedelta
        from django.utils import timezone
        # Marcar expirada
        self.key_activa.valid_until = timezone.now() - timedelta(days=2)
        self.key_activa.save()

        self.client.force_authenticate(self.dir_a)
        r = self._post(self.key_activa.id, days=30)
        self.assertEqual(r.status_code, 200, r.data)

        self.key_activa.refresh_from_db()
        # valid_from quedó en aprox ahora; valid_until = ahora + 30 días
        now = timezone.now()
        self.assertLess(abs((self.key_activa.valid_from - now).total_seconds()), 60)
        delta = self.key_activa.valid_until - self.key_activa.valid_from
        self.assertGreaterEqual(delta, timedelta(days=29, hours=23))

    def test_llave_sin_inscripcion_falla(self):
        from sales.models import AccessKey
        huerfana = AccessKey.objects.create()
        self.client.force_authenticate(self.dir_a)
        r = self._post(huerfana.id)
        self.assertEqual(r.status_code, 400)

    def test_llave_inexistente_404(self):
        self.client.force_authenticate(self.dir_a)
        r = self._post("00000000-0000-0000-0000-000000000000")
        self.assertEqual(r.status_code, 404)
