from django.core.management.base import BaseCommand

from core.attendance import reconcile_attendance


class Command(BaseCommand):
    help = "Raise and reconcile live late-arrival and overdue-departure cases without changing punches or pay."

    def handle(self, *args, **options):
        result = reconcile_attendance()
        self.stdout.write(f"opened={result['opened']} resolved={result['resolved']}")
