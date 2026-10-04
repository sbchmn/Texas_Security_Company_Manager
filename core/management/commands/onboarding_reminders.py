from django.core.management.base import BaseCommand
from core.models import Organization, Person
from core.services import provision_onboarding_tasks, queue_onboarding_assignment, queue_onboarding_reminders


class Command(BaseCommand):
    """Issue the checklist to whoever has none, then chase the steps that are past their date.

    Kept separate from ``compliance_reminders`` because the two ladders answer different questions:
    that one ages a credential toward an expiry date, this one ages a *step* against a date derived
    from a hire date that a person-editing screen can still change. They share nothing but the
    schedule.

    Both passes are idempotent. ``(person, item)`` is unique, so a company that defines a step six
    weeks after hiring somebody gets exactly one new task for them and not a second copy of the
    checklist; and the chase deduplicates on the step and its date, so running this every minute of a
    day sends one notice per late step. An officer with no sign-in is still issued to — the office
    owns some steps — but is not chased, because there is nowhere for the notice to go.
    """

    help = "Hand out missing onboarding steps and chase the ones past their date."

    def add_arguments(self, parser):
        parser.add_argument("--no-issue", action="store_true", help="Chase only; do not hand out missing steps.")

    def handle(self, *args, **options):
        issued = 0
        notified = 0
        people_reached = 0
        for organization in Organization.objects.all():
            roster = organization.people.filter(
                status__in=[Person.Status.ONBOARDING, Person.Status.ACTIVE])
            if not options["no_issue"]:
                owed = roster.exclude(pk__in=organization.onboarding_tasks.values_list("person_id", flat=True))
                for person in owed.select_related("user"):
                    created = provision_onboarding_tasks(person)
                    if not created:
                        continue
                    issued += len(created)
                    people_reached += 1
                    notified += queue_onboarding_assignment(person, created)
        chased = queue_onboarding_reminders()
        self.stdout.write(f"issued={issued} people={people_reached} assignment_notices={notified} "
                          f"chased={chased} issue_pass={'off' if options['no_issue'] else 'on'}")
