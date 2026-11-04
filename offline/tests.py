"""Modo offline: licencias de descarga, límite de dispositivos, renovación y firma.

Correr:  DJANGO_ENV=test manage.py test offline
"""
import base64
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import EstudianteLeccion
from accounts.tests import make_user
from sales.models import AccessKey, EstudianteCurso
from schools.models import Curso, Leccion, LeccionRecurso, Unidad

from . import services
from .models import LicenciaOffline

URL = '/api/v1/offline/licencias/'


def _b64d(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def dar_acceso(alumno, curso, dias=30, **kw):
    valid_until = timezone.now() + timedelta(days=dias) if dias is not None else None
    ak = AccessKey.objects.create(valid_until=valid_until, origen=kw.pop('origen', 'purchase'),
                                  status='active', **kw)
    EstudianteCurso.objects.create(estudiante_id=alumno, curso_id=curso, access_key_id=ak)
    return ak


class OfflineBase(TestCase):
    def setUp(self):
        self.al = make_user('al@a.com', is_estudiante=True)
        self.curso = Curso.objects.create(nombre='Curso B', descripcion='d', codigo='B')
        self.unidad = Unidad.objects.create(curso=self.curso, nombre='U1', orden=1)
        self.l1 = Leccion.objects.create(curso=self.curso, unidad=self.unidad, nombre='L1', posicion=1,
                                         tipo='texto', contenido='Texto {{figura:f1}}')
        self.l2 = Leccion.objects.create(curso=self.curso, unidad=self.unidad, nombre='Quiz', posicion=2,
                                         tipo='quiz', contenido='{"questions": []}',
                                         url_audio='https://cdn.x/audio-l2.mp3')
        LeccionRecurso.objects.create(leccion=self.l1, tipo='imagen', rol='figura', clave='f1',
                                      url='https://cdn.x/f1.png', meta={'hash': 'abc'})
        LeccionRecurso.objects.create(leccion=self.l1, tipo='pdf', rol='paginas_libro', clave='paginas',
                                      url='https://cdn.x/p.pdf')
        LeccionRecurso.objects.create(leccion=self.l1, tipo='video', rol='principal', clave='video',
                                      url='https://cdn.x/v.mp4')
        self.c = self.cliente(self.al)

    def cliente(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def pedir(self, cliente=None, device=None, curso=None):
        device = device or str(uuid.uuid4())
        r = (cliente or self.c).post(URL, {
            'curso': curso or self.curso.id, 'device_id': device, 'plataforma': 'android', 'nombre': 'Pixel',
        }, format='json')
        return r, device


class EmisionTests(OfflineBase):
    def test_sin_acceso_403(self):
        r, _ = self.pedir()
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data['codigo'], 'sin_acceso')

    def test_acceso_vencido_403(self):
        ak = dar_acceso(self.al, self.curso)
        ak.valid_until = timezone.now() - timedelta(minutes=1)
        ak.save()
        r, _ = self.pedir()
        self.assertEqual(r.status_code, 403)

    def test_no_estudiante_403(self):
        dora = make_user('dora@a.com', is_director=True)
        r, _ = self.pedir(cliente=self.cliente(dora))
        self.assertEqual(r.status_code, 403)

    def test_device_id_invalido_400(self):
        dar_acceso(self.al, self.curso)
        r = self.c.post(URL, {'curso': self.curso.id, 'device_id': 'x'}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_emite_licencia_firmada_cek_y_manifest(self):
        dar_acceso(self.al, self.curso, dias=30)
        r, device = self.pedir()
        self.assertEqual(r.status_code, 201, r.data)

        lic = json.loads(r.data['licencia'])
        self.assertEqual(lic['curso'], self.curso.id)
        self.assertEqual(lic['dispositivo'], device)
        self.assertEqual(lic['usuario'], self.al.id)
        # offline_hasta = ahora + 7 días (el acceso dura más).
        hasta = timezone.datetime.fromisoformat(lic['offline_hasta'].replace('Z', '+00:00'))
        self.assertAlmostEqual((hasta - timezone.now()).total_seconds(), 7 * 86400, delta=60)

        # Firma verificable con la llave pública publicada.
        pub = self.client.get('/api/v1/offline/llave-publica/').data['llave_publica']
        Ed25519PublicKey.from_public_bytes(_b64d(pub)).verify(_b64d(r.data['firma']), r.data['licencia'].encode())

        self.assertEqual(len(base64.b64decode(r.data['cek'])), 32)

        m = r.data['manifest']
        self.assertEqual([l['id'] for l in m['lecciones']], [self.l1.id, self.l2.id])
        self.assertEqual(m['lecciones'][0]['contenido'], 'Texto {{figura:f1}}')
        ids = {a['id']: a for a in m['archivos']}
        tipos = sorted(a['tipo'] for a in m['archivos'])
        self.assertEqual(tipos, ['audio', 'imagen', 'pdf'])  # sin video
        self.assertIn(f'l{self.l2.id}-audio', ids)          # espejo url_audio sin recurso
        self.assertEqual(len(m['unidades']), 1)

    def test_acepta_slug(self):
        dar_acceso(self.al, self.curso)
        r, _ = self.pedir(curso=self.curso.slug)
        self.assertEqual(r.status_code, 201, r.data)

    def test_offline_hasta_acotado_al_fin_del_acceso(self):
        ak = dar_acceso(self.al, self.curso, dias=2)
        r, _ = self.pedir()
        lic = json.loads(r.data['licencia'])
        self.assertEqual(lic['offline_hasta'], ak.valid_until.isoformat().replace('+00:00', 'Z'))
        self.assertEqual(lic['acceso_hasta'], lic['offline_hasta'])

    def test_cupo_sin_vencimiento_tope_7_dias(self):
        dar_acceso(self.al, self.curso, dias=None, origen='seat')
        r, _ = self.pedir()
        lic = json.loads(r.data['licencia'])
        self.assertIsNone(lic['acceso_hasta'])
        self.assertIsNotNone(lic['offline_hasta'])

    def test_misma_licencia_y_cek_al_repedir(self):
        dar_acceso(self.al, self.curso)
        r1, device = self.pedir()
        r2, _ = self.pedir(device=device)
        self.assertEqual(json.loads(r1.data['licencia'])['lic'], json.loads(r2.data['licencia'])['lic'])
        self.assertEqual(r1.data['cek'], r2.data['cek'])

    def test_cek_distinta_por_dispositivo(self):
        dar_acceso(self.al, self.curso)
        r1, _ = self.pedir()
        r2, _ = self.pedir()
        self.assertNotEqual(r1.data['cek'], r2.data['cek'])


class LimiteDispositivosTests(OfflineBase):
    def setUp(self):
        super().setUp()
        dar_acceso(self.al, self.curso)

    def test_tercer_dispositivo_409_con_lista(self):
        self.assertEqual(self.pedir()[0].status_code, 201)
        self.assertEqual(self.pedir()[0].status_code, 201)
        r, _ = self.pedir()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data['codigo'], 'limite_dispositivos')
        self.assertEqual(len(r.data['dispositivos']), 2)

    def test_mismo_dispositivo_no_cuenta_dos_veces(self):
        _, d1 = self.pedir()
        self.pedir()
        self.assertEqual(self.pedir(device=d1)[0].status_code, 201)

    def test_liberar_dispositivo_libera_cupo_y_revoca(self):
        r1, d1 = self.pedir()
        self.pedir()
        r = self.c.delete(f'/api/v1/offline/dispositivos/{d1}/')
        self.assertEqual(r.status_code, 204)
        self.assertEqual(self.pedir()[0].status_code, 201)
        lic = json.loads(r1.data['licencia'])['lic']
        ren = self.c.post(f'{URL}renovar/', {'device_id': d1, 'licencias': [lic]}, format='json')
        self.assertEqual(ren.data['resultados'][0]['estado'], 'revocada')

    def test_eliminar_descarga_propia(self):
        r1, d1 = self.pedir()
        lic = json.loads(r1.data['licencia'])['lic']
        self.assertEqual(self.c.delete(f'{URL}{lic}/?device_id={d1}').status_code, 204)
        self.assertTrue(LicenciaOffline.objects.get(pk=lic).revocada)

    def test_redescargar_tras_revocar_cambia_cek(self):
        r1, d1 = self.pedir()
        lic = json.loads(r1.data['licencia'])['lic']
        self.c.delete(f'{URL}{lic}/?device_id={d1}')
        r2, _ = self.pedir(device=d1)
        self.assertEqual(r2.status_code, 201)
        self.assertNotEqual(r1.data['cek'], r2.data['cek'])

    def test_licencia_inactiva_30_dias_no_ocupa_cupo(self):
        self.pedir()
        self.pedir()
        LicenciaOffline.objects.update(renovada_en=timezone.now() - timedelta(days=31))
        self.assertEqual(self.pedir()[0].status_code, 201)

    def test_lista_dispositivos(self):
        self.pedir()
        r = self.c.get('/api/v1/offline/dispositivos/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data['dispositivos']), 1)
        self.assertEqual(r.data['dispositivos'][0]['cursos'][0]['curso_id'], self.curso.id)

    def test_no_puede_liberar_dispositivo_ajeno(self):
        _, d1 = self.pedir()
        otro = make_user('otro@a.com', is_estudiante=True)
        r = self.cliente(otro).delete(f'/api/v1/offline/dispositivos/{d1}/')
        self.assertEqual(r.status_code, 404)


class RenovacionTests(OfflineBase):
    def setUp(self):
        super().setUp()
        self.ak = dar_acceso(self.al, self.curso, dias=30)
        r, self.device = self.pedir()
        self.lic = json.loads(r.data['licencia'])['lic']

    def renovar(self, device=None, lics=None):
        return self.c.post(f'{URL}renovar/', {
            'device_id': device or self.device, 'licencias': lics or [self.lic],
        }, format='json')

    def test_renueva_con_firma_nueva(self):
        ahora = timezone.now() + timedelta(days=3)
        with patch('django.utils.timezone.now', return_value=ahora):
            r = self.renovar()
        res = r.data['resultados'][0]
        self.assertEqual(res['estado'], 'vigente')
        lic = json.loads(res['licencia'])
        hasta = timezone.datetime.fromisoformat(lic['offline_hasta'].replace('Z', '+00:00'))
        self.assertAlmostEqual((hasta - ahora).total_seconds(), 7 * 86400, delta=60)
        self.assertNotIn('cek', res)

    def test_acceso_vencido_sin_acceso_y_revoca(self):
        with patch('django.utils.timezone.now', return_value=self.ak.valid_until + timedelta(seconds=1)):
            r = self.renovar()
        self.assertEqual(r.data['resultados'][0]['estado'], 'sin_acceso')
        self.assertTrue(LicenciaOffline.objects.get(pk=self.lic).revocada)

    def test_acceso_revocado_sin_acceso(self):
        self.ak.status = 'revoked'
        self.ak.save()
        self.assertEqual(self.renovar().data['resultados'][0]['estado'], 'sin_acceso')

    def test_otro_dispositivo_desconocida(self):
        r = self.renovar(device=str(uuid.uuid4()))
        self.assertEqual(r.data['resultados'][0]['estado'], 'desconocida')

    def test_manifest_requiere_device_correcto(self):
        ok = self.c.get(f'{URL}{self.lic}/manifest/?device_id={self.device}')
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(len(ok.data['lecciones']), 2)
        mal = self.c.get(f'{URL}{self.lic}/manifest/?device_id={uuid.uuid4()}')
        self.assertEqual(mal.status_code, 404)


class FirmaUrlsTests(OfflineBase):
    @override_settings(MEDIA_STORAGE='s3', MEDIA_S3_BUCKET='b', MEDIA_S3_PUBLIC_BASE_URL='https://cdn.x/')
    def test_manifest_firma_urls_del_bucket(self):
        dar_acceso(self.al, self.curso)

        class Fake:
            def generate_presigned_url(self, op, Params, ExpiresIn):
                return f"https://signed/{Params['Key']}?exp={ExpiresIn}"

        with patch('content_pipeline.media.storage._presign_client', Fake()):
            r, _ = self.pedir()
        img = next(a for a in r.data['manifest']['archivos'] if a['tipo'] == 'imagen')
        self.assertEqual(img['url'], f'https://signed/f1.png?exp={services.URL_EXPIRA_SEG}')
        self.assertEqual(img['url_original'], 'https://cdn.x/f1.png')

    def test_storage_local_no_firma(self):
        from content_pipeline.media.storage import url_firmada
        self.assertEqual(url_firmada('https://cdn.x/f1.png'), 'https://cdn.x/f1.png')


class ProgresoCompletadaEnTests(OfflineBase):
    def test_guarda_completada_en_y_no_acepta_futuro(self):
        dar_acceso(self.al, self.curso)
        antes = timezone.now() - timedelta(days=2)
        r = self.c.post('/api/v1/accounts/estudiante-leccion/', {
            'leccion': self.l1.id, 'curso': self.curso.id, 'completada_en': antes.isoformat(),
        }, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertAlmostEqual(
            EstudianteLeccion.objects.get(leccion=self.l1).completada_en.timestamp(), antes.timestamp(), delta=1)

        futuro = timezone.now() + timedelta(days=5)
        self.c.post('/api/v1/accounts/estudiante-leccion/', {
            'leccion': self.l2.id, 'curso': self.curso.id, 'completada_en': futuro.isoformat(),
        }, format='json')
        self.assertLessEqual(EstudianteLeccion.objects.get(leccion=self.l2).completada_en, timezone.now())
