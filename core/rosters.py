from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .models import AuditEvent, Membership, Person, PersonnelAssignment, Shift, TimePolicy


def pay_period_bounds(organization, at=None, offset=0):
    policy = TimePolicy.objects.filter(organization=organization).first()
    zone = ZoneInfo(policy.timezone if policy else organization.timezone)
    weekday = policy.workweek_start if policy else 0
    weeks = policy.pay_period_weeks if policy else 1
    anchor = policy.pay_period_anchor if policy else None
    if anchor is None:
        if weeks != 1:
            raise ValidationError("Configure the pay-period anchor in Time and payroll policy.")
        anchor = date(2000, 1, 3) + timedelta(days=weekday)
    moment = at or timezone.now()
    day = moment.astimezone(zone).date()
    length = timedelta(weeks=weeks)
    start_day = anchor + length * (((day - anchor).days // length.days) + offset)
    return (timezone.make_aware(datetime.combine(start_day, time.min), zone),
            timezone.make_aware(datetime.combine(start_day + length, time.min), zone))


def roster_people(organization_id, site_ids, client_ids, now=None):
    """Explicit assignments and their grace period replace perpetual shift-derived access."""
    now = now or timezone.now()
    assignments = PersonnelAssignment.objects.filter(organization_id=organization_id, person_id=OuterRef("pk"))
    reachable = Q(site_id__in=site_ids) | Q(client_id__in=client_ids) | Q(client__sites__id__in=site_ids)
    visible = assignments.filter(reachable).filter(Q(active=True) | Q(roster_access_until__gt=now))
    # Legacy shifts still grant visibility only where no explicit roster lifecycle exists.
    lifecycle = PersonnelAssignment.objects.filter(organization_id=organization_id,
        person_id=OuterRef("officer_id")).filter(Q(site_id=OuterRef("site_id")) | Q(client_id=OuterRef("site__client_id")))
    legacy = Shift.objects.filter(organization_id=organization_id, officer_id=OuterRef("pk"),
        site_id__in=site_ids).annotate(has_lifecycle=Exists(lifecycle)).filter(has_lifecycle=False)
    return Person.objects.filter(organization_id=organization_id).annotate(
        roster_visible=Exists(visible), legacy_visible=Exists(legacy)).filter(Q(roster_visible=True) | Q(legacy_visible=True))


def roster_scheduling_refusal(shift, officer):
    if not shift.site_id or shift.starts_at is None or shift.ends_at is None:
        return None
    targets = officer.roster_assignments.filter(organization_id=shift.organization_id).filter(
        Q(site_id=shift.site_id) | Q(client_id=shift.site.client_id))
    if targets.filter(active=True).exists() or not targets.filter(active=False).exists():
        return None
    # Existing assignments may be managed, but not moved, extended, restored from cancellation,
    # or reassigned to a removed officer under the guise of editing an old post.
    original = Shift.objects.filter(pk=shift.pk, organization_id=shift.organization_id,
        officer=officer, site_id=shift.site_id).exclude(status=Shift.Status.CANCELLED).first()
    if (original and shift.status != Shift.Status.CANCELLED
            and original.starts_at == shift.starts_at and original.ends_at >= shift.ends_at):
        return None
    return "Employee was removed from this client/site roster. Reassign them on their personnel file before scheduling another shift."


def _assignment_actor(organization, actor):
    if not organization.memberships.filter(user=actor, active=True,
            role__in=(Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR)).exists():
        raise PermissionDenied


@transaction.atomic
def add_roster_assignments(person, clients, sites, actor):
    _assignment_actor(person.organization, actor)
    person = Person.objects.select_for_update().get(pk=person.pk)
    changes = []
    for target, field in [(target, "client") for target in clients] + [(target, "site") for target in sites]:
        if target.organization_id != person.organization_id or not target.active:
            raise ValidationError("Choose an active client/site in this company.")
        row, created = PersonnelAssignment.objects.get_or_create(person=person, **{field: target},
            defaults={"organization": person.organization})
        if created or not row.active:
            row.active = True
            row.assigned_at = timezone.now()
            row.removed_at = row.last_shift_end = row.roster_access_until = None
            row.full_clean()
            row.save()
            AuditEvent.objects.create(organization=person.organization, actor=actor,
                action="person.roster_assigned", target_type="person", target_id=str(person.pk),
                metadata={"assignment": row.pk, "target": row.label, "reactivated": not created})
            changes.append(row)
    return changes


@transaction.atomic
def remove_roster_assignment(person, assignment_id, actor):
    _assignment_actor(person.organization, actor)
    person = Person.objects.select_for_update().get(pk=person.pk)
    row = person.roster_assignments.select_for_update().get(pk=assignment_id, organization=person.organization)
    if not row.active:
        raise ValidationError("This assignment is already removed.")
    now = timezone.now()
    shifts = person.shifts.exclude(status=Shift.Status.CANCELLED)
    shifts = shifts.filter(site=row.site) if row.site_id else shifts.filter(site__client=row.client)
    last_end = shifts.order_by("-ends_at").values_list("ends_at", flat=True).first()
    # Shift ends are exclusive: midnight belongs to the period the tour just finished.
    reference = last_end - timedelta(microseconds=1) if last_end else now
    row.active = False
    row.removed_at = now
    row.last_shift_end = last_end
    row.roster_access_until = pay_period_bounds(person.organization, reference, offset=1)[1]
    row.full_clean()
    row.save()
    AuditEvent.objects.create(organization=person.organization, actor=actor,
        action="person.roster_removed", target_type="person", target_id=str(person.pk),
        metadata={"assignment": row.pk, "target": row.label,
                  "last_shift_end": last_end.isoformat() if last_end else None,
                  "roster_access_until": row.roster_access_until.isoformat()})
    return row
