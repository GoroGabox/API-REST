"""LeccionRecurso: espejo url_*, import por clave, compatibilidad, API y migración de datos."""
from __future__ import annotations

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from content_pipeline.exporters.django_importer import import_generated_course
from schools.models import Leccion, LeccionRecurso
from schools.serializers import LeccionDetalleSerializer
from schools.services import PLACEHOLDER_URL

MANIFEST = {"curso": {"codigo": "REC", "nombre": "Curso Rec", "descripcion": "d"},
            "unidades": [{"orden": 1, "nombre": "U1", "categoria": "General"}]}


def _lesson(**extra):
    return {"unidad_orden": 1, "nombre": "Semáforos", "posicion": 1, "tipo": "texto", "categoria": "General",
            "contenido": "Texto.\n\n{{figura:F-aaa}}\n\nMás texto.", "fuentes": [], **extra}


FIG = {"tipo": "imagen", "rol": "figura", "clave": "F-aaa", "url": "http://cdn/f1.png", "orden": 1,
       "meta": {"pie": "Semáforo", "alt": "Semáforo en rojo", "pagina": 22, "ancho": 100, "alto": 200}}
AUDIO = {"tipo": "audio", "rol": "narracion", "clave": "audio", "url": "http://cdn/a.mp3", "orden": 1000}


class ImportRecursosTests(TestCase):
    def _import(self, lesson):
        import_generated_course(MANIFEST, [lesson])
        return Leccion.objects.get(nombre="Semáforos")

    def test_recursos_upsert_replace_and_mirror(self):
        lec = self._import(_lesson(recursos=[FIG, AUDIO]))
        self.assertEqual(sorted(lec.recursos.values_list("clave", flat=True)), ["F-aaa", "audio"])
        self.assertEqual(lec.url_audio, "http://cdn/a.mp3")                       # espejo
        # Re-import sin el audio: se borra el recurso y el espejo vuelve a placeholder.
        lec = self._import(_lesson(recursos=[{**FIG, "meta": {"pie": "Nuevo pie"}}]))
        self.assertEqual(list(lec.recursos.values_list("clave", flat=True)), ["F-aaa"])
        self.assertEqual(lec.recursos.get().meta["pie"], "Nuevo pie")             # update, no duplica
        self.assertEqual(lec.url_audio, PLACEHOLDER_URL)

    def test_resource_without_url_keeps_published_one(self):
        self._import(_lesson(recursos=[FIG]))
        lec = self._import(_lesson(recursos=[{**FIG, "url": "", "archivo_local": "out/f.png"}]))
        self.assertEqual(lec.recursos.get(clave="F-aaa").url, "http://cdn/f1.png")

    def test_legacy_json_imagenes_and_url_fields(self):
        legacy = _lesson(url_audio="http://cdn/old.mp3", url_pdf="http://cdn/x.pdf",
                         imagenes=[{"url": "http://cdn/f.png", "orden": 1, "pie": "P", "hash": "abcdef1234567890"}])
        lec = self._import(legacy)
        tipos = dict(lec.recursos.values_list("clave", "tipo"))
        self.assertEqual(tipos, {"F-abcdef1234": "imagen", "audio": "audio", "pdf": "pdf"})
        self.assertEqual(lec.url_pdf, "http://cdn/x.pdf")
        # Un JSON viejo SIN imagenes no borra las figuras existentes.
        lec = self._import(_lesson())
        self.assertTrue(lec.recursos.filter(clave="F-abcdef1234").exists())

    def test_manual_url_not_overwritten_without_resource(self):
        lec = self._import(_lesson())
        Leccion.objects.filter(pk=lec.pk).update(url_video="http://youtube/x")
        lec.refresh_from_db()
        LeccionRecurso.objects.create(leccion=lec, tipo="imagen", rol="figura", clave="F-z", url="http://cdn/z.png")
        lec.refresh_from_db()
        self.assertEqual(lec.url_video, "http://youtube/x")                       # no hay recurso video

    def test_serializer_exposes_recursos_and_derived_imagenes(self):
        lec = self._import(_lesson(recursos=[FIG, AUDIO]))
        data = LeccionDetalleSerializer(lec).data
        self.assertEqual({r["clave"] for r in data["recursos"]}, {"F-aaa", "audio"})
        img = data["imagenes"][0]
        self.assertEqual((img["clave"], img["pie"], img["pagina"], img["ancho"]), ("F-aaa", "Semáforo", 22, 100))
        self.assertEqual(data["url_audio"], "http://cdn/a.mp3")


class MigrarMediosTests(TransactionTestCase):
    """0026: LeccionImagen + url_* → LeccionRecurso."""

    def test_data_migration(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("schools", "0025_leccion_recurso")])
        apps = executor.loader.project_state([("schools", "0025_leccion_recurso")]).apps
        Curso = apps.get_model("schools", "Curso")
        Leccion_ = apps.get_model("schools", "Leccion")
        LeccionImagen = apps.get_model("schools", "LeccionImagen")
        curso = Curso.objects.create(nombre="C", codigo="MIG", descripcion="d")
        lec = Leccion_.objects.create(curso=curso, nombre="L", posicion=1, url_audio="http://cdn/a.mp3")
        LeccionImagen.objects.create(leccion=lec, url="http://cdn/i.png", orden=2, pie="Pie", hash="0123456789abcdef")

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

        recs = {r.clave: r for r in LeccionRecurso.objects.filter(leccion_id=lec.id)}
        self.assertEqual(set(recs), {"F-0123456789", "audio"})
        self.assertEqual((recs["F-0123456789"].meta["pie"], recs["F-0123456789"].orden), ("Pie", 2))
        self.assertEqual((recs["audio"].tipo, recs["audio"].rol), ("audio", "narracion"))
