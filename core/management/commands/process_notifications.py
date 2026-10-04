from django.core.management.base import BaseCommand
from django.db import transaction
from core.models import Notification
from core.services import deliver_notification

RETRYABLE = [Notification.Status.QUEUED, Notification.Status.FAILED]

class Command(BaseCommand):
    help="Deliver queued or retryable notifications."
    def handle(self,*args,**options):
        sent=failed=blocked=0
        # Claim each notice under a row lock held for the provider call, so a scaled-out
        # second worker waits instead of sending the same email or SMS twice.
        for pk in Notification.objects.filter(status__in=RETRYABLE,attempts__lt=5).values_list("pk",flat=True)[:500]:
            with transaction.atomic():
                item=Notification.objects.select_for_update().select_related("organization","recipient").filter(pk=pk,status__in=RETRYABLE).first()
                if item is None: continue
                deliver_notification(item)
                if item.status==Notification.Status.SENT: sent+=1
                elif item.status==Notification.Status.BLOCKED:
                    # Not a delivery failure. A notice refused for consent or suppression was never sent
                    # to a carrier and will never succeed on a retry — counting it among failures would
                    # make a healthy queue look ill, and the number an operator reads here is the one
                    # they act on.
                    blocked+=1
                else: failed+=1
        self.stdout.write(f"sent={sent} failed={failed} blocked={blocked}")
