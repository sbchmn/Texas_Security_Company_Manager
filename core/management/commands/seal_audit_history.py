"""Enforce ``Organization.audit_retention_days`` the only way a hash chain allows (roadmap §6, REC-4).

The audit log is append-only in three places — the queryset, the model, and a MySQL trigger — and it has
to stay that way, because an event's meaning comes from the chain it is welded into. Retention cannot be
"delete the old rows": the first surviving event would name a predecessor that no longer exists and every
later reader would be right to call that tampering. So this command closes a period, writes the whole of
it out with its digests, and only then trims the live rows, leaving the seal behind as the head the chain
continues from.

Run it from the worker loop and retention is enforced. Run it with ``--dry-run`` and it says what it would
do, which is the right first move on a tenant that has never sealed anything: the first pass can cover
years of history.

    python manage.py seal_audit_history --dry-run
    python manage.py seal_audit_history --organization guardco
    python manage.py seal_audit_history --no-purge
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ...models import Organization
from ...services import (audit_periods_due, purge_sealed_audit, seal_audit_period, verify_audit_chain,
                         audit_retention_floor_note)


class Command(BaseCommand):
    help = "Archive audit periods past the retention window, then trim them from the live chain."

    def add_arguments(self, parser):
        parser.add_argument("--organization", help="Slug of one company. Default: every company, in turn.")
        parser.add_argument("--dry-run", action="store_true",
            help="Report the periods that are due and how many rows each holds. Writes nothing.")
        parser.add_argument("--no-purge", action="store_true",
            help="Seal the archive but keep the live rows. Use this for the first pass on an old tenant.")

    def handle(self, *args, **options):
        organizations = (Organization.objects.filter(slug=options["organization"]) if options["organization"]
                         else Organization.objects.order_by("slug"))
        if options["organization"] and not organizations.exists():
            raise CommandError(f"No company with the slug {options['organization']!r}.")
        now = timezone.now()
        sealed_rows = purged_rows = 0
        for organization in organizations:
            note = audit_retention_floor_note(organization)
            if note:
                self.stdout.write(self.style.WARNING(f"{organization.slug}: {note}"))
                continue
            periods, _ = audit_periods_due(organization, now)
            # Archived and untrimmed is a real state: `--no-purge`, a redaction that blocked the delete,
            # or a run that stopped between the two steps. Once a seal exists the due-date walk starts
            # *after* it, so a job that only sealed would leave the retention setting unenforced for as
            # long as the archive sat there — which is why the purge pass runs on its own, before sealing.
            stuck = list(organization.audit_seals.filter(status="sealed").order_by("period_start"))
            if not periods and not stuck:
                self.stdout.write(f"{organization.slug}: nothing due under {organization.audit_retention_days} days.")
                continue
            errors = verify_audit_chain(organization)
            if errors:
                # Reported per company rather than raised, so one tenant's broken chain cannot stop the
                # others' retention running — and so the operator sees which chain to go and explain.
                self.stdout.write(self.style.ERROR(
                    f"{organization.slug}: chain verification failed for {len(errors)} event(s). "
                    "Nothing sealed or purged until that is explained."))
                continue
            if options["dry_run"]:
                counts = [organization.audit_events.filter(occurred_at__gte=start, occurred_at__lt=end).count()
                          for start, end in periods]
                self.stdout.write(f"{organization.slug}: {len(periods)} period(s) due — "
                                  + ", ".join(f"{start:%Y-%m} {count} row(s)"
                                              for (start, _end), count in zip(periods, counts))
                                  + (f"; {len(stuck)} archived period(s) awaiting their purge" if stuck else "")
                                  + ". Nothing written (--dry-run).")
                sealed_rows += sum(counts)
                continue
            for pending in stuck:
                if options["no_purge"]:
                    self.stdout.write(self.style.WARNING(
                        f"{organization.slug}: {pending.period_start:%Y-%m} is archived; --no-purge leaves it live."))
                    continue
                try:
                    purge_sealed_audit(pending)
                except ValidationError as exc:
                    self.stdout.write(self.style.ERROR(f"{organization.slug}: {pending.period_start:%Y-%m} "
                                                      f"is not purged: {' '.join(exc.messages)}"))
                    continue
                purged_rows += pending.event_count
                self.stdout.write(self.style.SUCCESS(
                    f"{organization.slug}: purged the archived period {pending.period_start:%Y-%m} "
                    f"({pending.event_count} event(s)); the chain continues from its seal head."))
            for start, end in periods:
                seal = seal_audit_period(organization, start, end)
                sealed_rows += seal.event_count
                self.stdout.write(self.style.SUCCESS(
                    f"{organization.slug}: sealed {start:%Y-%m} → {end:%Y-%m}, {seal.event_count} event(s), "
                    f"sha256 {seal.archive_sha256[:12]}…"))
                if options["no_purge"]:
                    continue
                try:
                    purge_sealed_audit(seal)
                except ValidationError as exc:
                    self.stdout.write(self.style.ERROR(f"{organization.slug}: {start:%Y-%m} is archived "
                                                      f"but not purged: {' '.join(exc.messages)}"))
                    continue
                purged_rows += seal.event_count
                self.stdout.write(self.style.SUCCESS(
                    f"{organization.slug}: purged that period from the live chain; "
                    f"the chain now continues from seal {seal.pk}."))
        self.stdout.write(self.style.SUCCESS(
            f"sealed={sealed_rows} purged={purged_rows} dry_run={str(bool(options['dry_run'])).lower()} "
            f"purge={str(not bool(options['no_purge'])).lower()}"))
