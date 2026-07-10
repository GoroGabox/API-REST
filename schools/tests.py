from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Usuario
from schools.models import Categoria, Ejercicio, Escuela
from accounts.tests import make_user


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
        Ejercicio.objects.create(categoria=cat, pregunta="2+2", respuesta="4", opcion_a="4")

    def test_lista_ejercicios_oculta_respuesta(self):
        r = self.client.get('/api/v1/schools/exercices/')
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        data = r.data['results'] if isinstance(r.data, dict) and 'results' in r.data else r.data
        for ej in data:
            self.assertNotIn('respuesta', ej)


class VincularEstudianteTests(APITestCase):
    """Endpoint seguro para vincular/crear estudiantes a una escuela."""

    def setUp(self):
        self.escuela_a = Escuela.objects.create(
            nombre="A", direccion="x", email="a@a.com", telefono="1",
        )
        self.escuela_b = Escuela.objects.create(
            nombre="B", direccion="y", email="b@b.com", telefono="2",
        )
        self.admin = make_user("adm_vinc@x.com", is_admin=True)
        self.dir_a = make_user("dira_vinc@x.com", is_director=True, escuela=self.escuela_a)
        self.dir_b = make_user("dirb_vinc@x.com", is_director=True, escuela=self.escuela_b)
        self.est_a = make_user("ea_vinc@x.com", is_estudiante=True, escuela=self.escuela_a)
        self.est_b = make_user("eb_vinc@x.com", is_estudiante=True, escuela=self.escuela_b)

    def _post(self, payload):
        return self.client.post(
            "/api/v1/schools/vincular-estudiante/", payload, format="json",
        )

    def test_director_crea_nuevo_estudiante(self):
        self.client.force_authenticate(self.dir_a)
        r = self._post({"email": "nuevo@x.com", "nombre": "N", "apellido": "N"})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["status"], "created")
        u = Usuario.objects.get(email="nuevo@x.com")
        self.assertTrue(u.is_estudiante)
        self.assertTrue(u.is_active)
        self.assertEqual(u.escuela_id, self.escuela_a.id)

    def test_director_vincula_estudiante_existente_de_otra_escuela(self):
        self.client.force_authenticate(self.dir_a)
        r = self._post({"email": self.est_b.email})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], "linked")
        self.est_b.refresh_from_db()
        self.assertEqual(self.est_b.escuela_id, self.escuela_a.id)

    def test_ya_vinculado_devuelve_200(self):
        self.client.force_authenticate(self.dir_a)
        r = self._post({"email": self.est_a.email})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], "already_linked")

    def test_no_permite_vincular_administrativo(self):
        self.client.force_authenticate(self.dir_a)
        r = self._post({"email": self.dir_b.email})
        self.assertEqual(r.status_code, 403, r.data)

    def test_no_permite_vincular_admin(self):
        self.client.force_authenticate(self.dir_a)
        r = self._post({"email": self.admin.email})
        self.assertEqual(r.status_code, 403)

    def test_estudiante_obtiene_403(self):
        self.client.force_authenticate(self.est_a)
        r = self._post({"email": "nuevo@x.com"})
        self.assertEqual(r.status_code, 403)

    def test_admin_requiere_escuela_id(self):
        self.client.force_authenticate(self.admin)
        r = self._post({"email": "otra@x.com", "nombre": "X"})
        self.assertEqual(r.status_code, 400)

    def test_admin_vincula_con_escuela_id(self):
        self.client.force_authenticate(self.admin)
        r = self._post({"email": "otra@x.com", "nombre": "X", "escuela_id": self.escuela_b.id})
        self.assertEqual(r.status_code, 201, r.data)
        u = Usuario.objects.get(email="otra@x.com")
        self.assertEqual(u.escuela_id, self.escuela_b.id)

    def test_email_requerido(self):
        self.client.force_authenticate(self.dir_a)
        r = self._post({})
        self.assertEqual(r.status_code, 400)


class EscuelaDirectorPatchTests(APITestCase):
    """Director puede PATCH campos limitados de SU escuela; nunca los saldos."""

    def setUp(self):
        self.escuela_a = Escuela.objects.create(
            nombre="A", direccion="x", email="a@a.com", telefono="1",
            basic_key=10, professional_key=5,
        )
        self.escuela_b = Escuela.objects.create(
            nombre="B", direccion="y", email="b@b.com", telefono="2",
        )
        self.admin = make_user("adm_patch@x.com", is_admin=True)
        self.dir_a = make_user("dpa_patch@x.com", is_director=True, escuela=self.escuela_a)
        self.dir_b = make_user("dpb_patch@x.com", is_director=True, escuela=self.escuela_b)
        self.est_a = make_user("epa_patch@x.com", is_estudiante=True, escuela=self.escuela_a)

    def _patch(self, pk, payload):
        return self.client.patch(f"/api/v1/schools/schools/{pk}/", payload, format="json")

    def test_director_edita_su_escuela_campos_permitidos(self):
        self.client.force_authenticate(self.dir_a)
        r = self._patch(self.escuela_a.id, {"nombre": "AN", "email": "n@a.com"})
        self.assertEqual(r.status_code, 200, r.data)
        self.escuela_a.refresh_from_db()
        self.assertEqual(self.escuela_a.nombre, "AN")
        self.assertEqual(self.escuela_a.email, "n@a.com")

    def test_director_no_puede_modificar_saldos(self):
        self.client.force_authenticate(self.dir_a)
        r = self._patch(self.escuela_a.id, {"basic_key": 9999, "professional_key": 9999})
        self.assertEqual(r.status_code, 400)
        self.escuela_a.refresh_from_db()
        self.assertEqual(self.escuela_a.basic_key, 10)
        self.assertEqual(self.escuela_a.professional_key, 5)

    def test_director_saldos_ignorados_si_mezcla_con_campos_validos(self):
        self.client.force_authenticate(self.dir_a)
        r = self._patch(self.escuela_a.id, {"nombre": "AN2", "basic_key": 9999})
        self.assertEqual(r.status_code, 200)
        self.escuela_a.refresh_from_db()
        self.assertEqual(self.escuela_a.nombre, "AN2")
        self.assertEqual(self.escuela_a.basic_key, 10)

    def test_director_otra_escuela_403_o_404(self):
        self.client.force_authenticate(self.dir_a)
        r = self._patch(self.escuela_b.id, {"nombre": "hack"})
        self.assertIn(r.status_code, (403, 404))
        self.escuela_b.refresh_from_db()
        self.assertEqual(self.escuela_b.nombre, "B")

    def test_admin_puede_modificar_todo(self):
        self.client.force_authenticate(self.admin)
        r = self._patch(self.escuela_a.id, {"basic_key": 50, "professional_key": 20})
        self.assertEqual(r.status_code, 200, r.data)
        self.escuela_a.refresh_from_db()
        self.assertEqual(self.escuela_a.basic_key, 50)
        self.assertEqual(self.escuela_a.professional_key, 20)

    def test_estudiante_no_puede_patchear(self):
        self.client.force_authenticate(self.est_a)
        r = self._patch(self.escuela_a.id, {"nombre": "hack"})
        self.assertEqual(r.status_code, 403)


class EjercicioExplicacionTests(APITestCase):
    """Campo `explicacion` se expone en submit + detalle de prueba."""

    def setUp(self):
        from schools.models import Curso
        self.user = make_user("expl@x.com", is_estudiante=True)
        self.client.force_authenticate(self.user)
        self.curso = Curso.objects.create(nombre="C", descripcion="d")
        cat = Categoria.objects.create(nombre="XExp")
        for i in range(15):
            Ejercicio.objects.create(
                categoria=cat, curso=self.curso,
                pregunta="q" + str(i),
                opcion_a="OK", opcion_b="MAL",
                respuesta="OK",
                explicacion="exp-" + str(i),
            )

    def test_submit_devuelve_explicacion_en_cada_detalle(self):
        from django.urls import reverse
        r = self.client.post(
            reverse("generate_test"),
            {"tipo": "rapida", "modalidad": "practica", "curso_id": self.curso.id},
            format="json",
        )
        prueba_id = r.data["prueba_id"]
        preguntas = r.data["preguntas"]
        respuestas = {str(p["id"]): "a" for p in preguntas}
        r2 = self.client.post(
            reverse("submit_test", args=[prueba_id]),
            {"respuestas": respuestas}, format="json",
        )
        self.assertEqual(r2.status_code, 200, r2.data)
        for d in r2.data["detalles"]:
            self.assertIn("explicacion", d)
            self.assertIn("pregunta", d)
            self.assertTrue(d["explicacion"].startswith("exp-"))

    def test_detalle_prueba_incluye_explicacion(self):
        from django.urls import reverse
        r = self.client.post(
            reverse("generate_test"),
            {"tipo": "rapida", "modalidad": "practica", "curso_id": self.curso.id},
            format="json",
        )
        prueba_id = r.data["prueba_id"]
        preguntas = r.data["preguntas"]
        respuestas = {str(p["id"]): "a" for p in preguntas}
        self.client.post(
            reverse("submit_test", args=[prueba_id]),
            {"respuestas": respuestas}, format="json",
        )
        r_det = self.client.get(reverse("mis_pruebas_detalle", args=[prueba_id]))
        self.assertEqual(r_det.status_code, 200, r_det.data)
        for row in r_det.data["respuestas"]:
            self.assertIn("explicacion", row)


class ProgresoEstudianteDetalleTests(APITestCase):
    """GET /schools/<id>/estudiantes/<uid>/progreso/"""

    def setUp(self):
        from schools.models import Curso, Leccion
        from accounts.models import EstudianteLeccion, Prueba
        from sales.models import AccessKey, EstudianteCurso

        self.escuela_a = Escuela.objects.create(
            nombre="A", direccion="x", email="a@a.com", telefono="1",
        )
        self.escuela_b = Escuela.objects.create(
            nombre="B", direccion="y", email="b@b.com", telefono="2",
        )
        self.admin = make_user("adm_p@x.com", is_admin=True)
        self.dir_a = make_user("dir_pa@x.com", is_director=True, escuela=self.escuela_a)
        self.dir_b = make_user("dir_pb@x.com", is_director=True, escuela=self.escuela_b)
        self.est_a = make_user("est_pa@x.com", is_estudiante=True, escuela=self.escuela_a)

        self.curso = Curso.objects.create(nombre="C", descripcion="d")
        self.l1 = Leccion.objects.create(curso=self.curso, nombre="L1", posicion=1)
        self.l2 = Leccion.objects.create(curso=self.curso, nombre="L2", posicion=2)

        self.key = AccessKey.objects.create()
        EstudianteCurso.objects.create(
            estudiante_id=self.est_a, curso_id=self.curso, access_key_id=self.key,
        )
        EstudianteLeccion.objects.create(
            estudiante=self.est_a, leccion=self.l1, curso=self.curso,
        )
        Prueba.objects.create(estudiante=self.est_a, curso=self.curso, tipo="rapida", aprobado=True, score=80)

    def _get(self):
        return self.client.get(
            f"/api/v1/schools/{self.escuela_a.id}/estudiantes/{self.est_a.id}/progreso/"
        )

    def test_director_de_la_escuela_ve_progreso(self):
        self.client.force_authenticate(self.dir_a)
        r = self._get()
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["estudiante"]["id"], self.est_a.id)
        curso_info = r.data["cursos"][0]
        self.assertEqual(curso_info["curso_id"], self.curso.id)
        self.assertEqual(curso_info["lecciones_total"], 2)
        self.assertEqual(curso_info["lecciones_completadas"], 1)
        self.assertEqual(len(curso_info["pruebas"]), 1)

    def test_director_otra_escuela_403(self):
        self.client.force_authenticate(self.dir_b)
        r = self._get()
        self.assertEqual(r.status_code, 403)

    def test_admin_ve_cualquier_escuela(self):
        self.client.force_authenticate(self.admin)
        r = self._get()
        self.assertEqual(r.status_code, 200, r.data)

    def test_estudiante_solo_a_si_mismo(self):
        self.client.force_authenticate(self.est_a)
        r = self._get()
        self.assertEqual(r.status_code, 200)


class CertificadosPorEscuelaTests(APITestCase):
    """GET /schools/<id>/certificados/"""

    def setUp(self):
        from schools.models import Curso
        from accounts.models import Certificado

        self.escuela_a = Escuela.objects.create(
            nombre="A", direccion="x", email="a@a.com", telefono="1",
        )
        self.escuela_b = Escuela.objects.create(
            nombre="B", direccion="y", email="b@b.com", telefono="2",
        )
        self.admin = make_user("adm_c@x.com", is_admin=True)
        self.dir_a = make_user("dir_ca@x.com", is_director=True, escuela=self.escuela_a)
        self.dir_b = make_user("dir_cb@x.com", is_director=True, escuela=self.escuela_b)
        self.est_a = make_user("est_ca@x.com", is_estudiante=True, escuela=self.escuela_a)
        self.est_b = make_user("est_cb@x.com", is_estudiante=True, escuela=self.escuela_b)

        self.curso = Curso.objects.create(nombre="C", descripcion="d")
        Certificado.objects.create(estudiante=self.est_a, curso=self.curso)
        Certificado.objects.create(estudiante=self.est_b, curso=self.curso)

    def test_director_ve_certs_de_su_escuela(self):
        self.client.force_authenticate(self.dir_a)
        r = self.client.get(f"/api/v1/schools/{self.escuela_a.id}/certificados/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(r.data["results"][0]["estudiante"]["email"], self.est_a.email)

    def test_director_no_ve_otra_escuela(self):
        self.client.force_authenticate(self.dir_a)
        r = self.client.get(f"/api/v1/schools/{self.escuela_b.id}/certificados/")
        self.assertEqual(r.status_code, 403)

    def test_admin_ve_cualquier_escuela(self):
        self.client.force_authenticate(self.admin)
        r = self.client.get(f"/api/v1/schools/{self.escuela_b.id}/certificados/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["count"], 1)

    def test_filter_por_curso(self):
        self.client.force_authenticate(self.admin)
        r = self.client.get(f"/api/v1/schools/{self.escuela_a.id}/certificados/?curso={self.curso.id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["count"], 1)

    def test_estudiante_403(self):
        self.client.force_authenticate(self.est_a)
        r = self.client.get(f"/api/v1/schools/{self.escuela_a.id}/certificados/")
        self.assertEqual(r.status_code, 403)


class PasswordResetInviteTests(APITestCase):
    """POST /accounts/password-reset-invite/<user_id>/"""

    def setUp(self):
        self.escuela_a = Escuela.objects.create(
            nombre="A", direccion="x", email="a@a.com", telefono="1",
        )
        self.escuela_b = Escuela.objects.create(
            nombre="B", direccion="y", email="b@b.com", telefono="2",
        )
        self.admin = make_user("adm_i@x.com", is_admin=True)
        self.dir_a = make_user("dir_ia@x.com", is_director=True, escuela=self.escuela_a)
        self.dir_b = make_user("dir_ib@x.com", is_director=True, escuela=self.escuela_b)
        self.est_a = make_user("est_ia@x.com", is_estudiante=True, escuela=self.escuela_a)
        self.est_b = make_user("est_ib@x.com", is_estudiante=True, escuela=self.escuela_b)

    def test_director_invita_a_su_estudiante(self):
        self.client.force_authenticate(self.dir_a)
        r = self.client.post(f"/api/v1/accounts/password-reset-invite/{self.est_a.id}/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], "sent")

    def test_director_no_puede_invitar_otro_estudiante(self):
        self.client.force_authenticate(self.dir_a)
        r = self.client.post(f"/api/v1/accounts/password-reset-invite/{self.est_b.id}/")
        self.assertEqual(r.status_code, 403)

    def test_director_no_puede_invitar_a_administrativo(self):
        self.client.force_authenticate(self.dir_a)
        r = self.client.post(f"/api/v1/accounts/password-reset-invite/{self.dir_b.id}/")
        self.assertEqual(r.status_code, 403)

    def test_admin_invita_a_cualquiera(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post(f"/api/v1/accounts/password-reset-invite/{self.est_b.id}/")
        self.assertEqual(r.status_code, 200, r.data)

    def test_estudiante_403(self):
        self.client.force_authenticate(self.est_a)
        r = self.client.post(f"/api/v1/accounts/password-reset-invite/{self.est_b.id}/")
        self.assertEqual(r.status_code, 403)

    def test_usuario_inexistente_404(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/v1/accounts/password-reset-invite/99999/")
        self.assertEqual(r.status_code, 404)


class EjercicioBulkUploadTests(APITestCase):
    """Carga masiva de ejercicios desde .xlsx (bulk-upload / bulk-template)."""

    def setUp(self):
        from schools.models import Curso, Leccion
        self.admin = make_user("adm_bulk@x.com", is_admin=True)
        self.estudiante = make_user("est_bulk@x.com", is_estudiante=True)
        self.cat = Categoria.objects.create(nombre="Señales")
        self.curso = Curso.objects.create(nombre="Clase B", codigo="B1")
        self.leccion = Leccion.objects.create(
            curso=self.curso, categoria=self.cat, nombre="L1", posicion=1,
        )

    def _xlsx(self, rows):
        import io, openpyxl
        from schools.views import BULK_COLUMNS
        wb = openpyxl.Workbook(); ws = wb.active
        ws.append(BULK_COLUMNS)
        for r in rows:
            ws.append(r)
        buf = io.BytesIO(); wb.save(buf); buf.seek(0)
        return buf

    def _upload(self, buf, dry_run=False, skip_duplicates=False):
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = SimpleUploadedFile(
            "e.xlsx", buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        data = {"file": f}
        if dry_run:
            data["dry_run"] = "true"
        if skip_duplicates:
            data["skip_duplicates"] = "true"
        return self.client.post(
            "/api/v1/schools/exercices/bulk-upload/", data, format="multipart",
        )

    def test_crea_ejercicios_mapeando_letra_a_texto(self):
        self.client.force_authenticate(self.admin)
        buf = self._xlsx([
            [1, "Pregunta 1", "A opt", "B opt", "C opt", "D opt", "NULL", "NULL",
             "D", self.cat.id, self.curso.id, "NULL", "http://placeholder.url"],
        ])
        r = self._upload(buf)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["created"], 1)
        ej = Ejercicio.objects.get(pregunta="Pregunta 1")
        self.assertEqual(ej.respuesta, "D opt")   # letra D → texto de opcion_d
        self.assertIsNone(ej.leccion_id)          # "NULL" → None
        self.assertEqual(ej.opcion_e, None)

    def test_dry_run_no_escribe(self):
        self.client.force_authenticate(self.admin)
        buf = self._xlsx([
            [1, "Solo validar", "A", "B", "", "", "", "", "A", self.cat.id, "NULL", "NULL", ""],
        ])
        r = self._upload(buf, dry_run=True)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data["dry_run"])
        self.assertEqual(r.data["would_create"], 1)
        self.assertEqual(Ejercicio.objects.filter(pregunta="Solo validar").count(), 0)

    def test_reporta_errores_y_omite_filas_invalidas(self):
        self.client.force_authenticate(self.admin)
        buf = self._xlsx([
            ["", "Sin categoria", "A", "B", "", "", "", "", "A", 999999, "NULL", "NULL", ""],
            ["", "Buena", "A", "B", "", "", "", "", "B", self.cat.id, "NULL", "NULL", ""],
        ])
        r = self._upload(buf)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["created"], 1)
        self.assertEqual(r.data["errors"], 1)

    def test_estudiante_no_autorizado(self):
        self.client.force_authenticate(self.estudiante)
        buf = self._xlsx([[1, "x", "a", "b", "", "", "", "", "A", self.cat.id, "", "", ""]])
        r = self._upload(buf)
        self.assertIn(r.status_code, (401, 403))
        self.assertEqual(Ejercicio.objects.count(), 0)

    def test_skip_duplicates_salta_repetidos_en_bd_y_archivo(self):
        self.client.force_authenticate(self.admin)
        Ejercicio.objects.create(categoria=self.cat, pregunta="Ya existe", opcion_a="a", opcion_b="b", respuesta="a")
        buf = self._xlsx([
            ["", "Ya existe", "a", "b", "", "", "", "", "A", self.cat.id, "", "", ""],  # dup en BD
            ["", "Nueva P", "a", "b", "", "", "", "", "A", self.cat.id, "", "", ""],     # nueva
            ["", "Nueva P", "a", "b", "", "", "", "", "A", self.cat.id, "", "", ""],     # dup en archivo
        ])
        r = self._upload(buf, skip_duplicates=True)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["created"], 1)   # solo "Nueva P" una vez
        self.assertEqual(r.data["skipped"], 2)
        self.assertEqual(Ejercicio.objects.filter(pregunta="Nueva P").count(), 1)
        self.assertEqual(Ejercicio.objects.filter(pregunta="Ya existe").count(), 1)

    def test_sin_flag_permite_duplicados(self):
        self.client.force_authenticate(self.admin)
        buf = self._xlsx([
            ["", "Repe", "a", "b", "", "", "", "", "A", self.cat.id, "", "", ""],
            ["", "Repe", "a", "b", "", "", "", "", "A", self.cat.id, "", "", ""],
        ])
        r = self._upload(buf)  # sin skip_duplicates
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["created"], 2)
        self.assertEqual(Ejercicio.objects.filter(pregunta="Repe").count(), 2)

    def test_template_descarga_xlsx(self):
        self.client.force_authenticate(self.admin)
        r = self.client.get("/api/v1/schools/exercices/bulk-template/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r["Content-Type"])
