"""Datos: LeccionImagen y url_audio/url_video/url_pdf → LeccionRecurso.

Los campos ``url_*`` se conservan (espejo de compatibilidad); solo se copian.
"""
from django.db import migrations

PLACEHOLDER = "http://placeholder.url"


def _real(url):
    return bool(url) and url != PLACEHOLDER


def forwards(apps, schema_editor):
    Leccion = apps.get_model("schools", "Leccion")
    LeccionImagen = apps.get_model("schools", "LeccionImagen")
    LeccionRecurso = apps.get_model("schools", "LeccionRecurso")

    claves: dict[int, set[str]] = {}
    for img in LeccionImagen.objects.all().order_by("leccion_id", "orden", "id"):
        usadas = claves.setdefault(img.leccion_id, set())
        clave = f"F-{img.hash[:10]}" if img.hash else f"F-img{img.id}"
        if clave in usadas:
            clave = f"{clave}-{img.id}"
        usadas.add(clave)
        LeccionRecurso.objects.create(
            leccion_id=img.leccion_id, tipo="imagen", rol="figura", clave=clave, url=img.url,
            orden=img.orden, titulo="",
            meta={"pie": img.pie, "alt": img.alt, "pagina": img.pagina, "ancho": img.ancho,
                  "alto": img.alto, "origen": img.origen, "mapeo": img.mapeo, "hash": img.hash},
        )

    medios = (("url_audio", "audio", "narracion", "audio"),
              ("url_video", "video", "principal", "video"),
              ("url_pdf", "pdf", "descarga", "pdf"))
    for lec in Leccion.objects.all().only("id", "url_audio", "url_video", "url_pdf"):
        for campo, tipo, rol, clave in medios:
            url = getattr(lec, campo)
            if _real(url):
                LeccionRecurso.objects.get_or_create(
                    leccion_id=lec.id, clave=clave,
                    defaults={"tipo": tipo, "rol": rol, "url": url, "orden": 1000},
                )


class Migration(migrations.Migration):

    dependencies = [
        ('schools', '0025_leccion_recurso'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
