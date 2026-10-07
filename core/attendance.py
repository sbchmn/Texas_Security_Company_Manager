"""Live observations of absent punch evidence, never an adjustment to worked time."""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone

from .models import AttendanceCase, AttendanceCaseAction, AuditEvent, Membership, Person, Punch, PunchAdjustment, Shift
from .scope import dispatch_recipients_for_shift, for_membership
from .services import ResolvedClockPolicy, effective_clock_policy, queue_notice, record_hold_over
from .sms import moment_label, public_base_url, shift_when

MANAGERS = (Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR,
            Membership.Role.SCHEDULER, Membership.Role.SUPERVISOR)
LOOKBACK_DAYS = 7


def _evidence(organization):
    correction = PunchAdjustment.objects.filter(
        organization=organization, punch_id=OuterRef("pk"), status=PunchAdjustment.Status.APPROVED
    ).order_by("-created_at").values("proposed_at")[:1]
    return Punch.objects.filter(organization=organization).exclude(review_status=Punch.Review.REJECTED).annotate(
        attendance_at=Coalesce(Subquery(correction), F("occurred_at")))


def scoped_cases(organization, scope):
    shifts = scope.filter_shifts(organization.shifts.all()).values("pk")
    return organization.attendance_cases.filter(shift_id__in=shifts)


def departure_target(shift):
    latest = shift.hold_overs.filter(Q(officer_id=shift.officer_id) | Q(officer__isnull=True)).order_by("-sequence", "-created_at", "-pk").first()
    if latest is None:
        return shift.ends_at
    if latest.held_until is None and latest.expected_release_at is None:
        return None
    release = latest.expected_release_at or latest.held_until
    assert release is not None
    return max(shift.ends_at, release)


def _state(shift, kind, policy, now, punches):
    rule = policy.attendance_alert(kind)
    target = shift.starts_at if kind == AttendanceCase.Kind.ARRIVAL else departure_target(shift)
    deadline = target + timedelta(minutes=rule["grace_minutes"]) if target else None
    if shift.status != Shift.Status.PUBLISHED or shift.officer is None or shift.officer.status in Person.NON_WORKING_STATUSES:
        return False, deadline, rule, "Assignment is no longer active"
    arrivals = [at for kind, at in punches if kind == Punch.Kind.IN]
    if kind == AttendanceCase.Kind.ARRIVAL and arrivals:
        return False, deadline, rule, "Clock-in recorded"
    if kind == AttendanceCase.Kind.DEPARTURE:
        if not arrivals:
            return False, deadline, rule, "No clock-in recorded; review arrival or missing punches"
        latest_in = max(arrivals)
        if any(kind == Punch.Kind.OUT and at >= latest_in for kind, at in punches):
            return False, deadline, rule, "Clock-out recorded"
    if not rule["enabled"]:
        return False, deadline, rule, "Alert disabled by effective policy"
    if deadline is None:
        return False, deadline, rule, "Open hold-over recorded; awaiting relief without a release deadline"
    if now <= deadline:
        return False, deadline, rule, "Expected time has not passed; schedule or hold-over moved the deadline"
    if kind == AttendanceCase.Kind.ARRIVAL and now >= shift.ends_at:
        return False, deadline, rule, "Shift ended; review missing punches"
    return True, deadline, rule, ""


def _action(case, kind, note, actor=None, now=None):
    metadata = {"cycle": case.cycle, "deadline": case.deadline_at.isoformat(), "policy": case.policy_snapshot}
    action = AttendanceCaseAction.objects.create(
        case=case, actor=actor, kind=kind, note=note, metadata=metadata, created_at=now or timezone.now())
    AuditEvent.objects.create(organization=case.organization, actor=actor,
        action=f"attendance.{kind}", target_type="attendance_case", target_id=str(case.pk),
        metadata={"action_id": action.pk, "kind": case.kind, "shift": str(case.shift_id),
                  "person": str(case.person_id), "cycle": case.cycle, "note": note, **metadata})
    return action


def _resolve(case, reason, now):
    case.status = AttendanceCase.Status.RESOLVED
    case.resolved_at = now
    case.resolution = reason
    case.save(update_fields=["status", "resolved_at", "resolution"])
    _action(case, AttendanceCaseAction.Kind.RESOLVED, reason, now=now)


@transaction.atomic
def reconcile_shift(shift_id, *, now=None, create=True, policy=None):
    now = now or timezone.now()
    shift = (Shift.objects.select_for_update() if create else Shift.objects).get(pk=shift_id)
    officer = shift.officer
    cases = AttendanceCase.objects.filter(shift=shift).order_by("pk")
    if not create:
        # Punch inserts hold a shared parent FK lock. Lock cases, not an upgrade of that shift lock.
        cases = cases.select_for_update()
    existing = list(cases if not create else cases.select_related("organization", "person"))
    resolved = 0
    for case in existing:
        if case.status == AttendanceCase.Status.OPEN and (officer is None or case.person != officer):
            _resolve(case, "Assignment changed; case belongs to the previous officer", now)
            resolved += 1
    if officer is None:
        return {"opened": 0, "resolved": resolved}
    if not create and not any(case.status == AttendanceCase.Status.OPEN for case in existing):
        return {"opened": 0, "resolved": resolved}
    policy = policy or effective_clock_policy(shift.organization, shift.site)
    punches = list(_evidence(shift.organization).filter(shift=shift, person=officer, attendance_at__lte=now)
                   .values_list("kind", "attendance_at"))
    opened = 0
    for kind in AttendanceCase.Kind.values:
        due, deadline, rule, reason = _state(shift, kind, policy, now, punches)
        case = next((item for item in existing if item.person == officer and item.kind == kind), None)
        if not due:
            if case and case.status == AttendanceCase.Status.OPEN:
                _resolve(case, reason, now)
                resolved += 1
            continue
        if not create or (case and case.status == AttendanceCase.Status.OPEN):
            continue
        if case and case.manager_closed:
            continue
        assert deadline is not None
        if case:
            case.status = AttendanceCase.Status.OPEN
            case.resolved_at = None
            case.resolution = ""
            case.opened_at = now
            case.deadline_at = deadline
            case.policy_snapshot = rule
            case.cycle += 1
            case.save()
        else:
            case = AttendanceCase.objects.create(
                organization=shift.organization, shift=shift, person=officer,
                kind=kind, deadline_at=deadline, policy_snapshot=rule, opened_at=now)
        _action(case, AttendanceCaseAction.Kind.OPENED,
                f"No {'clock-in' if kind == 'arrival' else 'clock-out'} recorded after {moment_label(shift.organization, deadline)}.",
                now=now)
        recipients = dispatch_recipients_for_shift(shift, officer)
        user = officer.user
        if user and Membership.objects.filter(organization=shift.organization, user=user, active=True).exists():
            recipients.add(user.pk)
        event = "punch.late_arrival" if kind == AttendanceCase.Kind.ARRIVAL else "punch.overdue_departure"
        detail = ("No clock-in has been recorded. Confirm arrival and coverage; offline evidence may still be syncing."
                  if kind == AttendanceCase.Kind.ARRIVAL else
                  "No clock-out has been recorded after the expected departure. Confirm whether the officer is still working, arrange relief, or record an approved hold-over. Do not assume they left.")
        base = public_base_url(shift.organization)
        if base:
            detail += "\nFollow up: " + base + reverse("attendance_detail", args=[case.pk])
        queue_notice(organization=shift.organization, recipients=recipients, event_type=event,
            subject=f"{AttendanceCase.Kind(case.kind).label}: {officer.full_name}",
            body=f"{officer.full_name} at {shift.site.name}, {shift_when(shift)}.\n{detail}",
            dedup_key=f"attendance:{case.pk}:{case.cycle}", mandatory=True,
            subject_user_ids={user.pk} if user else (),
            sms={"shift": shift, "officer": officer, "time": moment_label(shift.organization, deadline),
                 "minutes": int((now - deadline).total_seconds() // 60), "case_id": case.pk})
        opened += 1
    return {"opened": opened, "resolved": resolved}


def reconcile_attendance(now=None, organizations=None):
    from .models import Organization, TimePolicy, TimePolicyOverride
    now = now or timezone.now()
    totals = {"opened": 0, "resolved": 0}
    for org in organizations if organizations is not None else Organization.objects.all():
        company = TimePolicy.objects.filter(organization=org).first()
        if company is None:
            continue
        overrides = list(TimePolicyOverride.objects.filter(organization=org).select_related("client", "site"))
        clients = {row.client.pk: row for row in overrides if row.client is not None}
        sites = {row.site.pk: row for row in overrides if row.site is not None}
        evidence = _evidence(org).filter(
            shift_id=OuterRef("pk"), person_id=OuterRef("officer_id"), attendance_at__lte=now)
        shifts = Shift.objects.filter(organization=org).annotate(
            last_in=Subquery(evidence.filter(kind=Punch.Kind.IN).order_by("-attendance_at").values("attendance_at")[:1]),
            last_out=Subquery(evidence.filter(kind=Punch.Kind.OUT).order_by("-attendance_at").values("attendance_at")[:1]),
        ).filter(
            Q(attendance_cases__status=AttendanceCase.Status.OPEN) |
            (Q(status=Shift.Status.PUBLISHED, officer__isnull=False,
               starts_at__lte=now, starts_at__gte=now - timedelta(days=LOOKBACK_DAYS)) &
             (Q(ends_at__gt=now, last_in__isnull=True) |
              (Q(last_in__isnull=False) & (Q(last_out__isnull=True) | Q(last_out__lt=F("last_in"))))))
        ).select_related("site__client").distinct()
        for shift in shifts.iterator(chunk_size=200):
            chain = []
            if shift.site.pk in sites:
                chain.append(("site", sites[shift.site.pk]))
            if shift.site.client.pk in clients:
                chain.append(("contract", clients[shift.site.client.pk]))
            chain.append(("company", company))
            result = reconcile_shift(shift.pk, now=now, policy=ResolvedClockPolicy(chain, company))
            for key in totals:
                totals[key] += result[key]
    return totals


@transaction.atomic
def follow_up(case, actor, kind, note, *, expected_release_at=None, reason=None, relief=None):
    # Lock the shift before the case, matching worker/punch reconciliation lock order.
    Shift.objects.select_for_update().get(pk=case.shift.pk)
    case = AttendanceCase.objects.select_for_update().get(pk=case.pk)
    member = Membership.objects.filter(organization=case.organization, user=actor, active=True).first()
    if member is None or member.role not in MANAGERS or not for_membership(member).permits_shift(case.shift):
        raise PermissionDenied
    reconcile_shift(case.shift.pk, create=False)
    case.refresh_from_db()
    if case.status != AttendanceCase.Status.OPEN:
        raise ValidationError("This case is already resolved. Refresh the queue to see current evidence.")
    note = (note or "").strip()
    if len(note) < 10 or len(note) > 2000:
        raise ValidationError("Record what happened in 10 to 2000 characters.")
    allowed = (AttendanceCaseAction.Kind.CONTACT, AttendanceCaseAction.Kind.RELIEF,
               AttendanceCaseAction.Kind.HOLD_OVER, AttendanceCaseAction.Kind.CLOSED)
    if kind not in allowed:
        raise ValidationError("Choose a follow-up action.")
    if kind == AttendanceCaseAction.Kind.HOLD_OVER:
        if case.kind != AttendanceCase.Kind.DEPARTURE:
            raise ValidationError("A hold-over applies to an overdue departure, not a late arrival.")
        if expected_release_at is None or expected_release_at <= timezone.now():
            raise ValidationError("Choose an expected release in the future.")
        if relief and (relief.organization != case.organization or not for_membership(member).permits_person(relief)):
            raise PermissionDenied
        record_hold_over(case.shift, reason, actor=actor, expected_release_at=expected_release_at,
                         relief=relief, note=note)
    if kind == AttendanceCaseAction.Kind.CLOSED:
        case.status = AttendanceCase.Status.RESOLVED
        case.resolved_at = timezone.now()
        case.resolution = f"Manager review: {note}"[:200]
        case.manager_closed = True
        case.save(update_fields=["status", "resolved_at", "resolution", "manager_closed"])
    if kind == AttendanceCaseAction.Kind.HOLD_OVER:
        note += f"\nExpected release: {moment_label(case.organization, expected_release_at)}. Reason: {reason}. Relief: {relief.full_name if relief else 'not yet assigned'}."
    return _action(case, kind, note, actor=actor)
