from django.core.management.base import BaseCommand

from core.leave import sync_account
from core.models import LeaveAccount


class Command(BaseCommand):
    help = "Idempotently record due leave grants, completed-period accrual and year-end expiry."

    def handle(self, *args, **options):
        for account in LeaveAccount.objects.select_related("organization").iterator():
            sync_account(account)
        self.stdout.write("Leave banks synchronized.")
