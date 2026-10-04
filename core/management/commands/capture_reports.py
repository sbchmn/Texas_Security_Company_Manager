from django.core.management.base import BaseCommand
from core.services import capture_report_snapshots

class Command(BaseCommand):
    help="Store today's compliance, coverage and tour figures for the company and every branch and contract."

    def add_arguments(self, parser):
        parser.add_argument("--date", type=str, default=None,
                            help="Backfill a specific period (YYYY-MM-DD). Refuses to overwrite a day already captured.")

    def handle(self, *args, **options):
        from datetime import date
        from django.utils import timezone
        period = date.fromisoformat(options["date"]) if options["date"] else timezone.localdate()
        created = capture_report_snapshots(period_date=period)
        self.stdout.write(f"period={period} created={created}")
