"""Audio de lecciones: un JSON sin audio no lo borra; ``import_course --solo-audio``."""
from __future__ import annotations

import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from content_pipeline.exporters.django_importer import import_generated_course
from schools.models import Leccion, LeccionRecurso

MANIFEST = {"curso": {"codigo": "AUD", "nombre": "Curso Audio", "descripcion": "d"},
            "unidades": [{"orden": 1, "nombre": "U1", "categoria": "General"}]}
FIG = {"tipo": "imagen", "rol": "figura", "clave": "F-aaa", "url": "http://cdn/f1.png", "orden": 1}
AUDIO = {"tipo": "audio", "rol": "narracion", "clave": "audio", "url": "http://cdn/a.mp3", "orden": 1000,
         "meta": {"duracion_seg": 120}}


def _lesson(nombre="Semáforos", posicion=1, **extra):
    return {"unidad_orden": 1, "nombre": nombre, "posicion": posicion, "tipo": "texto",
            "categoria": "General", "contenido": "Texto original.", "fuentes": [], **extra}


class ReimportSinAudioTests(TestCase):
    def test_json_sin_audio_no_borra_recurso_ni_transcripcion(self):
        import_generated_course(MANIFEST, [_lesson(recursos=[FIG, AUDIO], transcripcion="Guion.")])
        import_generated_course(MANIFEST, [_lesson(recursos=[FIG], contenido="Texto nuevo.")])
        lec = Leccion.objects.get(nombre="Semáforos")
        self.assertEqual(lec.contenido, "Texto nuevo.")
        self.assertEqual(lec.transcripcion, "Guion.")
        self.assertTrue(lec.recursos.filter(clave="audio", rol="narracion").exists())
        self.assertEqual(lec.url_audio, "http://cdn/a.mp3")

    def test_transcripcion_vacia_no_pisa_y_creacion_queda_vacia(self):
        import_generated_course(MANIFEST, [_lesson(transcripcion="")])
        self.assertEqual(Leccion.objects.get(nombre="Semáforos").transcripcion, "")
        Leccion.objects.filter(nombre="Semáforos").update(transcripcion="Guion BD.")
        import_generated_course(MANIFEST, [_lesson(transcripcion="   ")])
        self.assertEqual(Leccion.objects.get(nombre="Semáforos").transcripcion, "Guion BD.")
        import_generated_course(MANIFEST, [_lesson(transcripcion="Guion nuevo.")])
        self.assertEqual(Leccion.objects.get(nombre="Semáforos").transcripcion, "Guion nuevo.")

    def test_json_sin_recursos_ni_url_audio_conserva_audio(self):
        import_generated_course(MANIFEST, [_lesson(recursos=[AUDIO])])
        import_generated_course(MANIFEST, [_lesson()])          # JSON legacy sin recursos
        self.assertTrue(LeccionRecurso.objects.filter(clave="audio").exists())


class SoloAudioTests(TestCase):
    def setUp(self):
        import_generated_course(MANIFEST, [_lesson(recursos=[FIG]), _lesson("Pare", 2)])
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _run(self, lessons, *args):
        path = Path(self.tmp.name) / "curso.json"
        path.write_text(json.dumps({"manifest": MANIFEST, "lessons": lessons}), encoding="utf-8")
        out, err = StringIO(), StringIO()
        call_command("import_course", "--file", str(path), "--solo-audio", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def _json_con_audio(self):
        # El JSON trae otro contenido y sin figuras: --solo-audio no debe aplicarlo.
        return [_lesson(contenido="NO APLICAR", recursos=[AUDIO], transcripcion="Guion 1."),
                _lesson("Pare", 2, transcripcion="", recursos=[]),
                _lesson("Inexistente", 9, recursos=[AUDIO], transcripcion="X")]

    def test_actualiza_solo_audio_y_transcripcion(self):
        out, err = self._run(self._json_con_audio())
        lec = Leccion.objects.get(nombre="Semáforos")
        self.assertEqual(lec.contenido, "Texto original.")
        self.assertEqual(lec.transcripcion, "Guion 1.")
        self.assertEqual(sorted(lec.recursos.values_list("clave", flat=True)), ["F-aaa", "audio"])
        audio = lec.recursos.get(clave="audio")
        self.assertEqual((audio.tipo, audio.rol, audio.meta["duracion_seg"]), ("audio", "narracion", 120))
        self.assertEqual(lec.url_audio, "http://cdn/a.mp3")
        pare = Leccion.objects.get(nombre="Pare")
        self.assertEqual((pare.transcripcion, pare.recursos.count()), ("", 0))
        self.assertIn("recurso_create: 1", out)
        self.assertIn("leccion_no_encontrada: 1", out)
        self.assertIn("Inexistente", err)
        self.assertFalse(Leccion.objects.filter(nombre="Inexistente").exists())
        # Re-correr es idempotente; una URL nueva actualiza el recurso.
        out, _ = self._run(self._json_con_audio())
        self.assertIn("recurso_igual: 1", out)
        lessons = self._json_con_audio()
        lessons[0]["recursos"] = [{**AUDIO, "url": "http://cdn/a2.mp3"}]
        out, _ = self._run(lessons)
        self.assertIn("recurso_update: 1", out)
        self.assertEqual(Leccion.objects.get(nombre="Semáforos").url_audio, "http://cdn/a2.mp3")

    def test_dry_run_no_escribe(self):
        out, _ = self._run(self._json_con_audio(), "--dry-run")
        lec = Leccion.objects.get(nombre="Semáforos")
        self.assertEqual(lec.transcripcion, "")
        self.assertFalse(lec.recursos.filter(clave="audio").exists())
        self.assertIn("DRY-RUN recurso_create: 1", out)
        self.assertIn("DRY-RUN transcripcion_update: 1", out)

    def test_curso_inexistente(self):
        path = Path(self.tmp.name) / "otro.json"
        manifest = {**MANIFEST, "curso": {**MANIFEST["curso"], "codigo": "ZZZ"}}
        path.write_text(json.dumps({"manifest": manifest, "lessons": [_lesson(recursos=[AUDIO])]}),
                        encoding="utf-8")
        with self.assertRaises(CommandError):
            call_command("import_course", "--file", str(path), "--solo-audio", stdout=StringIO())
