from django.core.management.base import BaseCommand
from core.services import queue_compliance_reminders
class Command(BaseCommand):
    help="Queue deduplicated credential expiration and invalid-status reminders."
    def handle(self,*args,**options): self.stdout.write(f"queued={queue_compliance_reminders()}")
