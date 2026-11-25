"""Tarea diaria de suscripciones (cron de Railway, una vez al día).

    python manage.py tareas_suscripciones

Avisos de vencimiento (7/3/1 días), gracia y suspensión; libera los cupos de
suscripciones vencidas.
Idempotente: correrla más de una vez el mismo día no duplica correos.
"""
from django.core.management.base import BaseCommand

from sales.services import devolver_llaves_no_usadas
from sales.suscripciones import tareas_diarias


class Command(BaseCommand):
    help = "Avisos y mantenimiento diario de suscripciones de escuelas."

    def handle(self, *args, **options):
        r = tareas_diarias()
        # Mismo cron: devuelve las llaves asignadas que el alumno nunca usó.
        devueltas = devolver_llaves_no_usadas()
        self.stdout.write(self.style.SUCCESS(
            f"Avisos: {r['avisos']} · escuelas con cupos liberados: {r['cupos_liberados']}"
            f" · llaves devueltas: {sum(f['llaves'] for f in devueltas)}"
        ))
