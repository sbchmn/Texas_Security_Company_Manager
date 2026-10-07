"""Resolve inbox actions from producer-owned dedup keys, never from message text or URLs.

Notification has no metadata/target column. queue_notice appends the channel to its
producer key; the allowlist below follows those existing producers without changing
delivery or treating a read receipt as a workflow decision.
"""

import uuid

from django.urls import reverse
from django.utils import timezone

from .models import (
    AttendanceCase, Credential, CredentialRegistryCheck, ImportBatch, Membership, OnboardingTask,
    PayrollRun, Person, PersonDocument, Punch, PunchAdjustment, Shift, ShiftClaim,
    ShiftExchange, ShiftSwap, ShiftTemplate, SigningRequest, TimeOffRequest,
    TrainingRecord, record_open_for, record_readable,
)
from .scope import scope_for

MANAGERS = (
    Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR,
    Membership.Role.SCHEDULER, Membership.Role.SUPERVISOR,
)
PAYROLL = (Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.PAYROLL)
TIME_REVIEWERS = PAYROLL + (Membership.Role.SUPERVISOR,)

# Prefixes and event names are intentionally paired: arbitrary keys do not confer access.
TARGETS = {
    "attendance": (AttendanceCase, {"punch.late_arrival", "punch.overdue_departure"}),
    "shift-published": (Shift, {"shift.published"}),
    "shift-changed": (Shift, {"shift.changed"}),
    "shift-cancelled": (Shift, {"shift.cancelled"}),
    "shift-open": (Shift, {"shift.open"}),
    "shift-gap": (Shift, {"shift.coverage_gap"}),
    "shift-claim": (ShiftClaim, {"shift.claim_requested"}),
    "shift-claim-decision": (ShiftClaim, {"shift.claim_approved", "shift.claim_rejected"}),
    "punch.exception": (Punch, {"punch.exception"}),
    "punch.missing": (Shift, {"punch.missing"}),
    "punch.correction": (PunchAdjustment, {
        "punch.correction_requested", "punch.correction_approved", "punch.correction_rejected"}),
    "timeoff-requested": (TimeOffRequest, {"timeoff.requested"}),
    "timeoff-decision": (TimeOffRequest, {"timeoff.approved", "timeoff.rejected"}),
    "credential": (Credential, {"credential.reminder", "credential.escalated"}),
    "credential-missing": (Person, {"credential.missing"}),
    "credential-registry": (CredentialRegistryCheck, {"credential.registry_adverse"}),
    "training": (TrainingRecord, {"training.reminder"}),
    "onboarding-plan": (Person, {"onboarding.assigned"}),
    "onboarding-overdue": (OnboardingTask, {"onboarding.overdue"}),
    "onboarding-decision": (OnboardingTask, {"onboarding.done", "onboarding.waived"}),
    "onboarding-signing": (SigningRequest, {"onboarding.signature_requested"}),
    "onboarding-signed": (SigningRequest, {"onboarding.signed"}),
    "document-remind": (PersonDocument, {"document.acknowledgment_requested"}),
    "series": (ShiftTemplate, {"shift.published"}),
    "series-blocked": (ShiftTemplate, {"shift.series_blocked"}),
    "payroll.locked": (PayrollRun, {"payroll.locked"}),
    "payroll.reopened": (PayrollRun, {"payroll.reopened"}),
    "payroll.exported": (PayrollRun, {"payroll.exported"}),
    "import.completed": (ImportBatch, {"import.completed"}),
}
for prefix, event in (
    ("shift-swap", "offered"), ("shift-swap-raised", "offered"),
    ("shift-swap-agreed", "agreed"), ("shift-swap-answer", "agreed"),
    ("shift-swap-withdrawn", "withdrawn"), ("shift-swap-approved", "approved"),
    ("shift-swap-approved-requester", "approved"), ("shift-swap-refused", "refused"),
    ("shift-swap-closed", "refused"), ("shift-swap-expired", "expired"),
):
    TARGETS[prefix] = (ShiftSwap, {f"shift.swap_{event}"})
TARGETS["shift-swap-answer"][1].add("shift.swap_declined")
for prefix, event in (
    ("shift-exchange", "proposed"), ("shift-exchange-raised", "proposed"),
    ("shift-exchange-agreed", "agreed"), ("shift-exchange-accepted", "agreed"),
    ("shift-exchange-declined", "declined"), ("shift-exchange-withdrawn", "withdrawn"),
    ("shift-exchange-approved", "approved"), ("shift-exchange-refused", "refused"),
    ("shift-exchange-expired", "expired"),
):
    TARGETS[prefix] = (ShiftExchange, {f"shift.exchange_{event}"})
TARGETS["shift-exchange-closed"] = (ShiftExchange, {
    "shift.exchange_refused", "shift.exchange_approved"})


def notification_action(request, notification):
    """Return ``{"url": internal_url, "label": text}`` or None for this actor.

    Call with the tenant-resolved request. Recipient, active membership, role,
    target tenant and live object authority are all rechecked, even for old notices.
    Deleted/malformed/unknown targets have no speculative links.
    """
    membership = getattr(request, "membership", None)
    if (membership is None or not membership.active
            or membership.user_id != request.user.pk
            or membership.organization_id != request.organization.pk
            or notification.organization_id != request.organization.pk
            or notification.recipient_id != request.user.pk):
        return None
    parts = notification.deduplication_key.split(":")
    spec = TARGETS.get(parts[0])
    if not spec or len(parts) < 2 or notification.event_type not in spec[1]:
        return None
    try:
        target_id = uuid.UUID(parts[1])
    except (ValueError, TypeError, AttributeError):
        return None
    model = spec[0]
    target = model.objects.filter(organization=request.organization, pk=target_id).first()
    if target is None:
        return None
    role = membership.role
    scope = scope_for(request)
    person = request.organization.people.filter(user=request.user).first()
    own_id = person.pk if person else None

    def link(route, label, args=(), suffix=""):
        return {"url": reverse(route, args=args) + suffix, "label": label}

    def person_link(subject, tab):
        if subject.organization_id != request.organization.pk:
            return None
        # Match person_detail: self is not a bypass of a bounded manager's scope.
        if (subject.pk == own_id or role in MANAGERS) and scope.permits_person(subject):
            return link("person_detail", "Open related personnel step",
                        (subject.pk,), f"?tab={tab}")
        return None

    def shift_allowed(shift):
        return (shift.organization_id == request.organization.pk
                and shift.site.organization_id == request.organization.pk
                and shift.site.client.organization_id == request.organization.pk)

    if isinstance(target, AttendanceCase):
        if not shift_allowed(target.shift):
            return None
        if (role in MANAGERS and scope.permits_shift(target.shift)) or (role not in MANAGERS and target.person_id == own_id):
            return link("attendance_detail", "Follow up on attendance" if role in MANAGERS else "View attendance notice", (target.pk,))
        return None

    if isinstance(target, (ShiftSwap, ShiftExchange)):
        shifts = ([target.shift] if isinstance(target, ShiftSwap) else
                  [target.initiator_shift] + ([target.partner_shift] if target.partner_shift else []))
        parties = ([target.requester, target.replacement] if isinstance(target, ShiftSwap)
                   else [target.initiator, target.partner])
        if not all(shift_allowed(shift) for shift in shifts) or not all(
                party.organization.pk == request.organization.pk for party in parties):
            return None
        if own_id in {party.pk for party in parties}:
            kind = "swap" if isinstance(target, ShiftSwap) else "exchange"
            needs_consent = target.status in ("offered", "proposed") and parties[1].pk == own_id
            if target.status == "agreed":
                label = "Check pending manager review"
            elif needs_consent:
                label = "Respond to offer"
            elif target.status in ("offered", "proposed"):
                label = "Check colleague consent"
            else:
                label = "Check move outcome"
            if parties[0].pk == own_id:
                field = "requester" if isinstance(target, ShiftSwap) else "initiator"
                visible_ids = model.objects.filter(
                    organization=request.organization, **{field: person}).values_list("pk", flat=True)[:20]
                visible = target.pk in visible_ids
            elif needs_consent:
                visible = shifts[0].status == Shift.Status.PUBLISHED and shifts[0].starts_at > timezone.now()
            else:
                field = "replacement" if isinstance(target, ShiftSwap) else "partner"
                open_status = "offered" if isinstance(target, ShiftSwap) else "proposed"
                visible_ids = model.objects.filter(
                    organization=request.organization, **{field: person}).exclude(
                        status=open_status).values_list("pk", flat=True)[:20]
                visible = target.pk in visible_ids
            return link("my_shifts", label if visible else "Check your assigned roster",
                        suffix=f"#{kind}-{target.pk}" if visible else "")
        if role in MANAGERS and all(scope.permits_shift(shift) for shift in shifts):
            kind = "swap" if isinstance(target, ShiftSwap) else "exchange"
            return link("swaps", "Review hand-offs and trades",
                        suffix=f"?{kind}={target.pk}" if target.is_open else "")
        return None
    if isinstance(target, ShiftClaim):
        if not shift_allowed(target.shift):
            return None
        if person and target.officer.pk == own_id:
            return link("open_posts", "Check your post request")
        if role in MANAGERS and scope.permits_shift(target.shift) and scope.permits_person(target.officer):
            return link("shift_requests", "Review post request", (target.shift.pk,))
        return None
    if isinstance(target, Shift):
        if not shift_allowed(target):
            return None
        if notification.event_type == "punch.missing":
            if person and target.officer and target.officer.pk == own_id:
                return link("clock", "Check your timecard")
            if role in TIME_REVIEWERS and scope.permits_shift(target):
                return link("time_review", "Review missing punches")
            return None
        if person and target.officer and target.officer.pk == own_id:
            visible_ids = Shift.objects.filter(
                organization=request.organization, officer=person, ends_at__gt=timezone.now()
            ).exclude(status=Shift.Status.CANCELLED).order_by("starts_at").values_list("pk", flat=True)[:40]
            suffix = f"#shift-{target.pk}" if target.pk in visible_ids else ""
            return link("my_shifts", "Check your assigned roster", suffix=suffix)
        if notification.event_type == "shift.open" and target.officer is None and target.status == Shift.Status.PUBLISHED:
            return link("open_posts", "Check available posts")
        if role in MANAGERS and scope.permits_shift(target):
            return link("shift_requests", "Open related post", (target.pk,))
        return None
    if isinstance(target, (Punch, PunchAdjustment)):
        punch = target.punch if isinstance(target, PunchAdjustment) else target
        if punch.organization.pk != request.organization.pk or punch.person.organization.pk != request.organization.pk:
            return None
        if punch.person.pk == own_id:
            return person_link(punch.person, "time")
        if role in TIME_REVIEWERS and scope.permits_punch(punch):
            pending = (target.status == PunchAdjustment.Status.REQUESTED
                       if isinstance(target, PunchAdjustment) else punch.review_status == Punch.Review.PENDING)
            suffix = ("?punches=pending&adjustments=pending"
                      if pending
                      else "?punches=all&adjustments=history")
            return link("time_review", "Review timekeeping work", suffix=suffix)
        return None
    if isinstance(target, TimeOffRequest):
        if target.person.organization.pk != request.organization.pk:
            return None
        if target.person.pk == own_id:
            return link("my_time_off", "Check your time-off request")
        if role in MANAGERS and scope.permits_person(target.person):
            return link("time_off", "Review time-off request", suffix=f"?request={target.pk}")
        return None
    if isinstance(target, Person):
        return person_link(target, "credentials" if notification.event_type == "credential.missing" else "onboarding")
    if isinstance(target, (Credential, TrainingRecord, OnboardingTask, SigningRequest, CredentialRegistryCheck)):
        if isinstance(target, SigningRequest):
            subject = target.task.person
            tab = "onboarding"
            if target.task.organization.pk != request.organization.pk:
                return None
            if not record_open_for(target.document_type, subject.pk, role, own_id):
                return None
        elif isinstance(target, CredentialRegistryCheck):
            subject = target.credential.person
            tab = "credentials"
        else:
            subject = target.person
            tab = "credentials" if isinstance(target, Credential) else "training" if isinstance(target, TrainingRecord) else "onboarding"
        action = person_link(subject, tab)
        if action is None:
            return None
        if isinstance(target, SigningRequest):
            if role in (Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR) and subject.pk != own_id:
                from .workflow_actions import onboarding_actions
                visible = getattr(request, "_notification_signing_tasks", None)
                if visible is None:
                    visible = {row["task"].pk for row in onboarding_actions(request)["signing_rows"]}
                    request._notification_signing_tasks = visible
                if target.task.pk in visible:
                    return link("signing_queue", "Review signing workflow", suffix=f"?task={target.task.pk}")
                action["label"] = "Check signing task outcome"
            action["url"] += f"#task-{target.task.pk}"
        elif isinstance(target, OnboardingTask):
            action["url"] += f"#task-{target.pk}"
        # Never link directly to provider URLs or private signed documents.
        return action
    if isinstance(target, PersonDocument):
        if target.archived_at or target.deleted_at or not target.is_current or target.scan_status != PersonDocument.ScanStatus.CLEAN:
            return None
        if not record_readable(target, role, own_id):
            return None
        try:
            subject_id = uuid.UUID(parts[2])
        except (IndexError, ValueError):
            return None
        subject = Person.objects.filter(organization=request.organization, pk=subject_id).first()
        if subject is None:
            return None
        if subject.pk == own_id:
            return link("my_documents", "Review acknowledgment")
        if subject.user is None:
            return person_link(subject, "profile")
        return None
    if isinstance(target, ShiftTemplate):
        if role in MANAGERS and target.site.organization.pk == request.organization.pk and scope.permits_site(target.site):
            return link("shift_template_generate", "Check recurring post plan", (target.pk,))
        return None
    if isinstance(target, PayrollRun):
        if role in PAYROLL:
            return link("payroll", "Check payroll period", suffix=f"?run={target.pk}")
        return None
    if isinstance(target, ImportBatch):
        if role in (Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR):
            return link("imports", "Check import outcome")
    return None
