"""Tests de 2FA (TOTP + códigos de recuperación), notificaciones por email y
la preferencia `email_notifications`. Cubren los flujos añadidos para el
lanzamiento del segmento director.
"""
import pyotp
from django.core import mail
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Usuario, Notificacion, Certificado
from accounts.tests import make_user
from accounts.twofa import hash_code
from schools.models import Escuela, Curso

PASSWORD = "Abcdef12!@#"  # el que usa make_user


def _enable_2fa(client, user):
    """Activa 2FA vía API. Devuelve (secret, recovery_codes)."""
    secret = client.post(reverse("me_2fa_setup")).data["secret"]
    r = client.post(reverse("me_2fa_verify"), {"code": pyotp.TOTP(secret).now()}, format="json")
    return secret, r.data["recovery_codes"]


class TwoFASetupVerifyTests(APITestCase):
    def setUp(self):
        self.user = make_user("u2fa@x.com")
        self.client.force_authenticate(self.user)

    def test_setup_devuelve_qr_y_no_activa_aun(self):
        r = self.client.post(reverse("me_2fa_setup"))
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertIn("secret", r.data)
        self.assertTrue(r.data["qr"].startswith("data:image/png;base64,"))
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_2fa_enabled)
        self.assertTrue(self.user.totp_secret)

    def test_verify_codigo_incorrecto_rechazado(self):
        self.client.post(reverse("me_2fa_setup"))
        r = self.client.post(reverse("me_2fa_verify"), {"code": "000000"}, format="json")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_2fa_enabled)

    def test_verify_activa_y_entrega_codigos_hasheados(self):
        _, codes = _enable_2fa(self.client, self.user)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_2fa_enabled)
        self.assertEqual(len(codes), 8)
        self.assertEqual(len(self.user.totp_recovery_codes), 8)
        # Se guardan hasheados, nunca en texto plano.
        self.assertNotIn(codes[0], self.user.totp_recovery_codes)
        self.assertIn(hash_code(codes[0]), self.user.totp_recovery_codes)

    def test_me_no_filtra_secreto_ni_hashes(self):
        _enable_2fa(self.client, self.user)
        r = self.client.get("/api/v1/accounts/me/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertTrue(r.data["is_2fa_enabled"])
        self.assertEqual(r.data["recovery_codes_remaining"], 8)
        self.assertNotIn("totp_secret", r.data)
        self.assertNotIn("totp_recovery_codes", r.data)


class TwoFALoginChallengeTests(APITestCase):
    def setUp(self):
        self.user = make_user("login2fa@x.com")
        self.client.force_authenticate(self.user)
        self.secret, self.codes = _enable_2fa(self.client, self.user)
        self.client.force_authenticate(None)  # el login va sin sesión

    def _login(self, otp=None):
        payload = {"email": self.user.email, "password": PASSWORD}
        if otp is not None:
            payload["otp"] = otp
        return self.client.post(reverse("login_view"), payload, format="json")

    def test_login_sin_otp_es_desafiado(self):
        r = self._login()
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(r.data.get("otp_required"))
        self.assertNotIn("access", r.data)

    def test_login_con_totp_ok(self):
        r = self._login(pyotp.TOTP(self.secret).now())
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertIn("access", r.data)

    def test_login_con_codigo_recuperacion_consume(self):
        r = self._login(self.codes[0])
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertIn("access", r.data)
        self.user.refresh_from_db()
        self.assertEqual(len(self.user.totp_recovery_codes), 7)

    def test_codigo_recuperacion_un_solo_uso(self):
        self.assertEqual(self._login(self.codes[0]).status_code, status.HTTP_200_OK)
        self.assertEqual(self._login(self.codes[0]).status_code, status.HTTP_400_BAD_REQUEST)

    def test_login_password_incorrecta_no_da_otp_required(self):
        r = self.client.post(
            reverse("login_view"),
            {"email": self.user.email, "password": "mala", "otp": pyotp.TOTP(self.secret).now()},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_401_UNAUTHORIZED)


class TwoFARecoveryAndDisableTests(APITestCase):
    def setUp(self):
        self.user = make_user("rec2fa@x.com")
        self.client.force_authenticate(self.user)
        self.secret, self.codes = _enable_2fa(self.client, self.user)

    def test_regenerar_requiere_totp_e_invalida_anteriores(self):
        # Sin código válido → 400.
        bad = self.client.post(reverse("me_2fa_recovery"), {"code": "000000"}, format="json")
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)

        r = self.client.post(reverse("me_2fa_recovery"), {"code": pyotp.TOTP(self.secret).now()}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        nuevos = r.data["recovery_codes"]
        self.assertEqual(len(nuevos), 8)
        self.user.refresh_from_db()
        # Los viejos ya no están; los nuevos sí.
        self.assertNotIn(hash_code(self.codes[0]), self.user.totp_recovery_codes)
        self.assertIn(hash_code(nuevos[0]), self.user.totp_recovery_codes)

    def test_disable_con_totp(self):
        r = self.client.post(reverse("me_2fa_disable"), {"code": pyotp.TOTP(self.secret).now()}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_2fa_enabled)
        self.assertEqual(self.user.totp_secret, "")
        self.assertEqual(self.user.totp_recovery_codes, [])

    def test_disable_con_codigo_recuperacion(self):
        r = self.client.post(reverse("me_2fa_disable"), {"code": self.codes[0]}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_2fa_enabled)


class TwoFAAdminDisableTests(APITestCase):
    def setUp(self):
        self.escuela = Escuela.objects.create(nombre="E", direccion="x", email="e@e.com", telefono="1")
        self.admin = make_user("admin_2fa@x.com", is_admin=True)
        self.director = make_user("dir_2fa@x.com", is_director=True, escuela=self.escuela)
        # Activar 2FA del director.
        self.client.force_authenticate(self.director)
        _enable_2fa(self.client, self.director)
        self.client.force_authenticate(None)

    def test_admin_puede_resetear(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post(reverse("admin_2fa_disable", args=[self.director.id]), {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.director.refresh_from_db()
        self.assertFalse(self.director.is_2fa_enabled)
        self.assertEqual(self.director.totp_secret, "")
        self.assertEqual(self.director.totp_recovery_codes, [])

    def test_no_admin_obtiene_403(self):
        self.client.force_authenticate(self.director)
        r = self.client.post(reverse("admin_2fa_disable", args=[self.admin.id]), {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)


class EmailNotificationPreferenceTests(APITestCase):
    def setUp(self):
        self.escuela = Escuela.objects.create(nombre="E", direccion="x", email="e@e.com", telefono="1")
        self.director = make_user("dir_notif@x.com", is_director=True, escuela=self.escuela)
        self.est = make_user("est_notif@x.com", is_estudiante=True, escuela=self.escuela)
        self.curso = Curso.objects.create(nombre="C", descripcion="d", is_profesional=False)

    def test_email_notifications_se_persiste_via_me(self):
        self.client.force_authenticate(self.est)
        r = self.client.patch("/api/v1/accounts/me/", {"email_notifications": False}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.est.refresh_from_db()
        self.assertFalse(self.est.email_notifications)

    def test_certificado_notifica_a_estudiante_y_director_por_email(self):
        mail.outbox = []
        n_est = Notificacion.objects.filter(usuario=self.est).count()
        n_dir = Notificacion.objects.filter(usuario=self.director).count()

        Certificado.objects.create(estudiante=self.est, curso=self.curso)

        # Notificación in-app a ambos.
        self.assertEqual(Notificacion.objects.filter(usuario=self.est).count(), n_est + 1)
        self.assertEqual(Notificacion.objects.filter(usuario=self.director).count(), n_dir + 1)
        # Email a ambos (ambos opt-in por defecto).
        destinatarios = sorted(sum([m.to for m in mail.outbox], []))
        self.assertIn(self.est.email, destinatarios)
        self.assertIn(self.director.email, destinatarios)

    def test_preferencia_off_omite_email_pero_conserva_in_app(self):
        self.est.email_notifications = False
        self.est.save()
        self.director.email_notifications = False
        self.director.save()
        mail.outbox = []
        n_est = Notificacion.objects.filter(usuario=self.est).count()

        Certificado.objects.create(estudiante=self.est, curso=self.curso)

        # In-app se crea igual…
        self.assertEqual(Notificacion.objects.filter(usuario=self.est).count(), n_est + 1)
        # …pero sin correos.
        self.assertEqual(len(mail.outbox), 0)
