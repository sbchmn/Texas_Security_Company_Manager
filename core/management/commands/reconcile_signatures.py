from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.document_signing import reconcile_signing_request
from core.models import SigningRequest


class Command(BaseCommand):
    help = "Verify outstanding DocuSeal submissions and file completed signed documents."

    def add_arguments(self, parser):
        parser.add_argument("--request", help="Check one signing request immediately.")

    def handle(self, *args, **options):
        pending = SigningRequest.objects.filter(
            status__in=[SigningRequest.Status.PREPARING, SigningRequest.Status.SENT],
            organization__signing_settings__enabled=True,
        )
        if options["request"]:
            try:
                pending = SigningRequest.objects.filter(pk=options["request"])
            except ValidationError as exc:
                raise CommandError("Invalid signing request ID.") from exc
            if not pending.exists():
                raise CommandError("Signing request not found.")
        else:
            pending = pending.filter(next_check_at__lte=timezone.now())
        checked = failed = 0
        for pk in pending.order_by("next_check_at").values_list("pk", flat=True)[:100]:
            try:
                reconcile_signing_request(pk)
            except ValidationError as exc:
                failed += 1
                self.stderr.write(f"{pk}: {' '.join(exc.messages)}")
            else:
                checked += 1
        self.stdout.write(f"checked={checked} failed={failed}")
        if failed:
            raise CommandError("Some signing requests could not be reconciled; their errors and retry times were recorded.")
