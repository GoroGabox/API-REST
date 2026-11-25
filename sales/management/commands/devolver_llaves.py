"""Devuelve al saldo de la escuela las llaves asignadas que el estudiante nunca usó.

    python manage.py devolver_llaves [--dry-run] [--escuela <id>]

Una llave asignada por un director a un estudiante con escuela, vencida y cuyo
curso el estudiante nunca abrió, vuelve a `Escuela.basic_key` (ver
`sales.services.devolver_llaves_no_usadas`). Idempotente. También la corre la
tarea diaria `tareas_suscripciones`.
"""
from django.core.management.base import BaseCommand

from sales.services import devolver_llaves_no_usadas


class Command(BaseCommand):
    help = "Devuelve las llaves vencidas que el estudiante nunca usó."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Solo lista, no devuelve.")
        parser.add_argument("--escuela", type=int, default=None, help="Solo esta escuela.")

    def handle(self, *args, **options):
        dry = options["dry_run"]
        filas = devolver_llaves_no_usadas(escuela_id=options["escuela"], dry_run=dry)
        for f in filas:
            self.stdout.write(f"  escuela {f['escuela_id']}: +{f['llaves']} · {f['estudiante']} · {f['curso']}")
        total = sum(f["llaves"] for f in filas)
        verbo = "Se devolverían" if dry else "Devueltas"
        self.stdout.write(self.style.SUCCESS(f"{verbo} {total} llave(s) de {len(filas)} asignación(es)."))
