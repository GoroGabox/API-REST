"""Planes de estudio por escuela: capa sobre el curso base + costo de mantención.

Correr:  manage.py test schools.tests_plan_estudio
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import EstudianteLeccion
from accounts.services import elegibilidad_examen_final
from accounts.tests import make_user
from sales.testing import dar_cupo, dar_suscripcion
from schools import plan_estudio as pe
from schools.models import (
    CambioPlanEstudio, CostoMantencion, Curso, Escuela, Leccion, PlanEstudio, Unidad,
)

BASE = "/api/v1/schools"


def cliente(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def escuela(nombre):
    return Escuela.objects.create(nombre=nombre, direccion="x", email=f"{nombre}@e.com", telefono="1")


class PlanEstudioTests(TestCase):
    def setUp(self):
        self.curso = Curso.objects.create(nombre="Curso B", codigo="B", descripcion="d")
        self.u1 = Unidad.objects.create(curso=self.curso, nombre="Normas", orden=1)
        self.u2 = Unidad.objects.create(curso=self.curso, nombre="Señales", orden=2)
        self.l11 = Leccion.objects.create(curso=self.curso, unidad=self.u1, nombre="L1.1", posicion=1, tipo="texto")
        self.l12 = Leccion.objects.create(curso=self.curso, unidad=self.u1, nombre="L1.2", posicion=2, tipo="texto")
        self.l21 = Leccion.objects.create(curso=self.curso, unidad=self.u2, nombre="L2.1", posicion=1, tipo="texto")

        self.esc = escuela("A")
        self.sus = dar_suscripcion(self.esc, cupos=5)
        self.dir = make_user("dir@a.com", is_director=True, escuela=self.esc)
        self.alumno = make_user("al@a.com", is_estudiante=True, escuela=self.esc)
        dar_cupo(self.sus, self.alumno)

        self.otra = escuela("B")
        sus_b = dar_suscripcion(self.otra, cupos=5)
        self.alumno_b = make_user("al@b.com", is_estudiante=True, escuela=self.otra)
        dar_cupo(sus_b, self.alumno_b)

        self.c = cliente(self.dir)

    # ---------------------------------------------------------------- helpers
    def crear_plan(self):
        r = self.c.post(f"{BASE}/planes-estudio/", {"curso": self.curso.id, "codigo_resolucion": "Res 12"},
                        format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def ops(self, plan_id, *ops):
        r = self.c.post(f"{BASE}/planes-estudio/{plan_id}/ops/", {"ops": list(ops)}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def unidades(self, user):
        r = cliente(user).get(f"{BASE}/courses/{self.curso.id}/units/")
        self.assertEqual(r.status_code, 200, r.data)
        return [(u["nombre"], [l["nombre"] for l in u["lecciones"]]) for u in r.data]

    def pu(self, data, unidad_base):
        return next(u["plan_unidad_id"] for u in data["unidades"] if u["id"] == unidad_base.id)

    # ---------------------------------------------------------------- tests
    def test_sin_plan_es_el_curso_base(self):
        esperado = [("Normas", ["L1.1", "L1.2"]), ("Señales", ["L2.1"])]
        self.assertEqual(self.unidades(self.alumno), esperado)
        self.assertEqual(pe.lecciones_visibles_ids(self.curso.id, None), [self.l11.id, self.l12.id, self.l21.id])

    def test_borrador_no_afecta_al_alumno_y_activo_si(self):
        plan = self.crear_plan()
        self.ops(plan["id"],
                 {"op": "reordenar_unidades", "unidades": [self.pu(plan, self.u2), self.pu(plan, self.u1)]},
                 {"op": "ocultar", "leccion": self.l12.id},
                 {"op": "renombrar", "leccion": self.l11.id, "nombre": "Ley de tránsito"})
        self.assertEqual(self.unidades(self.alumno)[0][0], "Normas")  # borrador: base
        r = self.c.post(f"{BASE}/planes-estudio/{plan['id']}/activar/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.unidades(self.alumno), [("Señales", ["L2.1"]), ("Normas", ["Ley de tránsito"])])
        # Otra escuela y B2C siguen con el base.
        self.assertEqual(self.unidades(self.alumno_b)[0], ("Normas", ["L1.1", "L1.2"]))
        self.assertEqual(len(pe.lecciones_visibles_ids(self.curso.id, None)), 3)
        # El detalle de lecciones aplica el plan.
        r = cliente(self.alumno).get(f"{BASE}/lessons/", {"curso": self.curso.id})
        self.assertEqual({l["nombre"] for l in r.data}, {"Ley de tránsito", "L2.1"})

    def test_leccion_propia_solo_para_su_escuela(self):
        plan = self.crear_plan()
        r = self.c.post(f"{BASE}/planes-estudio/{plan['id']}/lecciones/",
                        {"unidad": self.pu(plan, self.u1), "nombre": "Normativa local",
                         "contenido": "## Desarrollo\n\n" + "texto " * 400, "posicion": 1}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        propia = Leccion.objects.get(pk=r.data["id"])
        self.assertEqual((propia.escuela_id, propia.unidad_id), (self.esc.id, None))
        self.c.post(f"{BASE}/planes-estudio/{plan['id']}/activar/")
        self.assertEqual(self.unidades(self.alumno)[0], ("Normas", ["Normativa local", "L1.1", "L1.2"]))
        self.assertNotIn("Normativa local", str(self.unidades(self.alumno_b)))
        ids_b = {l["id"] for l in cliente(self.alumno_b).get(f"{BASE}/lessons/").data}
        self.assertNotIn(propia.id, ids_b)
        self.assertEqual(cliente(self.alumno_b).get(f"{BASE}/lessons/{propia.id}/").status_code, 404)
        # El conteo base del catálogo no incluye propias.
        r = cliente(self.alumno).get(f"{BASE}/courses/{self.curso.id}/")
        self.assertEqual(r.data.get("cantidad_lecciones", 3), 3)
        # Costo: agregar lección = 5 + 1 por cada 1.000 caracteres.
        cambio = CambioPlanEstudio.objects.get(tipo="agregar_leccion")
        self.assertEqual(cambio.puntos, pe.puntos_para("agregar_leccion", cambio.caracteres))
        self.assertGreater(cambio.puntos, Decimal("7"))

    def test_contenido_invalido_rechazado(self):
        plan = self.crear_plan()
        r = self.c.post(f"{BASE}/planes-estudio/{plan['id']}/lecciones/",
                        {"unidad": self.pu(plan, self.u1), "nombre": "X",
                         "contenido": "## Desarrollo\n\n> [!INVENTADO]\n> hola"}, format="json")
        self.assertEqual((r.status_code, r.data["code"]), (400, "contenido_invalido"))

    def test_elegibilidad_cuenta_solo_visibles(self):
        plan = self.crear_plan()
        self.ops(plan["id"], {"op": "ocultar", "unidad": self.pu(plan, self.u2)})
        self.c.post(f"{BASE}/planes-estudio/{plan['id']}/activar/")
        for lec in (self.l11, self.l12):
            EstudianteLeccion.objects.create(estudiante=self.alumno, leccion=lec, curso=self.curso)
        e = elegibilidad_examen_final(self.alumno, self.curso)
        self.assertEqual((e["lecciones_total"], e["lecciones_completadas"]), (2, 2))
        self.assertNotEqual(e["razon"], "lecciones_pendientes")
        # Una lección oculta ya vista no suma.
        self.ops(plan["id"], {"op": "ocultar", "leccion": self.l12.id})
        e = elegibilidad_examen_final(self.alumno, self.curso)
        self.assertEqual((e["lecciones_total"], e["lecciones_completadas"]), (1, 1))

    def test_novedad_del_base(self):
        plan = self.crear_plan()
        nueva = Leccion.objects.create(curso=self.curso, unidad=self.u1, nombre="L1.3", posicion=3, tipo="texto")
        r = self.c.get(f"{BASE}/planes-estudio/{plan['id']}/")
        self.assertEqual(r.data["novedades"], 1)
        lec = next(l for u in r.data["unidades"] for l in u["lecciones"] if l["id"] == nueva.id)
        self.assertTrue(lec["nueva_base"])
        data = self.ops(plan["id"], {"op": "aceptar_novedad", "leccion": nueva.id})
        self.assertEqual(data["novedades"], 0)
        self.assertTrue(CambioPlanEstudio.objects.filter(tipo="aceptar_novedad").exists())

    def test_base_eliminada_queda_registrada(self):
        plan = self.crear_plan()
        lid = self.l12.id
        self.l12.delete()
        c = CambioPlanEstudio.objects.get(tipo="base_eliminada")
        self.assertEqual((c.origen, c.objeto_id, c.plan_id), ("sistema", lid, plan["id"]))

    def test_prune_no_borra_lecciones_propias(self):
        from content_pipeline.exporters.django_importer import import_a2_course
        plan = self.crear_plan()
        propia = pe.crear_leccion_propia(PlanEstudio.objects.get(pk=plan["id"]),
                                         unidad=self.pu(plan, self.u1), nombre="Propia", contenido="")
        manifest = {"curso": {"nombre": "Curso B", "codigo": "B", "descripcion": "d"},
                    "unidades": [{"orden": 1, "nombre": "Normas"}, {"orden": 2, "nombre": "Señales"}]}
        lessons = [
            {"unidad_orden": 1, "posicion": 1, "nombre": "L1.1", "tipo": "texto", "contenido": "x"},
            {"unidad_orden": 2, "posicion": 1, "nombre": "L2.1", "tipo": "texto", "contenido": "x"},
        ]
        l12 = self.l12.id
        import_a2_course(manifest, lessons, prune=True)
        self.assertTrue(Leccion.objects.filter(pk=propia.id).exists())
        self.assertFalse(Leccion.objects.filter(pk=l12).exists())
        self.assertTrue(CambioPlanEstudio.objects.filter(tipo="base_eliminada", objeto_id=l12).exists())

    def test_coalesce_ediciones(self):
        plan = self.crear_plan()
        p = PlanEstudio.objects.get(pk=plan["id"])
        lec = pe.crear_leccion_propia(p, unidad=self.pu(plan, self.u1), nombre="Propia", contenido="a",
                                      usuario=self.dir)
        for i in range(3):
            r = self.c.patch(f"{BASE}/planes-estudio/{p.id}/lecciones/{lec.id}/",
                             {"contenido": "a" + "b" * (500 * (i + 1))}, format="json")
            self.assertEqual(r.status_code, 200, r.data)
        ediciones = CambioPlanEstudio.objects.filter(tipo="editar_leccion")
        self.assertEqual(ediciones.count(), 1)
        self.assertEqual(ediciones[0].caracteres, 1500)
        # Fuera de la ventana: evento nuevo.
        ahora = timezone.now() + pe.VENTANA_COALESCER + timedelta(minutes=1)
        with mock.patch("django.utils.timezone.now", return_value=ahora):
            self.c.patch(f"{BASE}/planes-estudio/{p.id}/lecciones/{lec.id}/", {"contenido": "zz"}, format="json")
        self.assertEqual(CambioPlanEstudio.objects.filter(tipo="editar_leccion").count(), 2)

    def test_eliminar_propia_con_avance_solo_oculta(self):
        plan = self.crear_plan()
        p = PlanEstudio.objects.get(pk=plan["id"])
        lec = pe.crear_leccion_propia(p, unidad=self.pu(plan, self.u1), nombre="Propia", contenido="")
        EstudianteLeccion.objects.create(estudiante=self.alumno, leccion=lec, curso=self.curso)
        r = self.c.delete(f"{BASE}/planes-estudio/{p.id}/lecciones/{lec.id}/")
        self.assertEqual(r.data["resultado"], "oculta")
        self.assertTrue(Leccion.objects.filter(pk=lec.id).exists())

    def test_ops_atomicas(self):
        plan = self.crear_plan()
        r = self.c.post(f"{BASE}/planes-estudio/{plan['id']}/ops/", {"ops": [
            {"op": "ocultar", "leccion": self.l11.id},
            {"op": "quitar_unidad", "unidad": self.pu(plan, self.u1)},   # base: no se puede
        ]}, format="json")
        self.assertEqual((r.status_code, r.data["op_indice"]), (400, 1))
        self.assertFalse(CambioPlanEstudio.objects.filter(tipo="ocultar").exists())

    def test_director_no_edita_plan_de_otra_escuela(self):
        plan = self.crear_plan()
        dir_b = make_user("dir@b.com", is_director=True, escuela=self.otra)
        r = cliente(dir_b).post(f"{BASE}/planes-estudio/{plan['id']}/ops/",
                                {"ops": [{"op": "ocultar", "leccion": self.l11.id}]}, format="json")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(cliente(self.alumno).get(f"{BASE}/planes-estudio/").status_code, 403)
        # Plan duplicado → 409.
        r = self.c.post(f"{BASE}/planes-estudio/", {"curso": self.curso.id}, format="json")
        self.assertEqual(r.status_code, 409)

    def test_reporte_admin_suma_puntos_horas_y_montos(self):
        plan = self.crear_plan()
        self.ops(plan["id"], {"op": "ocultar", "leccion": self.l11.id},
                 {"op": "agregar_unidad", "nombre": "Normativa comunal"})
        admin = make_user("admin@x.com", is_admin=True)
        ca = cliente(admin)
        r = ca.post(f"{BASE}/admin/mantencion/{self.esc.id}/costos/",
                    {"tipo": "horas_staff", "minutos": 90, "monto_clp": 30000, "descripcion": "revisión"},
                    format="json")
        self.assertEqual(r.status_code, 201, r.data)
        r = ca.get(f"{BASE}/admin/mantencion/")
        fila = next(f for f in r.data["escuelas"] if f["escuela_id"] == self.esc.id)
        self.assertEqual(fila["puntos"], 3.0)          # ocultar 1 + agregar_unidad 2
        self.assertEqual((fila["horas_staff"], fila["monto_clp"]), (1.5, 30000))
        self.assertEqual((fila["planes"], fila["por_tipo"]["ocultar"]), (1, 1))
        csv = ca.get(f"{BASE}/admin/mantencion/", {"formato": "csv"})
        self.assertIn(b"escuela_id", csv.content)
        r = ca.get(f"{BASE}/admin/mantencion/{self.esc.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data["costos"]), 1)
        self.assertEqual(CostoMantencion.objects.count(), 1)
        self.assertEqual(cliente(self.dir).get(f"{BASE}/admin/mantencion/").status_code, 403)

    def test_offline_manifest_usa_el_plan(self):
        from offline.services import manifiesto
        plan = self.crear_plan()
        p = PlanEstudio.objects.get(pk=plan["id"])
        propia = pe.crear_leccion_propia(p, unidad=self.pu(plan, self.u2), nombre="Propia", contenido="")
        self.ops(plan["id"], {"op": "ocultar", "leccion": self.l11.id})
        pe.activar_plan(p)
        req = mock.Mock(user=self.alumno)
        m = manifiesto(self.curso, req)
        ids = [l["id"] for l in m["lecciones"]]
        self.assertIn(propia.id, ids)
        self.assertNotIn(self.l11.id, ids)
        req_b = mock.Mock(user=self.alumno_b)
        self.assertNotIn(propia.id, [l["id"] for l in manifiesto(self.curso, req_b)["lecciones"]])
