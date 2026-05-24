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
