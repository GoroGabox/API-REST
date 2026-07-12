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


class ActivarCursoConSeatsTests(APITestCase):
    """Modelo híbrido: seat consume cupo, key consume llave. Source auto/key/seat."""

    def setUp(self):
        self.escuela = Escuela.objects.create(
            nombre="Sub", direccion="x", email="s@s.com", telefono="1",
            basic_key=3, professional_key=0,
            basic_access=True, basic_seats_max=2, basic_seats_used=0,
            professional_access=False,
        )
        self.dir_ = make_user("dsub@x.com", is_director=True, escuela=self.escuela)
        self.est = make_user("esub@x.com", is_estudiante=True, escuela=self.escuela)
        self.curso_basico = Curso.objects.create(nombre="B", descripcion="d", is_profesional=False)
        self.curso_pro = Curso.objects.create(nombre="P", descripcion="d", is_profesional=True)
        self.client.force_authenticate(self.dir_)

    def _post(self, curso, source=None, days=None):
        body = {"user_id": self.est.id, "curso_id": curso.id}
        if days is not None: body["days"] = days
        if source: body["source"] = source
        return self.client.post("/api/v1/sales/activar_curso/", body, format="json")

    def test_auto_prefiere_seat_cuando_esta_disponible(self):
        r = self._post(self.curso_basico)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["origen"], "seat")
        self.assertIsNone(r.data["valid_until"])
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_seats_used, 1)
        self.assertEqual(self.escuela.basic_key, 3)  # no toca llaves

    def test_auto_cae_a_key_si_no_hay_seat(self):
        self.escuela.basic_seats_used = self.escuela.basic_seats_max
        self.escuela.save()
        r = self._post(self.curso_basico)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["origen"], "key")
        self.assertIsNotNone(r.data["valid_until"])
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_key, 2)

    def test_source_seat_falla_si_no_hay_cupo(self):
        self.escuela.basic_seats_used = self.escuela.basic_seats_max
        self.escuela.save()
        r = self._post(self.curso_basico, source="seat")
        self.assertEqual(r.status_code, 400, r.data)

    def test_source_key_ignora_seat_disponible(self):
        r = self._post(self.curso_basico, source="key")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["origen"], "key")
        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_seats_used, 0)
        self.assertEqual(self.escuela.basic_key, 2)

    def test_source_seat_para_curso_pro_sin_suscripcion_falla(self):
        # No hay professional_access
        r = self._post(self.curso_pro, source="seat")
        self.assertEqual(r.status_code, 400)

    def test_pro_sin_llaves_ni_seats_falla_400(self):
        r = self._post(self.curso_pro)
        self.assertEqual(r.status_code, 400)

    def test_source_invalido(self):
        r = self._post(self.curso_basico, source="banana")
        self.assertEqual(r.status_code, 400)


class ExtenderSeatRechazaTests(APITestCase):
    """Seats (sin expiración) no se pueden extender."""

    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        self.escuela = Escuela.objects.create(
            nombre="E", direccion="x", email="e@e.com", telefono="1",
            basic_key=5,
        )
        self.dir_ = make_user("dext_seat@x.com", is_director=True, escuela=self.escuela)
        self.est = make_user("eext_seat@x.com", is_estudiante=True, escuela=self.escuela)
        self.curso = Curso.objects.create(nombre="C", descripcion="d", is_profesional=False)
        # Simula una activación por seat manualmente.
        self.key_seat = AccessKey.objects.create(valid_until=None, origen="seat")
        EstudianteCurso.objects.create(
            estudiante_id=self.est, curso_id=self.curso, access_key_id=self.key_seat,
        )
        self.client.force_authenticate(self.dir_)

    def test_extender_seat_devuelve_400(self):
        r = self.client.post(
            "/api/v1/sales/extender_llave/",
            {"access_key_id": str(self.key_seat.id), "days": 30},
            format="json",
        )
        self.assertEqual(r.status_code, 400)


class SubscriptionStatusTests(APITestCase):
    def setUp(self):
        self.escuela = Escuela.objects.create(
            nombre="E", direccion="x", email="e@e.com", telefono="1",
            basic_key=3, professional_key=1,
            basic_access=True, basic_seats_max=10, basic_seats_used=4,
            professional_access=True, professional_seats_max=5, professional_seats_used=2,
        )
        self.escuela_b = Escuela.objects.create(
            nombre="B", direccion="x", email="b@e.com", telefono="1",
        )
        self.admin = make_user("adm_ss@x.com", is_admin=True)
        self.dir_ = make_user("dir_ss@x.com", is_director=True, escuela=self.escuela)
        self.dir_b = make_user("dir_ssb@x.com", is_director=True, escuela=self.escuela_b)
        self.est = make_user("est_ss@x.com", is_estudiante=True, escuela=self.escuela)

    def _get(self, school_id=None):
        return self.client.get(f"/api/v1/schools/{school_id or self.escuela.id}/subscription-status/")

    def test_director_ve_status_de_su_escuela(self):
        self.client.force_authenticate(self.dir_)
        r = self._get()
        self.assertEqual(r.status_code, 200, r.data)
        b = r.data["basic"]
        self.assertEqual(b["seats_max"], 10)
        self.assertEqual(b["seats_used"], 4)
        self.assertEqual(b["seats_available"], 6)
        self.assertEqual(b["keys_available"], 3)
        self.assertTrue(b["access"])
        p = r.data["professional"]
        self.assertEqual(p["seats_available"], 3)

    def test_director_otra_escuela_403(self):
        self.client.force_authenticate(self.dir_b)
        r = self._get(self.escuela.id)
        self.assertEqual(r.status_code, 403)

    def test_admin_ve_cualquiera(self):
        self.client.force_authenticate(self.admin)
        r = self._get(self.escuela.id)
        self.assertEqual(r.status_code, 200)

    def test_estudiante_403(self):
        self.client.force_authenticate(self.est)
        r = self._get()
        self.assertEqual(r.status_code, 403)


class SubscriptionSeatsTests(APITestCase):
    """GET + DELETE de cupos ocupados por estudiantes."""

    def setUp(self):
        self.escuela = Escuela.objects.create(
            nombre="E", direccion="x", email="e@e.com", telefono="1",
            basic_access=True, basic_seats_max=5, basic_seats_used=0,
        )
        self.escuela_b = Escuela.objects.create(
            nombre="B", direccion="y", email="b@b.com", telefono="1",
        )
        self.admin = make_user("adm_sea@x.com", is_admin=True)
        self.dir_ = make_user("dir_sea@x.com", is_director=True, escuela=self.escuela)
        self.dir_b = make_user("dir_seab@x.com", is_director=True, escuela=self.escuela_b)
        self.est_1 = make_user("e1_sea@x.com", is_estudiante=True, escuela=self.escuela)
        self.est_2 = make_user("e2_sea@x.com", is_estudiante=True, escuela=self.escuela)
        self.curso = Curso.objects.create(nombre="C", descripcion="d", is_profesional=False)

        # Ocupamos 2 seats via activar_curso
        self.client.force_authenticate(self.dir_)
        self.client.post("/api/v1/sales/activar_curso/", {
            "user_id": self.est_1.id, "curso_id": self.curso.id, "source": "seat",
        }, format="json")
        self.client.post("/api/v1/sales/activar_curso/", {
            "user_id": self.est_2.id, "curso_id": self.curso.id, "source": "seat",
        }, format="json")

    def test_list_devuelve_solo_seats(self):
        r = self.client.get(f"/api/v1/schools/{self.escuela.id}/subscription-seats/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["count"], 2)
        emails = {row["estudiante"]["email"] for row in r.data["results"]}
        self.assertEqual(emails, {self.est_1.email, self.est_2.email})

    def test_director_otra_escuela_403(self):
        self.client.force_authenticate(self.dir_b)
        r = self.client.get(f"/api/v1/schools/{self.escuela.id}/subscription-seats/")
        self.assertEqual(r.status_code, 403)

    def test_delete_libera_seat(self):
        r_list = self.client.get(f"/api/v1/schools/{self.escuela.id}/subscription-seats/")
        ec_id = r_list.data["results"][0]["id"]
        self.escuela.refresh_from_db()
        seats_before = self.escuela.basic_seats_used

        r_del = self.client.delete(
            f"/api/v1/schools/{self.escuela.id}/subscription-seats/{ec_id}/"
        )
        self.assertEqual(r_del.status_code, 200, r_del.data)
        self.assertEqual(r_del.data["status"], "released")

        self.escuela.refresh_from_db()
        self.assertEqual(self.escuela.basic_seats_used, seats_before - 1)
        # La inscripción fue borrada.
        from sales.models import EstudianteCurso
        self.assertFalse(EstudianteCurso.objects.filter(pk=ec_id).exists())

    def test_delete_de_inscripcion_key_devuelve_400(self):
        from sales.models import AccessKey, EstudianteCurso
        from datetime import timedelta
        from django.utils import timezone
        # Nueva inscripción vía key
        est3 = make_user("e3_sea@x.com", is_estudiante=True, escuela=self.escuela)
        k = AccessKey.objects.create(
            valid_until=timezone.now() + timedelta(days=30), origen="key",
        )
        ec = EstudianteCurso.objects.create(
            estudiante_id=est3, curso_id=self.curso, access_key_id=k,
        )
        r = self.client.delete(
            f"/api/v1/schools/{self.escuela.id}/subscription-seats/{ec.id}/"
        )
        self.assertEqual(r.status_code, 400)

    def test_delete_inscripcion_de_otra_escuela_404(self):
        # Preparar seat en escuela B usando admin
        self.escuela_b.basic_access = True
        self.escuela_b.basic_seats_max = 3
        self.escuela_b.save()
        est_b = make_user("eb_sea@x.com", is_estudiante=True, escuela=self.escuela_b)
        self.client.force_authenticate(self.admin)
        self.client.post("/api/v1/sales/activar_curso/", {
            "user_id": est_b.id, "curso_id": self.curso.id, "source": "seat",
        }, format="json")
        r_list = self.client.get(f"/api/v1/schools/{self.escuela_b.id}/subscription-seats/")
        ec_id_b = r_list.data["results"][0]["id"]

        self.client.force_authenticate(self.dir_)
        r = self.client.delete(
            f"/api/v1/schools/{self.escuela.id}/subscription-seats/{ec_id_b}/"
        )
        self.assertEqual(r.status_code, 404)


# ======================================================================
# Revocar llave (#8) y bloqueo de escritura directa en viewsets (#2)
# ======================================================================
class RevocarLlaveTests(APITestCase):
    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        from schools.models import Curso, Escuela
        from sales.models import AccessKey, EstudianteCurso

        self.escuela_a = Escuela.objects.create(
            nombre="A", direccion="x", email="a@a.com", telefono="1",
            basic_key=5, professional_key=5,
        )
        self.escuela_b = Escuela.objects.create(
            nombre="B", direccion="y", email="b@b.com", telefono="2",
        )
        self.admin = make_user("adm_rev@a.com", is_admin=True)
        self.dir_a = make_user("dira_rev@a.com", is_director=True, escuela=self.escuela_a)
        self.dir_b = make_user("dirb_rev@b.com", is_director=True, escuela=self.escuela_b)
        self.est_a = make_user("esta_rev@a.com", is_estudiante=True, escuela=self.escuela_a)
        self.curso = Curso.objects.create(nombre="B", descripcion="d", is_profesional=False)

        now = timezone.now()
        self.key = AccessKey.objects.create(
            valid_from=now - timedelta(days=1), valid_until=now + timedelta(days=10), origen="key",
        )
        EstudianteCurso.objects.create(
            estudiante_id=self.est_a, curso_id=self.curso, access_key_id=self.key,
        )

    def _revocar(self, key_id):
        return self.client.post(
            "/api/v1/sales/revocar_llave/", {"access_key_id": str(key_id)}, format="json",
        )

    def test_director_revoca_llave_de_su_escuela(self):
        self.client.force_authenticate(self.dir_a)
        r = self._revocar(self.key.id)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.key.refresh_from_db()
        self.assertEqual(self.key.status, "revoked")

    def test_director_de_otra_escuela_403(self):
        self.client.force_authenticate(self.dir_b)
        r = self._revocar(self.key.id)
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.key.refresh_from_db()
        self.assertEqual(self.key.status, "active")

    def test_estudiante_403(self):
        self.client.force_authenticate(self.est_a)
        self.assertEqual(self._revocar(self.key.id).status_code, status.HTTP_403_FORBIDDEN)

    def test_llave_inexistente_404(self):
        self.client.force_authenticate(self.dir_a)
        r = self._revocar("00000000-0000-0000-0000-000000000000")
        self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)

    def test_revocar_seat_libera_cupo(self):
        from sales.models import AccessKey, EstudianteCurso
        self.escuela_a.basic_seats_used = 1
        self.escuela_a.save()
        seat_key = AccessKey.objects.create(valid_until=None, origen="seat")
        est2 = make_user("esta2_rev@a.com", is_estudiante=True, escuela=self.escuela_a)
        EstudianteCurso.objects.create(
            estudiante_id=est2, curso_id=self.curso, access_key_id=seat_key,
        )
        self.client.force_authenticate(self.dir_a)
        r = self._revocar(seat_key.id)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.escuela_a.refresh_from_db()
        self.assertEqual(self.escuela_a.basic_seats_used, 0)


class DirectWriteAuthzTests(APITestCase):
    """El director puede LEER llaves/inscripciones de su escuela pero no crear/
    editar por la vía genérica (evita otorgar acceso sin consumir saldo)."""

    def setUp(self):
        from schools.models import Curso, Escuela
        self.escuela = Escuela.objects.create(
            nombre="A", direccion="x", email="a@a.com", telefono="1", basic_key=5,
        )
        self.admin = make_user("adm_authz@a.com", is_admin=True)
        self.director = make_user("dir_authz@a.com", is_director=True, escuela=self.escuela)
        self.est = make_user("est_authz@a.com", is_estudiante=True, escuela=self.escuela)
        self.curso = Curso.objects.create(nombre="B", descripcion="d", is_profesional=False)

    def test_director_lee_pero_no_crea_access_key(self):
        self.client.force_authenticate(self.director)
        self.assertEqual(self.client.get("/api/v1/sales/access_key/").status_code, status.HTTP_200_OK)
        r = self.client.post(
            "/api/v1/sales/access_key/", {"status": "active", "origen": "key"}, format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_director_no_crea_estudiante_curso(self):
        from sales.models import EstudianteCurso
        self.client.force_authenticate(self.director)
        r = self.client.post(
            "/api/v1/sales/estudiante_curso/",
            {"estudiante_id": self.est.id, "curso_id": self.curso.id},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            EstudianteCurso.objects.filter(estudiante_id_id=self.est.id).count(), 0,
        )

    def test_admin_si_crea_access_key(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post(
            "/api/v1/sales/access_key/", {"status": "active", "origen": "key"}, format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
