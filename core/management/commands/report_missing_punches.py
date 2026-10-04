from django.core.management.base import BaseCommand
from core.services import PUNCH_MISSING_LOOKBACK_DAYS, queue_missing_punch_reports

class Command(BaseCommand):
    help="Queue a notice for every published post that closed without a clock-in or clock-out."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=PUNCH_MISSING_LOOKBACK_DAYS, help="How far back to look.")

    def handle(self, *args, **options):
        queued = queue_missing_punch_reports(days=options["days"])
        self.stdout.write(f"queued={queued} window={options['days']}d")

