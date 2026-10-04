from django.core.management.base import BaseCommand

from core.services import fill_active_series


class Command(BaseCommand):
    help="Fill every opted-in recurring series up to its horizon, and report the dates it could not place."

    def handle(self, *args, **options):
        created, blocked = fill_active_series()
        self.stdout.write(f"created={created} blocked={blocked}")
