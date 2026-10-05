"""Draft the Texas obligations into the control matrix (roadmap §8, CMP-1).

The matrix has held the *shape* of a rule since CMP-0 — jurisdiction, authority, interpretation,
effective dates, a renewal window, a reminder ladder, approval by a named actor — and has never held a
single obligation, because encoding a jurisdiction's numbers without the licensee approving them is the
thing this product refuses to do. This command writes the drafted rows from
`core/texas_rules.py`, where each one carries the primary text it came from and the date it was read.

Every row lands **unapproved**, which is not a cosmetic default: `is_approved` is what makes a rule
enforceable, and `unevaluated_reason` reports an unapproved row as "not approved, so not enforced yet".
A company that runs this and never reviews it gains a visible list and changes nothing about assignment
or the clock.

Re-running is safe. Rows are matched by code and an existing row is never touched, so a draft somebody
has started editing — or an approved obligation with a version history behind it — stays that company's
record.

    python manage.py seed_texas_obligations --dry-run
    python manage.py seed_texas_obligations --organization guardco
"""

from django.core.management.base import BaseCommand, CommandError

from ...models import Organization
from ...texas_rules import DUTIES, seed_texas_obligations


class Command(BaseCommand):
    help = "Write the drafted Texas compliance duties as unapproved rows, and their evidence types."

    def add_arguments(self, parser):
        parser.add_argument("--organization", help="Slug of one company. Default: every company, in turn.")
        parser.add_argument("--dry-run", action="store_true",
            help="List the duties that would be drafted for each company. Writes nothing.")

    def handle(self, *args, **options):
        organizations = (Organization.objects.filter(slug=options["organization"]) if options["organization"]
                         else Organization.objects.order_by("slug"))
        if options["organization"] and not organizations.exists():
            raise CommandError(f"No company with the slug {options['organization']!r}.")
        for organization in organizations:
            result = seed_texas_obligations(organization, None, dry_run=options["dry_run"])
            if not result["rules"]:
                self.stdout.write(f"{organization.slug}: all {len(DUTIES)} drafted duties are already "
                                  "present. Nothing changed.")
                continue
            self.stdout.write(self.style.SUCCESS(
                f"{organization.slug}: drafted {len(result['rules'])} duty(ies)"
                + (f" and {len(result['record_types'])} record type(s)" if result["record_types"] else "")
                + (" — nothing written (--dry-run)" if result["dry_run"] else "") + ": "
                + "; ".join(result["rules"])))
            if not result["dry_run"]:
                self.stdout.write(self.style.WARNING(
                    f"{organization.slug}: none of it is enforced. Each row needs approval by a named "
                    "person on /settings/compliance/ — read the interpretation, edit anything that is "
                    "wrong for this company, then approve."))
