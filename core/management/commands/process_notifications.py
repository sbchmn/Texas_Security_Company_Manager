from django.core.management.base import BaseCommand
from core.models import Notification
from core.services import deliver_notification
class Command(BaseCommand):
    help="Deliver queued or retryable notifications."
    def handle(self,*args,**options):
        sent=failed=0
        for item in Notification.objects.filter(status__in=[Notification.Status.QUEUED,Notification.Status.FAILED],attempts__lt=5).select_related("organization","recipient")[:500]:
            deliver_notification(item)
            if item.status==Notification.Status.SENT: sent+=1
            else: failed+=1
        self.stdout.write(f"sent={sent} failed={failed}")
