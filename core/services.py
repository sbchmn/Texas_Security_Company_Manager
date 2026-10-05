import csv
import base64
import hashlib
import hmac
import json
import re
import uuid
import math
from pathlib import Path
from collections import defaultdict, namedtuple
from datetime import date, datetime, timedelta
# Named apart from `django.utils.timezone`, which is imported below as `timezone` and no longer
# re-exports a UTC sentinel of its own.
from datetime import timezone as utc_reference
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from io import BytesIO, StringIO
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import connection, transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone
from .scope import ActorScope, dispatch_recipients_for_shift, manager_recipients_by_person
from .models import (
    AUDIENCE_ROLES, AuditEvent, AuditRedaction, AuditSeal, audit_event_hash, audit_seal_path, AvailabilityRule, ChannelAudience, ChannelRule, ClockKiosk, ComplianceRule, Credential, CredentialRegistryCheck, CredentialType,
    DeliveryEvent, MessageConsent, Suppression,
    DocumentAcknowledgment, DocumentType,
    Membership, Notification, OnboardingItem, OnboardingTask, Organization, PayCategory, PayCode, Person, PersonDocument, Punch, PunchAdjustment, ShiftHourDesignation,
    record_open_for, record_visibility_filter, ReportSnapshot, RuleRevision,
    Shift, ShiftExchange, ShiftSwap, ShiftTemplate, Site, TimeOffRequest, TimePolicy,
    TimePolicyOverride, TrainingRecord, HoldOver, SERIES_MAX_DAYS, WEEKDAY_LABELS,
)

# The two states in which a move still needs an answer. Kept here rather than in views so the
# expiry sweep, the approval queue and the overview count the same set of rows.
SWAP_OPEN_STATUSES = (ShiftSwap.Status.OFFERED, ShiftSwap.Status.AGREED)
EXCHANGE_OPEN_STATUSES = (ShiftExchange.Status.PROPOSED, ShiftExchange.Status.AGREED)

INVALID_CREDENTIAL_STATES = {"missing", "pending", "unverified", "expired", "suspended", "revoked"}
# States that mean the person is not currently authorized to work security in Texas at all.
# A shift defines which credentials a post requires, so "missing"/"pending"/"unverified" are
# enforced through shift_eligibility; these three are enforced on every clock-in for any
# credential type the installation marks as clock-gating (blocks_clock_in).
PROHIBITED_CREDENTIAL_STATES = {"expired", "suspended", "revoked"}
# Offline capture queues a punch, it never invents one; anything beyond this is device drift.
CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)

def parse_punch_timestamp(value):
    """Punch evidence must state its own offset; a naive timestamp is guesswork about the
    worker's location, so it is rejected rather than silently interpreted."""
    if not isinstance(value,str) or not value.strip():
        raise ValidationError("occurred_at is required.")
    try:
        parsed=datetime.fromisoformat(value.strip().replace("Z","+00:00"))
    except ValueError as exc:
        raise ValidationError("occurred_at must be an ISO-8601 timestamp with a UTC offset.") from exc
    if parsed.tzinfo is None:
        raise ValidationError("occurred_at must include a UTC offset.")
    return timezone.localtime(parsed)

def prohibited_credentials(person):
    blocked=[]
    credentials=Credential.objects.filter(person=person,credential_type__active=True,credential_type__blocks_clock_in=True).select_related("credential_type")
    for credential in credentials:
        if credential.effective_status in PROHIBITED_CREDENTIAL_STATES:
            blocked.append(f"{credential.credential_type.name}: {credential.effective_status}.")
    return blocked

def credential_obligations(person, types, held):
    """Every applicable requirement for one person, whether or not a row exists.

    "Missing" is only knowable from the requirement side: a guard who never had a
    commitment certificate recorded produces no Credential row, so a queue built by
    iterating credentials can never show them. ``held`` is keyed by credential type id and
    is expected to come from a prefetch cache, keeping this one query per page.
    """
    obligations=[]
    for credential_type in types:
        if not credential_type.applies_to_person(person):
            continue
        credential=held.get(credential_type.id)
        state=credential.effective_status if credential else Credential.Status.MISSING
        obligations.append((credential_type, credential, state))
    return obligations

def post_requirements(shift, posted=None):
    """Every credential the post demands, widest scope first, with where it came from.

    The contract and the site can require a registration of anyone who stands the post; the
    dispatcher should not have to re-tick it on each shift, because forgetting to is exactly how
    an unqualified officer ends up on a guard post. Only the post's *own* set comes from the form
    being validated, which is why ``posted`` replaces that layer and not the other two.
    """
    layers=[]
    client=None
    site=None
    if shift.site_id:
        site=getattr(shift,"site",None)
        client=getattr(site,"client",None) if site is not None else None
    if client is not None:
        layers.append((client.required_credentials.all(),"contract"))
    if site is not None:
        layers.append((site.required_credentials.all(),"site"))
    layers.append((posted if posted is not None else shift.required_credentials.all(),"post"))
    found=[]; seen=set()
    for queryset,origin in layers:
        for item in queryset:
            if item.id in seen:
                continue
            seen.add(item.id); found.append((item,origin))
    return found


def effective_pay_code(shift):
    """Which job code the hours on this post are paid under, and the level that said so.

    Post, then site, then contract — the same order the rates resolve in, because a dispatcher
    should not have to learn a second precedence rule for a second column. ``source`` is reported
    beside the code for the same reason it is beside the rate: when a client disputes which code
    an invoice line belongs to, "the site set it" is the answer that ends the argument.
    """
    site = getattr(shift, "site", None) if shift.site_id else None
    client = getattr(site, "client", None) if site is not None else None
    for code, origin in ((shift.pay_code, "post"),
                         (getattr(site, "default_pay_code", None) if site else None, "site"),
                         (getattr(client, "default_pay_code", None) if client else None, "contract")):
        if code is not None:
            return {"pay_code": code.code, "pay_code_name": code.name, "cost_centre": code.cost_centre or "",
                    "pay_code_source": origin}
    return {"pay_code": "", "pay_code_name": "", "cost_centre": "", "pay_code_source": ""}


def effective_rates(shift, officer=None):
    """Resolve the pay and bill rate for one post across the scopes that may set it.

    Order is post, site, contract, then the officer's own rate — so a holiday-premium shift can
    override a contract default without editing either. ``*_source`` is reported next to the
    number because "who is paying this rate" is the first question a client dispute asks.
    ``officer`` overrides the assigned person for timecard rows, where the hours belong to
    whoever punched them rather than to the name on the assignment.
    """
    site=getattr(shift,"site",None) if shift.site_id else None
    client=getattr(site,"client",None) if site is not None else None
    person=officer if officer is not None else shift.officer
    pay_candidates=((shift.pay_rate,"post"),(site.default_pay_rate if site else None,"site"),(client.default_pay_rate if client else None,"contract"),(person.hourly_rate if person else None,"officer"))
    bill_candidates=((shift.bill_rate,"post"),(site.default_bill_rate if site else None,"site"),(client.default_bill_rate if client else None,"contract"))

    def first(candidates):
        for value,origin in candidates:
            if value is not None:
                return Decimal(value),origin
        return None,""
    pay,pay_source=first(pay_candidates)
    bill,bill_source=first(bill_candidates)
    return {"pay_rate":pay,"pay_source":pay_source,"bill_rate":bill,"bill_source":bill_source}


def shift_eligibility(shift, officer=None, purpose="schedule", requirements=None):
    """Whether ``officer`` may stand ``shift`` right now.

    ``requirements`` accepts a pre-resolved list from :func:`post_requirements` so a caller that
    tests the same post against a whole roster (open-post candidates) resolves the inheritance
    once instead of once per officer.
    """
    officer = officer or shift.officer
    reasons=[]
    if not officer:
        return False,["No officer is assigned."]
    if officer.organization_id != shift.organization_id:
        return False,["Officer belongs to another organization."]
    credentials={c.credential_type_id:c for c in officer.credentials.select_related("credential_type")}
    for required,origin in (post_requirements(shift) if requirements is None else requirements):
        if purpose == "schedule" and not required.blocks_scheduling: continue
        if purpose == "clock" and not required.blocks_clock_in: continue
        credential=credentials.get(required.id)
        state=credential.effective_status if credential else "missing"
        if state in INVALID_CREDENTIAL_STATES:
            # Naming the layer that imposed the rule is what makes a refusal actionable rather
            # than a dead end for a dispatcher who can only see the post.
            reasons.append(f"{required.name}: {state}." + ("" if origin=="post" else f" Required by the {origin}."))
    if purpose == "schedule":
        # Approved leave blocks the *assignment*. It never blocks the clock: a guard called back
        # in on their own day off produced time that was worked, and the punch is the evidence.
        leave=officer.time_off_requests.filter(status=TimeOffRequest.Status.APPROVED,starts_at__lt=shift.ends_at,ends_at__gt=shift.starts_at).first()
        if leave:
            reasons.append(f"On approved leave {leave.starts_at:%b %d} to {leave.ends_at:%b %d}.")
    overlap=officer.shifts.exclude(pk=shift.pk).exclude(status=Shift.Status.CANCELLED).filter(starts_at__lt=shift.ends_at,ends_at__gt=shift.starts_at).exists()
    if overlap: reasons.append("Officer has an overlapping shift.")
    return not reasons,reasons


def shift_advisories(shift, officer=None):
    """What a dispatcher should be *told* about a post, as distinct from refused.

    Availability and the overtime threshold are advisory by design. A coverage gap is not solved
    by hiding the only officer who could fill it, and a premium hour is lawful — it costs money,
    and it has to be visible before the post is stood rather than after the invoice. Each
    warning names what to do about it.
    """
    officer = officer or shift.officer
    advisories=[]
    if not officer or not shift.starts_at or not shift.ends_at:
        return advisories
    windows=list(officer.availability_rules.all())
    if windows and not any(rule.covers(shift.starts_at) for rule in windows):
        stated=", ".join(str(rule) for rule in windows)
        advisories.append(f"{officer.full_name} has no availability window covering {timezone.localtime(shift.starts_at):%a %H:%M} (stated: {stated}).")
    leave=officer.time_off_requests.filter(status=TimeOffRequest.Status.APPROVED,starts_at__lt=shift.ends_at,ends_at__gt=shift.starts_at).first()
    if leave:
        advisories.append(f"{officer.full_name} is on approved leave {leave.starts_at:%b %d} to {leave.ends_at:%b %d}; assigning this post overrides it.")
    overtime=projected_week_hours(shift, officer)
    if overtime:
        hours,threshold,week_start=overtime
        advisories.append(f"This post brings the week of {week_start} to about {hours} hours, over the {threshold}-hour threshold: the excess is premium time.")
    return advisories


def projected_week_hours(shift, officer):
    """Scheduled hours for the officer in the configured workweek that contains this post.

    The estimate counts whole shifts that start inside the week, which is how the payroll side
    already attributes a day to a week; it is labelled "about" because rounding and breaks are
    timecard facts, not schedule facts.
    """
    try:
        policy=TimePolicy.objects.get(organization=shift.organization)
    except TimePolicy.DoesNotExist:
        return None
    local_start=timezone.localtime(shift.starts_at)
    week_start=local_start.date()-timedelta(days=(local_start.date().weekday()-policy.workweek_start)%7)
    week_end=week_start+timedelta(days=7)
    bounds=(timezone.make_aware(datetime.combine(week_start,datetime.min.time())),
            timezone.make_aware(datetime.combine(week_end,datetime.min.time())))
    total=timedelta()
    posts=officer.shifts.exclude(status=Shift.Status.CANCELLED).exclude(pk=shift.pk)
    for other in posts:
        starts=other.starts_at if other.starts_at.tzinfo else timezone.make_aware(other.starts_at)
        if bounds[0]<=starts< bounds[1]:
            total+=other.ends_at-other.starts_at
    total+=shift.ends_at-shift.starts_at
    hours=round(total.total_seconds()/3600,2)
    threshold=policy.overtime_after_hours
    if hours>float(threshold):
        return hours,threshold,week_start
    return None

def recurring_plan(template, range_start, range_end, status):
    """Every dated post a series would produce, and the dates its own gates would refuse.

    Generation runs the same two checks a dispatcher meets when they place a post by hand — the
    standing officer must qualify for it, and the site cannot be stood twice in the same hour —
    and reports a blocked date instead of quietly leaving a hole the dispatcher would only find
    on the night. Nothing is written here: the screen shows this plan and
    :func:`apply_recurring_plan` re-derives it before creating rows, so a registration that
    lapses while the preview is on screen is still refused.

    Returns ``{"rows": [...], "count": int, "weeks": [...], "notes": [...]}`` where each row names
    one occurrence with a ``created`` verdict, the reasons it is blocked, and notes worth showing
    beside it, and ``count`` is how many rows would actually be written.
    """
    organization = template.organization
    posted = list(template.required_credentials.all())
    plan_end = range_end + timedelta(days=1 if template.overnight else 0)
    occupied = list(Shift.objects.filter(organization=organization, site=template.site,
        starts_at__lt=timezone.make_aware(datetime.combine(plan_end, datetime.max.time())),
        ends_at__gte=timezone.make_aware(datetime.combine(range_start, datetime.min.time())),
        ).exclude(status=Shift.Status.CANCELLED).values_list("starts_at", "ends_at"))
    generated = set(Shift.objects.filter(organization=organization, template=template).values_list("starts_at", flat=True))
    rows = []
    accepted = []
    now = timezone.now()
    for starts_at, ends_at in template.occurrences(range_start, range_end):
        row = {"starts_at": starts_at, "ends_at": ends_at, "created": True, "reasons": [], "notes": []}
        rows.append(row)
        if starts_at in generated:
            row["created"] = False
            row["reasons"].append("Already generated from this series.")
            continue
        if any(other[0] < ends_at and other[1] > starts_at for other in occupied):
            row["created"] = False
            row["reasons"].append("Another post already stands this site across that time.")
            continue
        if starts_at <= now:
            row["notes"].append("Already elapsed.")
        officer = template.officer
        if officer is None:
            accepted.append((starts_at, ends_at))
            continue
        probe = Shift(organization=organization, site_id=template.site_id, starts_at=starts_at,
                      ends_at=ends_at, post_name=template.post_name, status=status, officer=officer)
        reasons = shift_eligibility(probe, officer=officer, requirements=post_requirements(probe, posted))[1]
        if any(window[0] < ends_at and window[1] > starts_at for window in accepted):
            reasons.append("Two posts in this series stand the same officer at the same time.")
        if reasons:
            row["created"] = False
            row["reasons"] = reasons
        else:
            accepted.append((starts_at, ends_at))
    return {"rows": rows, "count": sum(1 for row in rows if row["created"]),
            "weeks": _plan_week_hours(template, rows), "notes": _plan_series_notes(template, rows)}


def _plan_series_notes(template, rows):
    """What the whole series says about the standing officer's stated availability.

    A weekly pattern has one answer per weekday, so availability is read once for the series and
    named per day rather than re-derived for all fourteen occurrences — and unlike the per-post
    advisory at assignment, a dispatcher deciding a pattern needs to know which *day* is wrong.
    Approved leave is not here because it blocks the row outright (``shift_eligibility``).
    """
    officer = template.officer
    if officer is None or not any(row["created"] for row in rows):
        return []
    windows = list(officer.availability_rules.all())
    if not windows:
        return []
    covers = {}
    for index in template.day_indexes:
        moments = [row["starts_at"] for row in rows if row["created"]
                   and timezone.localtime(row["starts_at"]).weekday() == index]
        covers[index] = any(rule.covers(moment) for rule in windows for moment in moments)
    missed = [WEEKDAY_LABELS[index] for index, covered in covers.items() if not covered]
    if not missed:
        return []
    return [f"{officer.full_name} has stated no availability window covering "
            + ", ".join(missed) + f" at {template.window_label}. The posts are still generated — "
            "availability is advisory, not a refusal."]


def _plan_week_hours(template, rows):
    """Hours this plan adds to the standing officer's workweek, against the overtime threshold.

    A series is where the overtime decision actually gets made — one Tuesday is a warning at
    assignment, fourteen Tuesdays is a pay bill — and the per-post advisory cannot see it because
    the earlier rows of the batch are not in the database yet. Weeks are attributed the way
    ``projected_week_hours`` does it, from the configured workweek start, so the number here and
    the number on the timecard agree about which week an hour belongs to.
    """
    if template.officer_id is None:
        return []
    try:
        policy = TimePolicy.objects.get(organization=template.organization)
    except TimePolicy.DoesNotExist:
        return []
    threshold = policy.overtime_after_hours
    def week_of(moment):
        day = timezone.localtime(moment).date()
        return day - timedelta(days=(day.weekday() - policy.workweek_start) % 7)
    def minutes(moment_a, moment_b):
        return int((moment_b - moment_a).total_seconds() // 60)
    added = {}
    for row in rows:
        if row["created"]:
            added[week_of(row["starts_at"])] = added.get(week_of(row["starts_at"]), 0) + minutes(row["starts_at"], row["ends_at"])
    stood = {}
    for other in template.officer.shifts.exclude(status=Shift.Status.CANCELLED):
        week = week_of(other.starts_at)
        stood[week] = stood.get(week, 0) + minutes(other.starts_at, other.ends_at)
    weeks = []
    for week in sorted(added):
        hours = (Decimal(added[week] + stood.get(week, 0)) / Decimal(60)).quantize(Decimal("0.01"))
        weeks.append({"week_start": week, "series_hours": (Decimal(added[week]) / Decimal(60)).quantize(Decimal("0.01")),
                      "total_hours": hours, "threshold": threshold, "over": hours > threshold})
    return weeks


@transaction.atomic
def apply_recurring_plan(template, range_start, range_end, status, actor=None):
    """Create the posts a series plan allows, after re-deriving the plan inside the transaction.

    One notice per officer per run rather than one per post: a guard who is named on a fourteen-day
    series does not need fourteen emails to learn they work Tuesdays, and the first date says more
    than each repetition would.
    """
    from .models import AuditEvent, Shift
    plan = recurring_plan(template, range_start, range_end, status)
    created = []
    posted = list(template.required_credentials.all())
    for row in plan["rows"]:
        if not row["created"]:
            continue
        shift = Shift.objects.create(organization=template.organization, site_id=template.site_id,
            starts_at=row["starts_at"], ends_at=row["ends_at"], status=status,
            post_name=template.post_name, post_orders=template.post_orders,
            officer=template.officer, template=template)
        shift.required_credentials.set(posted)
        created.append(shift)
    if created:
        AuditEvent.objects.create(organization=template.organization, actor=actor,
            action="shift_template.generated", target_type="shift_template", target_id=str(template.pk),
            metadata={"name": template.name, "range": [str(range_start), str(range_end)],
                      "status": status, "created": len(created), "blocked": len(plan["rows"]) - len(created)})
        if template.officer_id and template.officer.user_id:
            queue_notice(organization=template.organization, recipients={template.officer.user_id},
                event_type="shift.published",
                subject=f"{len(created)} recurring posts added to your schedule",
                body=f"{template.name} at {template.site} from {created[0].starts_at:%a %b %d} "
                     f"({template.window_label}). Open your shifts to see them.",
                dedup_key=f"series:{template.pk}:{range_start}:{range_end}")
    plan["created"] = created
    plan["count"] = len(created)
    return plan


COMPLIANCE_KINDS = ("credentials", "training", "documents", "duties")


def _duty_state(rule, item, today):
    """(state, note, needs_attention) for one duty and the evidence row filed against it."""
    if item is None:
        return ("missing", "No current record filed", True)
    days = (item.expires_on - today).days if item.expires_on else None
    if days is not None and days < 0:
        return ("expired", "The filed evidence has expired", True)
    if days is not None and days <= rule.warning_days:
        return ("pending", f"inside the {rule.warning_days}-day renewal window", True)
    return ("active", "", False)


def _duty_row(rule, person, state, label, note, item=None, measured=False):
    return {"person": person, "subject": rule.name, "reference": (item.original_name if item else rule.code),
            "date": (item.expires_on if item else None), "state": state, "label": label, "note": note,
            "_needs": state in ("missing", "expired", "pending"), "_measured": measured}


def compliance_duties(organization, scope, roster, today=None, reader=None):
    """Each entered duty, who is missing its evidence, and — when there is no answer — the reason.

    CMP-0 makes a non-credential obligation *enterable*; this decides how much of it can honestly be
    *measured* with the evidence stores that already exist. Two are checked: a record the company
    itself holds (§1702.124's certificate of liability is held by the licensee, not by a guard) and a
    record every officer in the named categories must have on their own file. Anything else is listed
    as entered but not evaluated rather than being scored, because "no posting rows found" and
    "every site is compliant" are the same query result read two different ways, and only one is true.

    The reader's document ladder is applied here too, and it changes the *answer* rather than only
    the row: a duty whose proof the reader is not allowed to open is reported as not measured,
    because "no claim filed for Ana Delgado" is a false statement when the claim is filed and sealed
    from them — a permission decision dressed up as a compliance gap.

    An unmeasured duty stays out of ``total`` so it cannot dilute the rate, and is counted separately
    so the screen can say how much of the matrix the number is not describing.
    """
    today = today or timezone.localdate()
    role, subject = reader if reader else (None, None)
    rules = list(organization.compliance_rules.filter(active=True).select_related("document_type"))
    needed = {rule.document_type_id for rule in rules if rule.document_type_id}
    filed = {}
    if needed:
        # One pass over the register, keyed by (record type, holder). A company-held duty is filed
        # against the tenant with no person on the row, which is the same shape as a handbook.
        for item in organization.person_documents.filter(
                deleted_at__isnull=True, revisions__isnull=True,
                scan_status=PersonDocument.ScanStatus.CLEAN, document_type_id__in=needed):
            filed.setdefault((item.document_type_id, item.person_id), item)
    unopenable = "your role does not open the record type that proves it"
    rows = []
    for rule in rules:
        reason = rule.unevaluated_reason
        if reason:
            rows.append(_duty_row(rule, None, "not-evaluated", "Not evaluated", f"Entered, not measured: {reason}"))
            continue
        if rule.applies_to_subject == ComplianceRule.Subject.ORGANIZATION:
            # A company duty stays visible to a bounded supervisor: the certificate is the tenant's,
            # and hiding the one obligation nobody in their scope can file would read as compliance.
            if not record_open_for(rule.document_type, None, role, subject):
                rows.append(_duty_row(rule, None, "not-evaluated", "Not evaluated", f"Entered, not measured: {unopenable}"))
                continue
            item = filed.get((rule.document_type_id, None))
            state, note, _needs = _duty_state(rule, item, today)
            rows.append(_duty_row(rule, None, state, state.title(), note, item, measured=True))
            continue
        holders = [person for person in (roster if isinstance(roster, list) else list(scope.filter_people(roster)))
                   if rule.applies_to_person(person)]
        for person in holders:
            if not record_open_for(rule.document_type, person.pk, role, subject):
                rows.append(_duty_row(rule, person, "not-evaluated", "Not evaluated", f"Entered, not measured: {unopenable}"))
                continue
            item = filed.get((rule.document_type_id, person.pk))
            state, note, _needs = _duty_state(rule, item, today)
            rows.append(_duty_row(rule, person, state, state.title(), note, item, measured=True))
    measured = [row for row in rows if row["_measured"]]
    return {"rows": rows, "attention": sum(1 for row in measured if row["_needs"]), "total": len(measured),
            "unmeasured": len(rows) - len(measured)}


def compliance_attendance(organization, scope, today=None, people=None, reader=None):
    """Every credential, training and record obligation this actor can act on, and which need work.

    This is the single definition of "needs attention" in the product. The compliance queue, the
    dashboard tiles and the reports page all read it, because a report that recomputed the rule by
    a second path would eventually disagree with the list the supervisor is working from — and the
    percentage would then be describing a set nobody can see.

    Each section is bounded by ``scope``: a queue built company-wide while the directory, the
    schedule and the punch desk were narrowed would tell a branch supervisor they own
    registrations they cannot act on.

    ``reader`` is the caller's ``(role, own person id)`` pair, and it gates the two sections that
    read the personnel file. Withholding it is not a neutral default: a rate computed over sealed and
    restricted files the reader was never allowed to open would put a private record's *existence*
    into a number they are entitled to see, and the queue is the surface where that number gets
    quoted to somebody else.
    """
    today = today or timezone.localdate()
    org = organization
    kinds = {}
    types = list(org.credential_types.filter(active=True))
    held = {}
    for item in scope.filter_by_person(org.credentials.select_related("credential_type")):
        held.setdefault(item.person_id, {})[item.credential_type_id] = item
    credential_rows = []
    roster = people if people is not None else scope.filter_people(org.people.exclude(status=Person.Status.INACTIVE)).order_by("last_name", "first_name")
    # One query for every check the loaded credentials could have, newest per credential. The
    # credentials are already in `held`, so this does not scale with the roster.
    checks = registry_checks_by_credential(org, [item.pk for by_type in held.values() for item in by_type.values() if item.pk])
    for person in roster:
        for credential_type, credential, state in credential_obligations(person, types, held.get(person.pk, {})):
            days = (credential.expires_on - today).days if credential and credential.expires_on else None
            due = days is not None and days <= credential_type.warning_days
            registry = credential_registry_state(credential_type, credential,
                                                 checks.get(credential.pk) if credential else None, today)
            # `adverse` joins the attention set; `lapsed` never does. A registry lookup that came
            # back saying the licence is not valid is a fact about whether this officer may work,
            # which is what the rate measures. A lookup nobody has done this quarter is a fact about
            # our filing, and folding it in would let a paperwork gap cancel a post the same way an
            # expired registration does.
            needs = state in INVALID_CREDENTIAL_STATES or due or registry["adverse"]
            note = (("inside the %d-day window" % credential_type.warning_days) if due and state == "active"
                    else ("No record filed for an applicable requirement" if credential is None else ""))
            if registry["adverse"] or registry["lapsed"]:
                note = (note + "; " if note else "") + registry["detail"]
            credential_rows.append({"person": person, "subject": credential_type.name, "reference": (credential.number if credential else "") or "No number recorded", "date": credential.expires_on if credential else None, "state": state, "registry": registry, "note": note, "_needs": needs})
    kinds["credentials"] = {"rows": credential_rows, "attention": sum(1 for row in credential_rows if row["_needs"]), "total": len(credential_rows)}

    training_rows = []
    for item in scope.filter_by_person(org.training_records.select_related("person")):
        days = (item.expires_on - today).days if item.expires_on else None
        needs = days is None or days <= TRAINING_WARNING_DAYS
        training_rows.append({"person": item.person, "subject": item.course_name, "reference": item.provider or item.certificate_number or "No provider", "date": item.expires_on, "state": "expired" if days is not None and days < 0 else ("pending" if needs else "active"), "note": "No renewal date tracked" if days is None else ("renewal window open" if needs else ""), "_needs": needs})
    kinds["training"] = {"rows": training_rows, "attention": sum(1 for row in training_rows if row["_needs"]), "total": len(training_rows)}

    record_rows = []
    # A company record with no person on the row is nobody's personnel file, so a scoped actor's
    # record section is only their own people; the workforce completion count below is then
    # computed against *their* roster rather than the whole company's.
    records = org.person_documents.filter(deleted_at__isnull=True, revisions__isnull=True,
                                          archived_at__isnull=True).select_related("person", "document_type")
    records = records.filter(record_visibility_filter(*reader)) if reader else records.none()
    documents = list(scope.filter_by_person(records) if scope.restricted else records)
    # A company record issued to every worker is only complete when each worker signs, so its
    # state comes from the acknowledgment rows, never from PersonDocument.acknowledged_at — that
    # column is set by the first signature and made the remaining roster disappear.
    company_issued = [item.pk for item in documents if item.document_type.acknowledgment_required and not item.person_id]
    signed = {}
    active_roster = set()
    if company_issued:
        for document_pk, person_pk in DocumentAcknowledgment.objects.filter(document_id__in=company_issued).values_list("document_id", "person_id"):
            signed.setdefault(document_pk, set()).add(person_pk)
        active_roster = set(scope.filter_people(org.people.exclude(status=Person.Status.INACTIVE)).values_list("pk", flat=True))
    for item in documents:
        days = (item.expires_on - today).days if item.expires_on else None
        outstanding = None
        if item.document_type.acknowledgment_required and not item.person_id:
            outstanding = len(active_roster - signed.get(item.pk, set()))
        awaiting = (outstanding > 0) if outstanding is not None else (item.document_type.acknowledgment_required and not item.acknowledged_at)
        needs = (item.scan_status != PersonDocument.ScanStatus.CLEAN or awaiting or (days is not None and days <= TRAINING_WARNING_DAYS))
        state = "rejected" if item.scan_status == PersonDocument.ScanStatus.REJECTED else (item.scan_status if item.scan_status != PersonDocument.ScanStatus.CLEAN else ("expired" if days is not None and days < 0 else ("pending" if needs else "active")))
        signatures = f"{outstanding} of {len(active_roster)} have not acknowledged" if outstanding else ""
        record_rows.append({"person": item.person, "subject": item.document_type.name, "reference": item.original_name, "date": item.expires_on, "state": state, "label": item.get_scan_status_display(), "acknowledgments": (item.pk if outstanding is not None else None), "note": ("not scanned clean" if item.scan_status != PersonDocument.ScanStatus.CLEAN else (signatures or ("renewal window open" if days is not None and days <= TRAINING_WARNING_DAYS else ""))), "_needs": needs})
    kinds["documents"] = {"rows": record_rows, "attention": sum(1 for row in record_rows if row["_needs"]), "total": len(record_rows)}
    kinds["duties"] = compliance_duties(org, scope, roster, today, reader)
    return kinds


def compliance_summary(kinds, readable_kinds=COMPLIANCE_KINDS):
    """The same rows as a rate, so the report and the queue are one computation.

    The denominator is *obligations the actor can see*, and it is returned alongside the number:
    "92% compliant" over one armed registration and "92% compliant" over three hundred are
    different facts, and an operator has to be able to say which one they looked at.
    """
    visible = {name: value for name, value in kinds.items() if name in readable_kinds}
    total = sum(value["total"] for value in visible.values())
    attention = sum(value["attention"] for value in visible.values())
    return {"total": total, "attention": attention, "satisfied": total - attention,
            "rate": (round((total - attention) * 100 / total, 1) if total else None),
            "by_kind": {name: {"total": value["total"], "attention": value["attention"], "satisfied": value["total"] - value["attention"]} for name, value in visible.items()},
            "kinds": tuple(visible.keys())}


def coverage_state(shift, requirements=None):
    """Whether one post actually puts a qualified officer on the ground, and why not if it does not.

    This is the single definition SCH-4 asked for. `coverage_report` builds its three buckets from it
    and the gap check run at cancel time reads it too, so the advisory a dispatcher sees before
    cancelling a post cannot disagree with the report the same night appears in. A second copy of
    "does this post count" is exactly how those two drift apart, and RPT-4 exists to keep "needs
    attention" one calculation.

    Drafts and cancellations are decided here rather than by each caller: a draft is a plan and a
    plan is not coverage, and a cancelled post covers nothing — the two exclusions the report already
    applied by queryset.
    """
    if shift.status == Shift.Status.DRAFT:
        return "draft", []
    if shift.status == Shift.Status.CANCELLED:
        return "cancelled", []
    if not shift.officer_id:
        return "unfilled", []
    allowed, reasons = shift_eligibility(shift, requirements=post_requirements(shift) if requirements is None else requirements)
    return ("filled", []) if allowed else ("at_risk", reasons)


def uncovered_windows(organization, scope, shift):
    """The part of this post's own hours that nothing else would cover if it went away.

    The question SCH-4 names: `shift_advisories` says whether *this officer* crosses a threshold, and
    the report says whether *this post* is staffed, but nothing asked whether removing the officer
    leaves the *post* uncovered — which is what a dispatcher actually weighs at 21:00 when the relief
    does not exist.

    A gap is measured as time, not as a row count, because the post that disappears takes its own
    hours with it: cancelling the 20:00–08:00 tour removes the row that made that span covered, so the
    report's per-post view shows nothing at all and the site is quietly dark. Another officer at the
    same site standing overlapping hours closes the gap — that is what relief is — and an overlapping
    post whose officer may not work it does not, because an at-risk officer is not coverage any more
    than a draft is.

    Returns a list of ``{"starts_at", "ends_at", "hours"}`` remainders, empty when the whole span is
    covered some other way.
    """
    if not shift.site_id or not shift.starts_at or not shift.ends_at:
        return []
    neighbours = scope.filter_shifts(organization.shifts.filter(
        site_id=shift.site_id, starts_at__lt=shift.ends_at, ends_at__gt=shift.starts_at,
    ).exclude(pk=shift.pk)).select_related("officer", "site")
    covered = []
    for other in neighbours:
        state, _ = coverage_state(other)
        if state == "filled":
            covered.append((max(other.starts_at, shift.starts_at), min(other.ends_at, shift.ends_at)))
    covered.sort()
    merged = []
    for start, end in covered:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
            continue
        merged.append((start, end))
    gaps = []
    cursor = shift.starts_at
    for start, end in merged:
        if start > cursor:
            gaps.append((cursor, min(start, shift.ends_at)))
        cursor = max(cursor, end)
    if cursor < shift.ends_at:
        gaps.append((cursor, shift.ends_at))
    return [{"starts_at": start, "ends_at": end,
             "hours": round((end - start).total_seconds() / 3600, 2)} for start, end in gaps]


def uncovered_advisory(organization, scope, shift):
    """The sentence the screen shows, plus the fact behind it, from one call.

    Kept as one function because the confirm page, the POST handler, the audit row and the notice all
    have to say the same thing about the same hours. A warning that is reworded per surface is a
    warning nobody believes.
    """
    gaps = uncovered_windows(organization, scope, shift)
    if not gaps:
        return {"gaps": [], "text": ""}
    hours = round(sum(gap["hours"] for gap in gaps), 2)
    first = gaps[0]
    text = (f"Cancelling this leaves {shift.site} uncovered for {hours}h"
            + (f" (from {timezone.localtime(first['starts_at']):%a %H:%M})" if len(gaps) == 1 else
               f" across {len(gaps)} window{'s' if len(gaps) != 1 else ''}")
            + ". No other published post at this site stands those hours with an officer who may work them.")
    return {"gaps": gaps, "text": text}


def coverage_report(organization, scope, start, end):
    """Whether the published schedule in a window is actually staffed by people who may work it.

    Counted on *published* posts, and drafts are returned as a separate excluded count: a draft
    is a plan, and a plan is not coverage. A post whose officer is no longer eligible counts as
    at risk rather than filled, because that is the case a Texas post cannot absorb — an expired
    registration is a stop, not a warning.
    """
    posts = scope.filter_shifts(
        organization.shifts.filter(starts_at__gte=start, starts_at__lt=end)
    ).select_related("site__client", "officer").prefetch_related("required_credentials").order_by("starts_at", "site__name", "pk")
    drafts = posts.filter(status=Shift.Status.DRAFT).count()
    filled = []
    unfilled = []
    at_risk = []
    for shift in posts.exclude(status__in=[Shift.Status.CANCELLED, Shift.Status.DRAFT]):
        # One call to the one definition, so the buckets below and the gap check at cancel time
        # cannot diverge: `coverage_state` decides whether a post covers ground, and this function
        # only sorts those decisions into the three lists the screen names.
        state, reasons = coverage_state(shift)
        if state == "unfilled":
            unfilled.append(shift)
        elif state == "at_risk":
            at_risk.append({"shift": shift, "reasons": reasons})
        elif state == "filled":
            filled.append(shift)
    total = len(filled) + len(unfilled) + len(at_risk)
    return {"filled": filled, "unfilled": unfilled, "at_risk": at_risk, "total": total, "drafts_excluded": drafts,
            "rate": (round(len(filled) * 100 / total, 1) if total else None)}


def tour_completion(organization, scope, start, end, now=None):
    """Whether past posts have complete time evidence — the punch-out a payroll number rests on.

    A tour counts as closed when it has a clock-in and a clock-out no earlier than that
    clock-in. An unclosed tour is not a figure to hide: it is the reason a timecard is an
    estimate rather than a record, and this is the report that shows which posts are unclosed
    and what is missing from them.
    """
    now = now or timezone.now()
    posts = scope.filter_shifts(
        organization.shifts.filter(ends_at__lte=now, starts_at__gte=start, ends_at__gt=start)
    ).select_related("site__client", "officer").exclude(status=Shift.Status.CANCELLED).order_by("starts_at", "site__name", "pk")
    punches = defaultdict(list)
    for punch in Punch.objects.filter(organization=organization, shift__in=posts).only("shift_id", "kind", "occurred_at"):
        punches[punch.shift_id].append(punch)
    closed = 0
    closed_shifts = []
    open_tours = []
    for shift in posts:
        events = punches.get(shift.pk, [])
        ins = [item.occurred_at for item in events if item.kind == Punch.Kind.IN]
        outs = [item.occurred_at for item in events if item.kind == Punch.Kind.OUT]
        if ins and outs and max(outs) >= min(ins):
            closed += 1
            closed_shifts.append(shift)
        else:
            missing = "clock-out" if ins else "clock-in and clock-out"
            open_tours.append({"shift": shift, "missing": missing})
    total = len(posts)
    return {"closed": closed, "closed_shifts": closed_shifts, "open": open_tours, "total": total,
            "rate": (round(closed * 100 / total, 1) if total else None)}


# --------------------------------------------------------------------------- reporting (RPT-1..4)

def schedule_week_start(day=None, offset=0):
    """The Monday that begins the week the schedule renders.

    Exposed because the boundary belongs to the page, not to whoever is looking at it: a fixture
    that placed posts at `now + 2 days` and asserted they appeared on the default week was wrong on
    a Saturday run, where +2 days was already next Monday. Anything that needs to name a week should
    ask this, so the arithmetic exists once.
    """
    day = day or timezone.localdate()
    return day - timedelta(days=day.weekday()) + timedelta(weeks=offset)


def week_offset_for(day, from_day=None):
    """How many weeks away `day` is from the week containing `from_day`, for the `?week=` parameter."""
    return (schedule_week_start(day) - schedule_week_start(from_day)).days // 7


# A window a captured figure describes. Seven days matches the reports page default, so the live
# panel and the history table underneath it are the same measurement — and the window is stored on
# every row, because a trend whose periods changed width partway through the series is not a trend.
SNAPSHOT_WINDOW_DAYS = 7
SNAPSHOT_HISTORY_ROWS = 8

# A compliance obligation attaches to a person, and a person belongs to a branch; it does not belong
# to a contract. Coverage and tours attach to a site, which has both. Stating that instead of
# inventing a client split for credentials is what keeps the breakdown honest.
BREAKDOWN_NOTE = "Obligations attach to a person and their branch, not to a contract — the contract view of an obligation would be a guess about where they work."


def _day_start(day):
    return timezone.make_aware(datetime.combine(day, datetime.min.time()))


def _group(name, rows):
    """One breakdown line: how many obligations a subject carries and how many need action."""
    total = len(rows)
    attention = sum(1 for row in rows if row.get("_needs"))
    return {"subject": name, "total": total, "attention": attention, "satisfied": total - attention,
            "rate": (round((total - attention) * 100 / total, 1) if total else None)}


def _group_shifts(name, items, needs):
    total = len(items)
    attention = sum(1 for item in items if needs(item))
    return {"subject": name, "total": total, "attention": attention, "satisfied": total - attention,
            "rate": (round((total - attention) * 100 / total, 1) if total else None)}


def _attention_rows(attendance, kinds):
    """Every row that needs action across the kinds the rate counted, tagged with its kind."""
    rows = []
    for name in kinds:
        for row in attendance.get(name, {}).get("rows", []):
            if not row.get("_needs"):
                continue
            rows.append({"kind": name,
                         "person": (row["person"].full_name if row.get("person") else None),
                         "subject": row["subject"], "state": row["state"], "note": row["note"]})
    return rows


def _all_rows(attendance):
    """Every row the rate counted. Rows the queue reports but does not measure are left out here too.

    A duty whose evidence store does not exist yet is listed in the queue and excluded from the
    denominator, so a breakdown that grouped it would add up to more than the panel above it — the
    exact disagreement RPT-4 exists to prevent, and the reason this filters instead of flattening.
    """
    rows = []
    for section in attendance.values():
        rows.extend(row for row in section.get("rows", []) if row.get("_measured") is not False)
    return rows


def report_figures(organization, scope, today=None, days=SNAPSHOT_WINDOW_DAYS, now=None):
    """The three figures for one authority scope, in the shape a snapshot stores.

    One function serves the live panel, the nightly capture and the saved CSV. That is deliberate
    (RPT-4): `compliance_attendance` is the product's only definition of "needs attention", and a
    report that recomputed the rule by a second path would eventually disagree with the queue a
    supervisor is working from — the number would then describe a set nobody can see.

    A stored figure is taken on the **company-wide reader basis** (every record type counted), because
    a business record is not one person's view. A role that may not open every record type sees a
    different live rate — and the page says so, rather than letting a history line and the panel above
    it quietly measure different things.
    """
    today = today or timezone.localdate()
    now = now or timezone.now()
    start = _day_start(today)
    window_start = start
    window_end = start + timedelta(days=days)
    past_start = start - timedelta(days=days)
    attendance = compliance_attendance(organization, scope, today=today, reader=(Membership.Role.OWNER, None))
    summary = compliance_summary(attendance)
    coverage = coverage_report(organization, scope, window_start, window_end)
    # Same arguments the live panel passes: the tour window runs to the moment of measurement, not to
    # midnight, so the stored basis describes the window that was actually counted.
    closed = tour_completion(organization, scope, past_start, now)
    return {
        ReportSnapshot.Metric.COMPLIANCE: {
            "total": summary["total"], "satisfied": summary["satisfied"], "attention": summary["attention"],
            "rate": summary["rate"],
            "basis": {"window_days": days, "kinds": list(summary["kinds"]), "by_kind": summary["by_kind"],
                      "reader_basis": "company-wide: every record type counted",
                      "excluded": "inactive personnel, and requirements whose categories do not apply to the person"},
            "exceptions": _attention_rows(attendance, summary["kinds"]),
        },
        ReportSnapshot.Metric.COVERAGE: {
            "total": coverage["total"], "satisfied": len(coverage["filled"]),
            "attention": len(coverage["unfilled"]) + len(coverage["at_risk"]), "rate": coverage["rate"],
            "basis": {"window_days": days, "counted": "published posts only",
                      "window_start": window_start.isoformat(), "window_end": window_end.isoformat(),
                      "drafts_excluded": coverage["drafts_excluded"]},
            "exceptions": [{"post": (item["shift"].post_name or "General post"),
                            "site": str(item["shift"].site), "client": str(item["shift"].site.client),
                            "when": item["shift"].starts_at.isoformat(),
                            "problem": "assigned officer is not eligible: " + " ".join(item["reasons"])}
                           for item in coverage["at_risk"]] + [
                           {"post": (shift.post_name or "General post"), "site": str(shift.site),
                            "client": str(shift.site.client), "when": shift.starts_at.isoformat(),
                            "problem": "no officer assigned"} for shift in coverage["unfilled"]],
        },
        ReportSnapshot.Metric.TOUR: {
            "total": closed["total"], "satisfied": closed["closed"], "attention": len(closed["open"]),
            "rate": closed["rate"],
            "basis": {"window_days": days, "counted": "posts that ended by the moment of measurement, cancelled excluded",
                      "window_start": past_start.isoformat(), "window_end": now.isoformat()},
            "exceptions": [{"post": (item["shift"].post_name or "General post"), "site": str(item["shift"].site),
                            "client": str(item["shift"].site.client), "ended": item["shift"].ends_at.isoformat(),
                            "missing": item["missing"],
                            "officer": (item["shift"].officer.full_name if item["shift"].officer_id else None)}
                           for item in closed["open"]],
        },
    }


def report_subjects(organization):
    """The subjects a capture measures: the company, each branch, each contract.

    Synthetic grant rows rather than a second filter: `ActorScope` already expands a branch grant to
    the sites and people it covers, and reusing it is what keeps a branch's saved figure meaning the
    same thing as that branch manager's own screen.
    """
    anchor = (organization.memberships.filter(active=True, role__in=[Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR]).first()
              or organization.memberships.filter(active=True).first())
    if anchor is None:
        return
    # ``follows_own_branch`` is False on every synthetic row by construction — this is a report subject,
    # not a person's grant, and there is no membership for it to follow. It is spelled out rather than
    # defaulted inside ActorScope because a stand-in that quietly lacks a grant column would make a real
    # AuthorityScope typo read as "explicit scope", which is the wrong answer in the one direction that
    # matters: it would widen reach instead of narrowing it.
    grant_type = namedtuple("Grant", "branch_id client_id site_id label follows_own_branch")
    yield "company", None, None, ActorScope(anchor, ())
    for branch in organization.branches.filter(active=True).order_by("name"):
        yield f"branch:{branch.pk}", branch, None, ActorScope(anchor, (grant_type(branch.pk, None, None, branch.name, False),))
    for client in organization.clients.filter(active=True).order_by("name"):
        yield f"client:{client.pk}", None, client, ActorScope(anchor, (grant_type(None, client.pk, None, client.name, False),))


def capture_report_snapshots(organization=None, period_date=None, days=SNAPSHOT_WINDOW_DAYS):
    """Write the period's figures for every subject, once. Idempotent, and never rewrites a day.

    The first capture of a day stands: a record that silently updated whenever evidence changed
    underneath it would stop being a record of anything. `captured_at` says which moment the number
    came from, which is the honest version of "last month we were at 92%".

    The early `exists()` test is what makes this safe to leave in a worker loop that ticks every
    minute. Computing three figures per subject is real work, and paying it 1,440 times a day to
    create nothing on the first pass would be a self-inflicted capacity problem — so a period that
    already has any row is left alone, and the manual "Save today's figures" button reports that
    honestly instead of pretending to have pinned a new one.
    """
    period_date = period_date or timezone.localdate()
    created = 0
    organizations = [organization] if organization is not None else list(Organization.objects.all())
    for org in organizations:
        if ReportSnapshot.objects.filter(organization=org, period_date=period_date).exists():
            continue
        for subject_key, branch, client, scope in report_subjects(org):
            for metric, figure in report_figures(org, scope, today=period_date, days=days).items():
                _, was_created = ReportSnapshot.objects.get_or_create(
                    organization=org, subject_key=subject_key, metric=metric, period_date=period_date,
                    defaults={"branch": branch, "client": client, "total": figure["total"],
                              "satisfied": figure["satisfied"], "attention": figure["attention"],
                              "rate": figure["rate"], "basis": figure["basis"],
                              "exceptions": figure["exceptions"]})
                created += int(was_created)
    return created


def saved_report_history(organization, scope, limit=SNAPSHOT_HISTORY_ROWS):
    """The stored figures this actor's own subjects, newest first.

    Deliberately *not* averaged into one line per date: a supervisor holding two branches and a
    contract has subjects whose denominators overlap (an officer in branch A who stands a site under
    contract X is counted in both), so summing them would invent a number that never was measured.
    """
    keys = None if not scope.restricted else sorted(
        {f"branch:{pk}" for pk in scope.branch_ids} | {f"client:{pk}" for pk in scope.client_ids})
    rows = ReportSnapshot.objects.filter(organization=organization).select_related("branch", "client")
    if keys is not None:
        if not keys:
            # A grant that reaches no branch and no contract has no saved subject to show.
            return {metric.value: [] for metric in ReportSnapshot.Metric}
        rows = rows.filter(subject_key__in=keys)
    rows = list(rows.order_by("-period_date", "metric", "subject_key"))
    history = {metric.value: [] for metric in ReportSnapshot.Metric}
    for row in rows:
        if len(history[row.metric]) < limit:
            history[row.metric].append(row)
    return history


def report_breakdown(organization, attendance, coverage, closed, today=None):
    """The live figures split by branch and by contract, from the rows the panels already built.

    Grouped in Python over existing rows rather than by re-running each computation per subject: N
    passes would be N definitions of "needs attention" with N chances to drift (RPT-4), and this way
    the groups provably add up to the number above them because they *are* the same rows.

    A subject with no branch recorded still gets a line, so a breakdown cannot quietly lose the
    people the company never filed a branch for — the ones most likely to be the gap.
    """
    branches = {row.pk: row.name for row in organization.branches.all()}
    clients = {row.pk: row.name for row in organization.clients.all()}
    unassigned = "Branch not recorded"

    by_branch = defaultdict(list)
    for row in _all_rows(attendance):
        person = row.get("person")
        key = branches.get(person.branch_id, unassigned) if person is not None else "Company record"
        by_branch[key].append(row)
    compliance_rows = [_group(key, rows) for key, rows in sorted(by_branch.items())]

    def site_groups(items):
        branch_map, client_map = defaultdict(list), defaultdict(list)
        for item in items:
            shift = item.get("shift") if isinstance(item, dict) else item
            if shift is None:
                continue
            branch_map[branches.get(shift.site.branch_id, unassigned)].append(item)
            client_map[clients.get(shift.site.client_id, "Contract not recorded")].append(item)
        return branch_map, client_map

    def shift_metric_windows(branch_map, client_map, needs):
        return ([_group_shifts(key, items, needs) for key, items in sorted(branch_map.items())],
                [_group_shifts(key, items, needs) for key, items in sorted(client_map.items())])

    filled = [{"shift": shift, "_ok": True} for shift in coverage["filled"]]
    risky = [{**item, "_ok": False} for item in coverage["at_risk"]]
    gaps = [{"shift": shift, "_ok": False} for shift in coverage["unfilled"]]
    coverage_items = filled + risky + gaps
    branch_map, client_map = site_groups(coverage_items)
    coverage_branch, coverage_client = shift_metric_windows(branch_map, client_map, lambda item: not item["_ok"])

    tour_items = ([{"shift": shift, "_ok": True} for shift in closed["closed_shifts"]]
                  + [{"shift": item["shift"], "_ok": False} for item in closed["open"]])
    branch_map, client_map = site_groups(tour_items)
    tour_branch, tour_client = shift_metric_windows(branch_map, client_map, lambda item: not item["_ok"])

    return {"compliance": {"branch": compliance_rows, "contract": [], "note": BREAKDOWN_NOTE},
            "coverage": {"branch": coverage_branch, "contract": coverage_client, "note": ""},
            "tour": {"branch": tour_branch, "contract": tour_client, "note": ""}}


def snapshot_csv(row):
    """A saved figure and the items behind it, as one downloadable file.

    Reuses `safe_cell` for the same reason the payroll exporter does: an exception note can carry
    text somebody typed, and a spreadsheet that evaluates it is a security defect.
    """
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Report", "Subject", "Period", "Captured", "Total", "Satisfied", "Needs action", "Rate %"])
    writer.writerow([row.get_metric_display(), row.subject_label, row.period_date.isoformat(),
                     row.captured_at.isoformat(), row.total, row.satisfied, row.attention,
                     "" if row.rate is None else row.rate])
    writer.writerow([])
    writer.writerow(["Basis"])
    for key, value in row.basis.items():
        writer.writerow([safe_cell(key), safe_cell(json.dumps(value) if isinstance(value, (dict, list)) else value)])
    writer.writerow([])
    writer.writerow(["Items needing action"])
    columns = ["kind", "person", "subject", "site", "client", "post", "officer", "when", "ended", "state", "missing", "problem", "note"]
    writer.writerow(columns)
    for item in row.exceptions:
        writer.writerow([safe_cell(item.get(column, "")) for column in columns])
    return output.getvalue()


def haversine_meters(lat1, lon1, lat2, lon2):
    radius=6371000
    p1,p2=math.radians(float(lat1)),math.radians(float(lat2))
    dp=math.radians(float(lat2)-float(lat1)); dl=math.radians(float(lon2)-float(lon1))
    a=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return radius*2*math.atan2(math.sqrt(a),math.sqrt(1-a))

class ResolvedClockPolicy:
    """The clock rule that governs one post, walked level by level: site → contract → company.

    ``docs/discovery-decisions.md``: "Clock evidence is configurable at global, client, and site
    levels. Authorized client and site policies may strengthen or weaken the global baseline. The
    resolved effective policy, source scope, version, and authorizing actor must be visible and
    auditable."

    Inheritance is per field, so a property that waives only the geofence still takes its
    rounding from the contract it stands on — resolving "most specific row wins" as a whole would
    have silently dropped the contract's rounding back to the company's, which is the bug this
    class exists to avoid. Each consumed value therefore reports its own origin:

        policy.rounding   -> {"mode","minutes","source","version"}   stamped on every timecard
        policy.geofence   -> {"required","source","version"}         stamped on every punch audit

    The calendar (workweek start, overtime threshold, timezone, reopen) is delegated to the
    company row unconditionally: a pay period cannot begin on two days at once, and an override
    that appeared to change that would be a lie.
    """

    __slots__ = ("chain", "company")

    def __init__(self, chain, company):
        self.chain = chain          # [(scope, row)] most specific first, company row last
        self.company = company

    def origin_of(self, field):
        for scope, row in self.chain:
            value = getattr(row, field, None)
            if value not in (None, ""):
                return {"value": value, "source": scope, "version": f"{row.pk}:{row.revision}"}
        return {"value": None, "source": "company", "version": None}

    @property
    def rounding(self):
        mode = self.origin_of("rounding_mode")
        minutes = self.origin_of("rounding_minutes")
        # The version is the mode's own row: the interval is meaningless without the mode, so a
        # stamp that named some other row's version would point at text that never chose this
        # rounding. The company row always supplies both values with its defaults, so neither
        # origin can come back empty.
        return {"mode": mode["value"], "minutes": minutes["value"], "source": mode["source"], "version": mode["version"]}

    @property
    def geofence(self):
        origin = self.origin_of("require_geofence")
        return {"required": bool(origin["value"]), "source": origin["source"], "version": origin["version"]}

    @property
    def kiosk(self):
        # CLK-2. Resolved on exactly the geofence's shape, and for the same reason: "a shared clock
        # is allowed here" is a per-field decision, so a contract that only waived the fence must not
        # also flip the kiosk rule, and a site that forbade the kiosk keeps the contract's fence.
        # `bool(origin["value"])` alone would be a lie in one case — a company row cannot be null, so
        # the only way to reach `value=None` is a rule that was never seeded, and the default is then
        # "allowed", stated explicitly rather than by accident.
        origin = self.origin_of("allow_kiosk")
        return {"allowed": True if origin["value"] is None else bool(origin["value"]),
                "source": origin["source"], "version": origin["version"]}

    @property
    def spoof_risk(self):
        """CLK-4. Whether an implausible location reading should reach a human at this post.

        Resolved per field like the two above it, and defaulted to on for the reason in the model:
        the signal only *names* a punch, it never blocks the clock, so leaving it on cannot strand an
        officer at a gate. A firm that has decided it will not review these turns it off at the level
        that owns the decision, and the version stamp records who said so.
        """
        origin = self.origin_of("flag_spoof_risk")
        return {"flagged": True if origin["value"] is None else bool(origin["value"]),
                "source": origin["source"], "version": origin["version"]}

    @property
    def selfie(self):
        """CLK-1. Whether a frame is asked for at the ends of a tour at this post.

        Third field on the same chain, resolved the same way for the same reason: "prove who you are
        with your face" is a per-contract decision, not a company-wide one, and a property that runs a
        kiosk cannot be forced to inherit a biometric rule it never agreed to.

        `required` is only half the question. Whether a given *punch* owes the frame is
        `selfie_owed()` below, because the owner's ruling makes it conditional on no other identity
        method having covered the event — and a resolver that answers both would have to know about
        kiosks and checkpoints, which is not its business.
        """
        origin = self.origin_of("require_selfie")
        return {"required": False if origin["value"] is None else bool(origin["value"]),
                "source": origin["source"], "version": origin["version"]}

    # Convenience for the two consumers, so neither has to know the shape above.
    @property
    def rounding_mode(self):
        return self.rounding["mode"]

    @property
    def rounding_minutes(self):
        return self.rounding["minutes"]

    @property
    def require_geofence(self):
        return self.geofence["required"]

    def __getattr__(self, name):
        # workweek_start / overtime_after_hours / timezone / allow_reopen
        return getattr(self.company, name)


def effective_clock_policy(organization, site=None):
    """Resolve the clock rule for one post, naming the level each value came from."""
    company, _ = TimePolicy.objects.get_or_create(organization=organization, defaults={"timezone": organization.timezone})
    if site is None:
        return ResolvedClockPolicy([("company", company)], company)
    rows = {row.scope: row for row in TimePolicyOverride.objects.filter(
        Q(site_id=site.pk) | Q(client_id=site.client_id), organization_id=organization.pk)}
    chain = [("company", company)]
    contract = rows.get("contract")
    if contract is not None:
        chain.insert(0, ("contract", contract))
    site_row = rows.get("site")
    if site_row is not None:
        chain.insert(0, ("site", site_row))
    return ResolvedClockPolicy(chain, company)


def clock_policy_coverage(organization):
    """How many active posts take their *rounding* from each level.

    Counted on the rounding rule rather than "does any rule exist here", because that is the
    number an operator is weighing when they edit the baseline rounding: a site that waived only
    the geofence still follows the company's rounding and must not be reported as exempt from a
    baseline change. Two queries, not one per site.
    """
    sites = list(Site.objects.filter(organization=organization, active=True).values("id", "client_id"))
    site_rules = {row.site_id: row for row in TimePolicyOverride.objects.filter(organization=organization, site__isnull=False)}
    client_rules = {row.client_id: row for row in TimePolicyOverride.objects.filter(organization=organization, client__isnull=False)}
    company, _ = TimePolicy.objects.get_or_create(organization=organization, defaults={"timezone": organization.timezone})
    counts = {"company": 0, "contract": 0, "site": 0}
    for site in sites:
        chain = [("site", site_rules[site["id"]]) if site["id"] in site_rules else None,
                 ("contract", client_rules[site["client_id"]]) if site["client_id"] in client_rules else None,
                 ("company", company)]
        resolved = ResolvedClockPolicy([entry for entry in chain if entry is not None], company)
        counts[resolved.rounding["source"]] += 1
    return counts, len(sites)


# The fields that actually change how an evaluation or a timecard comes out. A rule's history is
# kept for these, not for every column: renaming a requirement is not a new version of the rule,
# and recording it as one would make the revision number mean nothing.
RULE_WATCHED = {
    # ``overtime_premium`` has to appear here *and* in ``bump_policy_revision``'s list. The bump list
    # decides when a version moves; this one decides what a stored version actually holds. They were
    # not kept in step when PAY-5 landed, so a stub stamped "company:4" could resolve to a snapshot
    # with every rounding field in it and no multiple in it — the one number the stamp was there to
    # protect. A watched-but-unstored value is worse than an unwatched one, because it is promised.
    RuleRevision.Kind.CLOCK_POLICY: ("timezone", "workweek_start", "overtime_after_hours", "overtime_premium",
        "rounding_mode", "rounding_minutes", "require_geofence", "allow_kiosk", "flag_spoof_risk",
        "require_selfie", "allow_reopen"),
    RuleRevision.Kind.CLOCK_RULE: ("require_geofence", "allow_kiosk", "flag_spoof_risk", "require_selfie",
        "rounding_mode", "rounding_minutes"),
    # `name` and `code` are deliberately absent: a label retyped is not a rule changed, and an
    # evaluation cannot come out differently because of it. What a version was *called* is carried
    # by the row's own label column instead, so history still reads as itself.
    RuleRevision.Kind.CREDENTIAL_RULE: ("jurisdiction", "authority_url", "authority_reference",
        "interpretation", "effective_from", "effective_until", "blocks_scheduling", "blocks_clock_in",
        "warning_days", "reminder_days_before", "evidence_required", "applies_to", "active"),
    # Same rule, same reasoning: a duty's *name* may be retyped without anyone's reading of the
    # obligation changing, while which evidence closes it, who it binds, and when it took effect
    # are exactly what a later answer to "why was this person stood anyway" turns on.
    RuleRevision.Kind.COMPLIANCE_RULE: ("jurisdiction", "authority_url", "authority_reference",
        "interpretation", "effective_from", "effective_until", "warning_days", "reminder_days_before",
        "evidence", "applies_to_subject", "applies_to", "document_type_id", "required_hours", "active"),
    # PAY-2. The three switches on a category are watched because between them they decide what a
    # designation is worth; `name` and `interpretation` are not, for the same reason they are not on
    # the credential row — a clerk retyping "Holiday hours" as "Public holiday pay" has not changed
    # what those hours cost, and a version that moves for nothing stops meaning anything.
    RuleRevision.Kind.PAY_CATEGORY: ("paid", "multiplier", "counts_toward_overtime", "active"),
}
# The stamp a punch or payroll row carries says which level the value came from; that is what tells
# this table which kind of rule to look the revision up under.
RULE_SOURCE_KIND = {"company": RuleRevision.Kind.CLOCK_POLICY, "contract": RuleRevision.Kind.CLOCK_RULE,
                    "site": RuleRevision.Kind.CLOCK_RULE}


def _rule_json(value):
    """A JSON-safe form of a rule value, so a stored revision can be read back without the row."""
    if value is None or isinstance(value, (bool, int, float, list, dict)):
        return value
    return str(value)


def _rule_label(rule, kind):
    """How a rule names itself in history, so a removed one still reads as itself."""
    for attribute in ("label", "name", "code"):
        value = getattr(rule, attribute, None)
        if value and str(value).strip():
            return str(value)[:200]
    return dict(RuleRevision.Kind.choices).get(kind, str(kind))


def rule_values(kind, rule):
    return {name: _rule_json(getattr(rule, name, None)) for name in RULE_WATCHED[kind]}


def record_rule_revision(rule, kind, actor=None, previous=None):
    """Append the rule's current values under its current revision.

    Written on create as well as on change: a stamp reading ``7:1`` has to resolve too, and if only
    edited rules had history then the first version of every rule — the one most timecards were
    actually produced by — would be the one version missing.

    ``previous`` is the same treatment for the version being replaced. A rule created before this
    table existed has no history for its revision 1, and the moment it is edited the only record of
    what revision 1 said is the value the save just overwrote — so the caller hands that snapshot in
    and it is written under the old revision number. Without this, stamping a punch ``4:1`` and
    then editing the rule once would be enough to make the punch unanswerable.

    Existing rows are never updated. This table is the thing a wage claim is answered with, and a
    history that can be rewritten to match the present is not a history.
    """
    if rule.pk is None:
        return None
    rule_id = str(rule.pk)
    revision = getattr(rule, "revision", 1) or 1
    if previous and previous[0] and previous[0] < revision:
        backfill_rule_revision(rule, kind, previous[0], previous[1], actor)
    values = rule_values(kind, rule)
    stored = RuleRevision.objects.filter(kind=kind, rule_id=rule_id, revision=revision).first()
    if stored is not None:
        return stored
    previous_row = RuleRevision.objects.filter(kind=kind, rule_id=rule_id, revision__lt=revision).order_by("-revision").first()
    before = previous_row.values if previous_row else {}
    changed = {name: {"before": before.get(name), "after": value} for name, value in values.items() if before.get(name) != value}
    return RuleRevision.objects.create(organization=rule.organization, kind=kind, rule_id=rule_id,
        revision=revision, values=values, changed=changed, label=_rule_label(rule, kind), saved_by=actor)


def backfill_rule_revision(rule, kind, revision, values, actor=None):
    """Write an earlier version whose values were captured just before they were overwritten."""
    if rule.pk is None or revision is None:
        return None
    if RuleRevision.objects.filter(kind=kind, rule_id=str(rule.pk), revision=revision).exists():
        return None
    earlier = RuleRevision.objects.filter(kind=kind, rule_id=str(rule.pk), revision__lt=revision).order_by("-revision").first()
    before = earlier.values if earlier else {}
    return RuleRevision.objects.create(organization=rule.organization, kind=kind, rule_id=str(rule.pk),
        revision=revision, values=values,
        changed={name: {"before": before.get(name), "after": value} for name, value in values.items() if before.get(name) != value},
        label=_rule_label(rule, kind), saved_by=actor)


def rule_snapshot(kind, rule):
    """The watched values of a rule as they stand now, for comparing against a save."""
    return rule_values(kind, rule)


def ensure_rule_history(organization):
    """Give rules that predate this table a version 1, so a later edit has something to differ from.

    Recorded lazily rather than in a data migration: the values written here are the rule *as it
    stands today*, which is the honest label for it — an older revision cannot be reconstructed
    from anything, and inventing one would put numbers into the history that no punch was ever
    produced by. A stamp pointing before this row is reported as predating the history instead.
    """
    seeded = 0
    for kind, rows in ((RuleRevision.Kind.CLOCK_POLICY, TimePolicy.objects.filter(organization=organization)),
                       (RuleRevision.Kind.CLOCK_RULE, TimePolicyOverride.objects.filter(organization=organization)),
                       (RuleRevision.Kind.CREDENTIAL_RULE, CredentialType.objects.filter(organization=organization)),
                       (RuleRevision.Kind.COMPLIANCE_RULE, ComplianceRule.objects.filter(organization=organization)),
                       # A category seeded before this call existed may already have moved its
                       # revision with nothing behind any of it. Seeding records it as it stands
                       # today — the honest label, for the same reason as every other kind here.
                       (RuleRevision.Kind.PAY_CATEGORY, PayCategory.objects.filter(organization=organization))):
        known = set(RuleRevision.objects.filter(organization=organization, kind=kind).values_list("rule_id", flat=True))
        for row in rows:
            if str(row.pk) in known:
                continue
            record_rule_revision(row, kind)
            seeded += 1
    return seeded


def resolve_rule_version(organization, source, version):
    """What a ``policy_version`` stamp was produced from, even after the rule row is deleted.

    This is POL-1's done-criterion stated as a function: a punch recorded against version 2 of a
    site rule must still answer "what were the numbers in version 2" once that row is gone.
    Returns ``None`` when the stamp predates this table, which is reported honestly rather than
    guessed at from the rule's current text.
    """
    kind = RULE_SOURCE_KIND.get(source)
    if kind is None or not version or ":" not in str(version):
        return None
    rule_id, _, revision = str(version).partition(":")
    try:
        revision = int(revision)
    except ValueError:
        return None
    row = RuleRevision.objects.filter(organization=organization, kind=kind, rule_id=rule_id, revision=revision).first()
    if row is not None:
        return {"source": source, "version": str(version), "values": row.values, "changed": row.changed,
                "label": row.label, "saved_at": row.saved_at, "saved_by": str(row.saved_by or ""),
                "kind": row.get_kind_display()}
    # The company baseline has always existed at revision 1; a stamp into it that predates this
    # table can still be read off the live row, because that row is never deleted.
    live = TimePolicy.objects.filter(organization=organization).first()
    if kind == RuleRevision.Kind.CLOCK_POLICY and live and str(live.pk) == rule_id:
        return {"source": source, "version": str(version), "values": rule_values(kind, live), "label": "Company clock policy",
                "saved_at": live.updated_at, "saved_by": "", "kind": live.get_kind_display() if hasattr(live, "get_kind_display") else "Company clock policy",
                "from_live_row": live.revision == revision}
    return None


def bump_policy_revision(row, before):
    """Advance a policy row's version only when a value actually changed.

    Counting every save would make the stamp meaningless: a timecard claiming version 7 of a
    rule that was never edited differently from version 6 is worse than no version at all.
    """
    # `name in before` is load-bearing, not tidiness: a field the caller did not snapshot cannot be
    # compared, and str(None) != str(value) would report a change on every save. When PAY-5 added
    # overtime_premium to this list, the time-policy view that passes a hand-written snapshot began
    # bumping its own version on every save — a caught-red-handed version is worse than none,
    # because it is a false statement about the past that everyone trusts.
    #
    # The list is read from RULE_WATCHED rather than written out again, because the two were the same
    # set by accident and drifted once. A field that is watched for storage but not for bumping makes
    # every stored version a statement about a value that never moved, and that is the PAY-5 defect in
    # the other direction. One source: adding a field to RULE_WATCHED makes it bumpable and storable
    # together, which is the only way they can stay in step without a test remembering both.
    watched = [name for name in RULE_WATCHED[RuleRevision.Kind.CLOCK_POLICY]
               if hasattr(row, name) and name in before]
    if any(str(before.get(name)) != str(getattr(row, name)) for name in watched):
        row.revision = (row.revision or 1) + 1
    return row


def bump_rule_revision(row, kind, before):
    """The clock policy's rule for any governed row: a new version only when a watched value moved.

    Kept separate from ``bump_policy_revision`` rather than merged into it, because that one is
    already the contract the clock tests pin; this is the same idea applied to a credential
    requirement, where changing a reminder lead time silently invalidates every evaluation and
    notice computed under the old one.
    """
    watched = RULE_WATCHED[kind]
    if any(str(before.get(name)) != str(_rule_json(getattr(row, name, None))) for name in watched):
        row.revision = (getattr(row, "revision", 1) or 1) + 1
    return row


def rounding_preview(mode, minutes, samples=(("7:32", 452), ("8:53", 533), ("10:07", 607), ("12:00", 720))):
    """The effect of a proposed rounding rule on worked time, before it is saved.

    ``docs/discovery-decisions.md`` requires the UI to "preview examples and the aggregate
    effect before activation". Rounding moves money on a guard's longest tour, so the operator
    has to see the direction and the size of the shift, not approve a mode name.
    """
    preview = []
    for label, worked in samples:
        rounded = round_minutes(worked, _PreviewPolicy(mode, minutes))
        delta = rounded - worked
        preview.append({"worked": label, "worked_minutes": worked, "rounded_minutes": rounded,
                        "rounded": f"{rounded // 60}:{rounded % 60:02d}", "delta_minutes": delta,
                        "paid_hours": round(rounded / 60, 2)})
    return preview


class _PreviewPolicy:
    """Just enough shape for round_minutes() to run on unsaved values."""
    __slots__ = ("rounding_mode", "rounding_minutes")

    def __init__(self, mode, minutes):
        self.rounding_mode = mode or TimePolicy.RoundingMode.EXACT
        self.rounding_minutes = minutes or 1


# ── CLK-4: mock-location / spoofing-risk signals ────────────────────────────────────
#
# DD lists "mock-location/spoofing-risk signals" among the supported clock-evidence options. There is
# no honest way to *prove* a phone did not forge its coordinates from a browser: the operating system
# knows (Android's `Location.isFromMockProvider`) and no web API exposes it. What can be measured is
# implausibility, so these are signals that route a punch to a human, not verdicts that reject one —
# and the thresholds below are numbers a reviewer is allowed to disagree with, printed in the detail.
IMPLAUSIBLE_ACCURACY_METERS = 2.0
# A phone reports the radius it *believes* its fix is good to. Sub-2-metre accuracy means either a
# survey-grade receiver on a guard tower or somebody typed a coordinate pair; the mock providers in
# circulation set the radius to 0 or 1 because they do not bother filling it in.
MAX_FIX_AGE_SECONDS = 300
IMPOSSIBLE_SPEED_KMH = Decimal("200")
MIN_TELEPORT_METERS = 25_000
# Both floors together are what keeps a legitimate double-header out of review: a 60 km gap between
# two posts is only a signal if the officer covered it in under 18 minutes. Below the distance floor
# the numbers are indistinguishable from GPS jitter at one post, and a signal that fires on jitter
# trains reviewers to ignore signals.

def location_risk_signals(*, occurred_at, latitude=None, longitude=None, accuracy_m=None,
                          fix_age_seconds=None, offline=False, previous=None):
    """Every reason to doubt one location reading, as measurements rather than opinions.

    Returns a list of `{"code", "detail", "measured"}` dicts. Empty means "nothing about this reading
    is impossible", which is *not* the same claim as "this reading is genuine" — a well-chosen fake
    coordinate a kilometre from the post is indistinguishable from the real thing here, and saying so
    is why this feeds a review queue instead of an approval.

    Each signal is independent, so a reviewer sees all three reasons when a punch has all three:
    a faked location that also has a stale fix and teleports is a different conversation from one
    that merely has an odd radius, and the count is the evidence.
    """
    signals = []
    has_coordinates = latitude is not None and longitude is not None
    if has_coordinates and accuracy_m is not None:
        try:
            accuracy = Decimal(str(accuracy_m))
        except (TypeError, ValueError, ArithmeticError):
            accuracy = None
        if accuracy is not None and accuracy <= Decimal(str(IMPLAUSIBLE_ACCURACY_METERS)):
            signals.append({"code": "implausible_accuracy",
                "detail": f"Reported a location good to {accuracy} m, which a phone does not measure on its own.",
                "measured": {"accuracy_m": str(accuracy)}})
    if has_coordinates and not offline and fix_age_seconds is not None:
        try:
            age = int(fix_age_seconds)
        except (TypeError, ValueError):
            age = None
        # Only for an online punch. An offline reading is *supposed* to be old by the time it
        # synchronises — that is the feature CLK-5 ships — and applying this test to it would flag
        # every punch from a post with no signal, which is most of them.
        if age is not None and age > MAX_FIX_AGE_SECONDS:
            signals.append({"code": "stale_fix",
                "detail": f"The location was taken {age // 60} minutes before the clock event, not for it.",
                "measured": {"fix_age_seconds": age}})
    if has_coordinates and previous is not None and previous.latitude is not None and previous.longitude is not None:
        distance = haversine_meters(float(latitude), float(longitude),
                                    float(previous.latitude), float(previous.longitude))
        elapsed = (occurred_at - previous.occurred_at).total_seconds()
        # A non-positive interval means the two readings are simultaneous or out of order, which is
        # a *sequence* problem the double-punch rules already own. Speed across it would be infinite,
        # and an arithmetic artefact must not be reported as a person's deception.
        if distance > MIN_TELEPORT_METERS and elapsed > 0:
            speed_kmh = Decimal(distance) / Decimal(1000) / (Decimal(elapsed) / Decimal(3600))
            if speed_kmh > IMPOSSIBLE_SPEED_KMH:
                signals.append({"code": "impossible_travel",
                    "detail": "Would have needed {:.0f} km/h to cover {:.1f} km in {} minutes.".format(
                        speed_kmh, distance / 1000, int(elapsed // 60)),
                    "measured": {"distance_m": int(distance), "elapsed_seconds": int(elapsed),
                                 "speed_kmh": str(speed_kmh.quantize(Decimal("1"))),
                                 "previous_punch": str(previous.pk)}})
    return signals


# ── CLK-1: the clock selfie ─────────────────────────────────────────────────────────
#
# The owner's ruling of 2026-10-04 (DD §Sites, geofences, clocks) is a frame at clock-in and
# clock-out, "if other methods weren't used", and not at breaks. Everything about this slice follows
# from that sentence, including the part that is *not* built here: a break is an annotation on a tour,
# not a punch, so there is nothing to exempt and no setting to hide it behind.
CLOCK_SELFIE_CODE = "clock_selfie"
# Ninety days, chosen because it is the horizon over which a tour is actually disputed — a pay-period
# query, an absence grievance, an insurer asking who stood the post — and short enough that a firm
# which never visits the settings page is not accumulating faces indefinitely. It is a starting value,
# not a policy: `DocumentType.retention_days` is editable by an owner or administrator from there up to
# permanent or down to a week, on the same screen that already shows every other record type's window
# and who can open it.
CLOCK_SELFIE_RETENTION_DAYS = 90
# A frame is evidence about a moment, so it has to be attached within one. Beyond this the photo could
# have been taken any time since the last punch, which is a different claim and should not be
# recorded as this one.
SELFIE_MAX_AGE_SECONDS = 120
# Well under the 25 MiB document ceiling: a phone's compressed frame is a few hundred kilobytes, so
# anything approaching this is not a selfie and should not be stored as one.
SELFIE_MAX_BYTES = 4 * 1024 * 1024
# Keyed on `verified_type` — what the bytes were *proved* to be — never on the declared content type,
# for the reason recorded at `PersonDocument.verified_type`: the browser's word is attacker-controlled.
SELFIE_VERIFIED_TYPES = ("image/jpeg", "image/png")
SELFIE_EVENT_KINDS = (Punch.Kind.IN, Punch.Kind.OUT)


def ensure_clock_selfie_type(organization):
    """The selfie's record type, created on first need rather than assumed to exist.

    It is a normal `DocumentType`, and that is the design: a frame then inherits the magic-signature
    check, the malware scan, the SHA-256, the per-type retention clock, the disclosure ladder and the
    retention/disposition review that every other personal record already goes through, instead of a
    second store with a second copy of each of those rules. Seeding it lazily also means a tenant that
    never turns the switch on is never asked to configure something it does not use.
    """
    document_type, _ = DocumentType.objects.get_or_create(
        organization=organization, code=CLOCK_SELFIE_CODE,
        defaults={"name": "Clock selfie", "audience": DocumentType.Audience.PERSON,
                  "sensitivity": DocumentType.Sensitivity.BIOMETRIC.value,
                  "retention_days": CLOCK_SELFIE_RETENTION_DAYS, "active": True})
    return document_type


def selfie_owed(*, policy, kind, identified_by_station=False):
    """Whether this punch has to carry a frame, given what already proves it.

    The ruling's own clause does the work: *if other methods weren't used*. A punch already
    attributed to a person by a verified station PIN has its answer to "who was this", from a device
    that is not theirs to lose or fake a face for, and a frame on top of it is friction that proves
    nothing new. So the exemption is not a loophole — it is the point of the sentence.

    There is deliberately no checkpoint term. A checkpoint scan is its own event kind, and
    `_resolve_checkpoint` refuses a checkpoint code on any other kind, so a scan can never also be a
    clock-in and the branch would be unreachable. An unreachable allowance is not a safety margin; it
    is a rule that reads as stricter than it is.
    """
    if kind not in SELFIE_EVENT_KINDS:
        return False
    if identified_by_station:
        return False
    return bool(policy.selfie["required"])


def discard_unattached_selfie(*, organization, person, document):
    """Delete a frame the officer took and then replaced, or never used at all.

    An unattached selfie is not evidence, so leaving it in storage means a face photograph with no
    purpose behind it, filed under a retention clock the owner set for *evidence*. The retake path is
    therefore a deletion path.

    Two refusals, both load-bearing. The document must be this person's own clock selfie — otherwise a
    crafted `replaces` id becomes an API for deleting arbitrary records out of somebody's personnel
    file, which is a far worse hole than the one it closes. And it must be attached to no punch: once a
    frame has been recorded as the evidence for a clock event it belongs to the audit story, and the
    officer's own device no longer gets to erase it. That case raises rather than no-opping, because it
    means the page and the server disagree about a punch that exists.
    """
    if document.organization_id != organization.pk or document.person_id != person.pk:
        raise ValidationError("That photo is not yours to discard.")
    if document.document_type.code != CLOCK_SELFIE_CODE:
        raise ValidationError("That record is not a clock photo.")
    if Punch.objects.filter(selfie=document).exists():
        raise ValidationError("That photo is already recorded against a punch.")
    document.file.delete(save=False)
    document.delete()
    return True


@transaction.atomic
def store_clock_selfie(*, organization, person, upload, actor=None, replaces=None):
    """Validate and file one clock frame, returning the document a punch can point at.

    Deliberately separate from the punch itself, and one round trip ahead of it: the camera has to
    hand back an id before the clock event is sent, so the punch and its evidence are written in the
    same breath and there is no window where a punch exists with a photo still to arrive — or, worse,
    where a refused upload leaves a punch that silently has none.

    There is no station twin of this route on purpose. The owner's exemption says a frame is owed
    "if other methods weren't used", and a station punch is by definition PIN-verified, so a camera at
    the kiosk would be a rule asking for evidence its own exemption already supplies.

    `replaces` is the retake path, and it runs **after** the new frame is stored: a photo the upload
    rejects must not have already destroyed the one the officer still had in hand. The whole function is
    transactional, so a rejection leaves both the old row and the old bytes exactly where they were.
    """
    if upload is None:
        raise ValidationError("No photo was received.")
    if upload.size > SELFIE_MAX_BYTES:
        raise ValidationError("A clock photo must be 4 MiB or smaller — the camera should send a "
                              "compressed frame, not a raw one.")
    previous = None
    if replaces:
        previous = PersonDocument.objects.filter(pk=replaces).first()
        if previous is None:
            raise ValidationError("The photo you were replacing is no longer here.")
    document = store_person_document(organization=organization, person=person,
        document_type=ensure_clock_selfie_type(organization), upload=upload, actor=actor)
    if document.verified_type not in SELFIE_VERIFIED_TYPES:
        # Unreachable by construction — `store_person_document` only returns a verified MIME that
        # passed the signature test, so anything but a JPEG or a PNG is refused here rather than
        # filed and deleted. Kept because the alternative is trusting an allowlist two files away.
        raise ValidationError("A clock photo must be a JPEG or a PNG.")
    if previous is not None:
        discard_unattached_selfie(organization=organization, person=person, document=previous)
    AuditEvent.objects.create(organization=organization, actor=actor, action="clock.selfie_captured",
        target_type="person_document", target_id=str(document.pk),
        metadata={"person": str(person.pk), "sha256": document.sha256, "bytes": document.size,
                  "verified_type": document.verified_type, "replaced": str(previous.pk) if previous else None,
                  "retention_days": document.document_type.retention_days})
    return document


def resolve_clock_selfie(*, organization, person, document, occurred_at, offline=False):
    """Prove a frame may stand as the evidence for *this* punch, or refuse the claim.

    Four things are checked and each has its own reason, because the interesting failures are not
    accidents:

    * **whose face** — a document filed against another officer would put one person's face on
      another person's time, which is the buddy-punching hole wearing better metadata;
    * **what kind of record** — an id scan or a claim file already in the personnel folder is not a
      clock photo, and attaching it would launder an old document into fresh evidence;
    * **already used** — the same frame for clock-in and clock-out is how a tour of twenty hours is
      invented from one photo, so a document is good for exactly one punch;
    * **how old** — see `SELFIE_MAX_AGE_SECONDS`.

    An offline punch cannot carry a frame at all, for the same reason CLK-2 refuses an offline PIN:
    the value of the artefact is that it reached the server at the moment it claims. A photo that
    arrives nine hours later with a queue of punched timestamps is indistinguishable from one taken
    during those nine hours, so the punch records without it and the exception says so.
    """
    if offline:
        raise ValidationError("A clock photo cannot be attached to an offline punch. "
                              "The photo is evidence about the moment it was sent, not one replayed later.")
    if document.organization_id != organization.pk:
        raise ValidationError("That photo does not belong to this company.")
    if document.person_id != person.pk:
        raise ValidationError("That photo is not this officer's.")
    if document.document_type.code != CLOCK_SELFIE_CODE:
        raise ValidationError("That record is not a clock photo.")
    if Punch.objects.filter(selfie=document).exists():
        raise ValidationError("That photo has already been used for another punch.")
    age = abs((occurred_at - document.created_at).total_seconds())
    if age > SELFIE_MAX_AGE_SECONDS:
        raise ValidationError(f"That photo was taken {int(age // 60)} minutes from the punch time, "
                              "which is too far to be evidence about it. Take another.")
    return document


@transaction.atomic
def record_punch(*, organization, person, client_event_id, kind, occurred_at, actor=None, shift=None, site=None, latitude=None, longitude=None, offline=False, source="web", evidence=None, accuracy_m=None, fix_age_seconds=None, selfie=None, checkpoint=None):
    from .models import PayrollRun
    # PAY-4: the lock is asked per row, not per period. A punch at a site whose branch has been left
    # open for correction is allowed while the rest of the run stays frozen, and the site — not the
    # officer's personnel file — decides, because that is what the client is billed against. With no
    # site to place the punch, the run's own status answers, which is the conservative direction: an
    # unattributable clock event is never a licence to edit an approved period.
    lock_site = (shift.site if shift is not None and shift.site_id else None) or \
                (site if site is not None and getattr(site, "pk", None) else None)
    state = payroll_lock_state(organization, occurred_at,
                               branch_id=getattr(lock_site, "branch_id", None),
                               client_id=getattr(lock_site, "client_id", None))
    if state["locked"]:
        raise ValidationError(f"This payroll period is locked — {state['by']}.")
    existing=Punch.objects.filter(client_event_id=client_event_id).first()
    if existing:
        if existing.organization_id != organization.id or existing.person_id != person.id:
            raise ValidationError("Client event ID is already in use.")
        return existing,False
    if person.organization_id != organization.id:
        raise ValidationError("Person belongs to another organization.")
    # CLK-2's rule, stated at the boundary every clock path goes through rather than only in the
    # kiosk view. A punch that names an officer and links a post assigned to somebody else would put
    # one person's time on another person's post — which is what a shared station makes easy and what
    # the PIN exists to prevent. Both the online and the offline path already took a shift id from
    # the page, so this was reachable by anyone who could open devtools; the kiosk would have been the
    # one surface that checked and the others the ones that didn't.
    if shift is not None and shift.officer_id != person.pk:
        raise ValidationError("A punch can only be recorded against a post assigned to that officer.")
    now=timezone.now()
    if occurred_at > now + CLOCK_SKEW_TOLERANCE:
        raise ValidationError("Punch time is ahead of the server clock; correct the device time.")
    if now-occurred_at > timedelta(hours=12):
        raise ValidationError("Offline punches must synchronize within 12 hours.")
    geofence_site = site or (shift.site if shift else None)
    # Resolved for the site the punch claims, before any exception is judged: whether a missing
    # location is an exception at all is the property's rule, not the company's.
    policy = effective_clock_policy(organization, geofence_site)
    review=Punch.Review.ACCEPTED; exceptions=[]
    if not shift: exceptions.append("Punch was not linked to a scheduled shift.")
    if kind == Punch.Kind.IN:
        prohibited=prohibited_credentials(person)
        if prohibited: raise ValidationError("Clock-in blocked: "+" ".join(prohibited))
        if shift:
            allowed,reasons=shift_eligibility(shift,person,purpose="clock")
            if not allowed: raise ValidationError("Clock-in blocked: "+" ".join(reasons))
    last=person.punches.filter(occurred_at__lt=occurred_at).order_by("-occurred_at").first()
    if kind == Punch.Kind.IN and last and last.kind == Punch.Kind.IN:
        exceptions.append("Previous shift was never closed with a clock-out.")
    if kind == Punch.Kind.OUT and (not last or last.kind != Punch.Kind.IN):
        exceptions.append("Clock-out has no matching clock-in.")
    if policy.require_geofence and geofence_site and geofence_site.latitude is not None and geofence_site.longitude is not None:
        if latitude is None or longitude is None:
            exceptions.append("Location was not supplied.")
        elif haversine_meters(latitude,longitude,geofence_site.latitude,geofence_site.longitude)>geofence_site.geofence_radius_meters:
            exceptions.append("Punch was outside the site geofence.")
    # CLK-4. Measured on every punch that carries coordinates, whatever the policy says, and stored
    # whatever it finds: the reading is a fact about this device at this moment, and a company that
    # later decides it wants to look should be able to look back. What the resolved rule controls is
    # whether a human is *bothered* now — one query, on the same index the double-punch check above
    # already uses, and only when there is a location to doubt.
    signals = []
    if latitude is not None and longitude is not None:
        previous = person.punches.filter(occurred_at__lt=occurred_at, latitude__isnull=False,
                                         longitude__isnull=False).order_by("-occurred_at").first()
        signals = location_risk_signals(occurred_at=occurred_at, latitude=latitude, longitude=longitude,
                                        accuracy_m=accuracy_m, fix_age_seconds=fix_age_seconds,
                                        offline=offline, previous=previous)
    if signals and policy.spoof_risk["flagged"]:
        exceptions.append("Location reading carries a spoof signal: " + "; ".join(entry["detail"] for entry in signals))
    # CLK-1. The frame is validated *before* the punch is written, because an attachment that turns out
    # to be somebody else's face, or nine hours old, or already used, is a refused claim rather than a
    # punch with bad evidence on it. `record_punch` is transactional, so the raise costs nothing.
    selfie_document = None
    if selfie is not None:
        selfie_document = resolve_clock_selfie(organization=organization, person=person, document=selfie,
                                               occurred_at=occurred_at, offline=offline)
    # A station punch is exempt by the ruling's own words: the PIN already proved who was standing
    # there. Read from the evidence the caller had to earn, not from a flag it could set — the kiosk
    # route passes `identified_by="pin"` only after `kiosk_identity()` has verified the token, so a
    # hand-built payload claiming a station is the one thing this cannot be fooled by.
    station_punch = bool((evidence or {}).get("kiosk")) and bool((evidence or {}).get("identified_by"))
    selfie_required = selfie_owed(policy=policy, kind=kind, identified_by_station=station_punch)
    if selfie_required and selfie_document is None:
        exceptions.append("A photo was required by the clock policy and none was captured"
                          + (", because this punch was collected offline" if offline else "") + ".")
    if exceptions: review=Punch.Review.PENDING
    punch=Punch.objects.create(organization=organization,person=person,shift=shift,client_event_id=client_event_id,kind=kind,occurred_at=occurred_at,latitude=latitude,longitude=longitude,offline=offline,review_status=review,exception_reason=(" ".join(exceptions))[:255],source=source,risk_flags=[entry["code"] for entry in signals],selfie=selfie_document,checkpoint=checkpoint)
    # The raw punch is immutable, so the rule it was judged against is recorded beside it rather
    # than written into it: "this was accepted because that property does not require a fence"
    # has to be provable after the override is edited or deleted.
    geofence=policy.geofence
    audit_metadata={"kind":kind,"offline":offline,"review":review,"exceptions":exceptions,"policy_source":geofence["source"],"policy_version":geofence["version"],"require_geofence":geofence["required"]}
    # CLK-2/CLK-1/CLK-4's single extension point. Every clock-evidence artefact is captured by a
    # different path (a PIN at a station, a selfie frame, a location reading with its accuracy) and
    # all three have to answer the same question on the review screen years later: "what made you
    # believe this punch?" Rather than bolt a column onto the raw punch per evidence type, the
    # evidence is described beside the event that judged it — immutable with the chain, and pointing
    # at its own stored row by id. Nothing here copies the artefact itself; a selfie stays in the
    # document store with the rest of the personal-record rules.
    evidence_block = dict(evidence or {})
    if selfie_document is not None or policy.selfie["required"]:
        # Whether a frame was owed, whether one arrived, and which rule said so — all three, because
        # "we required a photo and this officer did not provide one" is a claim about a policy that has
        # to survive the policy being edited. The digest is stored so a later reader can prove the
        # file behind this row is the one that was here, after the retention pass has deleted it.
        # The condition is the company rule rather than `selfie_required` so the *exemption* is
        # recorded too: a station punch that owed nothing because a PIN already identified the officer
        # is a decision, and a reviewer reading it two years later should be able to see that the frame
        # was waived rather than forgotten.
        photo = policy.selfie
        evidence_block["selfie"] = {"attached": selfie_document is not None, "required": selfie_required,
                                    "document": str(selfie_document.pk) if selfie_document else None,
                                    "sha256": selfie_document.sha256 if selfie_document else None,
                                    "verified_type": selfie_document.verified_type if selfie_document else None,
                                    "exempt_station": station_punch,
                                    "source": photo["source"], "version": photo["version"]}
    if signals:
        # The verdict alone would be indefensible later: which rule was in force when somebody was
        # told their phone looked wrong, and what exactly was measured, are both answers a claim about
        # deception has to carry. `flagged` records that the signal was *seen* and not sent for review,
        # which is the difference between a policy decision and a missed detection.
        spoof = policy.spoof_risk
        evidence_block["location_risk"] = {"signals": signals, "flagged": spoof["flagged"],
                                          "source": spoof["source"], "version": spoof["version"]}
    if evidence_block:
        audit_metadata["evidence"] = evidence_block
    AuditEvent.objects.create(organization=organization,actor=actor,action="punch.recorded",target_type="punch",target_id=str(punch.pk),metadata=audit_metadata)
    if review == Punch.Review.PENDING:
        # NTF-3: the exception already has a row, and today only the person who thinks to open
        # /time/review/ ever sees it. The officer is not the audience — they just made the punch —
        # so this goes to the dispatcher whose authority reaches the post, or to the compliance
        # roles when the punch matched no schedule at all.
        recipients = (dispatch_recipients_for_shift(shift, person) if shift else
                      role_recipients(organization, [Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR]))
        queue_notice(organization=organization, recipients=recipients, event_type="punch.exception",
            subject=f"Timecard exception: {person.full_name}",
            body=("{} at {} — {}. {}".format(
                    "Clock-in" if kind == Punch.Kind.IN else "Clock-out",
                    occurred_at.strftime("%d %b %Y %H:%M"), " ".join(exceptions),
                    "Synchronised from an offline device, so compare it against the device log." if offline
                    else "Review it in Time review.")),
            dedup_key=f"punch.exception:{punch.pk}")
    return punch,True

def round_minutes(minutes, policy):
    if policy.rounding_mode == TimePolicy.RoundingMode.EXACT or policy.rounding_minutes == 1:
        return minutes
    interval=Decimal(policy.rounding_minutes); value=Decimal(minutes)/interval
    modes={TimePolicy.RoundingMode.NEAREST:ROUND_HALF_UP,TimePolicy.RoundingMode.UP:ROUND_CEILING,TimePolicy.RoundingMode.DOWN:ROUND_FLOOR}
    return int(value.quantize(Decimal("1"),rounding=modes[policy.rounding_mode])*interval)

def payroll_rows(organization, start, end):
    """Timecard rows, one per officer *per post* — the unit a client is billed at.

    Overtime stays a person-week fact: the first ``overtime_after_hours`` of each workweek are
    regular whichever post they were worked at, and every minute after that is premium. Splitting
    the person's week across the posts they stood in chronological order keeps the totals exact
    and puts each hour against the rate that pays and bills it.
    """
    from zoneinfo import ZoneInfo
    policy,_=TimePolicy.objects.get_or_create(organization=organization,defaults={"timezone":organization.timezone})
    # Rounding is resolved per post, memoised per site: a contract that rounds to the nearest 15
    # and a warehouse on the company baseline can sit in the same pay period, and the number each
    # one produced has to be attributable to the rule and version that produced it.
    resolved={None:ResolvedClockPolicy([("company",policy)],policy)}
    def clock_policy(shift):
        site=shift.site if shift and shift.site_id else None
        key=site.pk if site is not None else None
        if key not in resolved: resolved[key]=effective_clock_policy(organization,site)
        return resolved[key]
    punches=organization.punches.filter(occurred_at__gte=start,occurred_at__lt=end,review_status=Punch.Review.ACCEPTED).select_related(
        "person","shift__site__client","shift__officer","shift__pay_code","shift__site__default_pay_code",
        "shift__site__client__default_pay_code").prefetch_related("adjustments")
    grouped=defaultdict(list)
    for punch in punches: grouped[punch.person].append(punch)
    # PAY-2. The company's own reading of each hour kind, and the designations made against the
    # posts in this period. Loaded once here rather than per officer: a roster of a hundred
    # guards re-querying the same five rows a hundred times would be the N+1 shape §11 already
    # had to justify twice, and there is no reason to earn it here.
    pay_categories={item.kind:item for item in organization.pay_categories.filter(active=True)}
    designations=defaultdict(dict)
    for designation in organization.hour_designations.filter(category__active=True).select_related("category"):
        designations[designation.shift_id][designation.category.kind]=designation
    # SCH-3. The reason a tour ran long, loaded with the same one-query-per-period discipline: the
    # hours themselves already come from the punches, so the row needs only the sentence explaining
    # them, and re-deriving it per officer would be a second loop over the same window.
    hold_overs=defaultdict(list)
    for row in organization.hold_overs.select_related("relief","recorded_by").order_by("created_at"):
        hold_overs[row.shift_id].append(row)
    # Post id → minutes paid for it in this window, filled as each officer's segments are bucketed.
    paid_minutes={}
    rows=[]
    for person,events in grouped.items():
        segments=[];open_in=None;open_in_time=None
        for event in events:
            approved=next((item for item in event.adjustments.all() if item.status=="approved"),None)
            event_time=approved.proposed_at if approved else event.occurred_at
            if event.kind==Punch.Kind.IN:
                open_in=event;open_in_time=event_time
            elif event.kind==Punch.Kind.OUT and open_in:
                in_adjustment=next((item for item in open_in.adjustments.all() if item.status=="approved"),None)
                in_time=in_adjustment.proposed_at if in_adjustment else open_in_time
                if event_time>=in_time:
                    worked=int((event_time-in_time).total_seconds()//60)
                    segment_policy=clock_policy(open_in.shift)
                    minutes=round_minutes(worked,segment_policy)
                    rounding=segment_policy.rounding
                    local_day=in_time.astimezone(ZoneInfo(policy.timezone)).date();days_since_start=(local_day.weekday()-policy.workweek_start)%7
                    segments.append({"shift":open_in.shift,"week":local_day-timedelta(days=days_since_start),"minutes":minutes,"raw":worked,
                                     "policy_source":rounding["source"],"policy_version":rounding["version"],
                                     "rounding_mode":rounding["mode"],"rounding_minutes":rounding["minutes"]})
                    open_in=None;open_in_time=None
        # PAY-2, applied *before* the week is totalled, because whether an hour counts toward the
        # overtime threshold is a question about the threshold and not about the line it prints on.
        # Two of the three category shapes remove time from the worked pool and one does not, and
        # that difference is the whole design (see ``PayCategory``): an unpaid break is not hours
        # worked at all, paid-but-not-counting hours leave the basis and still pay, and a premium
        # category leaves its hours exactly where they are and prices only what sits above straight
        # time — so an hour is never counted twice and never quietly disappears.
        owed={shift_id:{kind:int(row.hours*60) for kind,row in designations.get(shift_id,{}).items()}
              for shift_id in {segment["shift"].pk for segment in segments if segment["shift"]}}
        carved=defaultdict(lambda:defaultdict(int))
        for segment in segments:
            shift_id=segment["shift"].pk if segment["shift"] else None
            if shift_id is None: continue
            for kind,remaining in list(owed[shift_id].items()):
                category=pay_categories.get(kind)
                if category is None or remaining<=0: continue
                if category.paid and category.counts_toward_overtime:
                    continue
                # Take it in chronological order, and never more than the segment holds: a tour
                # recorded as two in/out pairs must not lose the tail of a four-hour break to a
                # two-hour segment that had already been emptied.
                take=min(remaining,segment["minutes"])
                if take<=0: continue
                segment["minutes"]-=take
                owed[shift_id][kind]=remaining-take
                carved[shift_id][kind]+=take
        weekly=defaultdict(int)
        for segment in segments: weekly[segment["week"]] += segment["minutes"]
        # Allocate regular first in chronological order per workweek, then charge the remainder
        # as premium, so a guard split across two posts still gets exactly one OT threshold.
        remaining={week:int(round(policy.overtime_after_hours*60)) for week in weekly}
        allocated=[]
        for segment in segments:
            room=remaining[segment["week"]]
            regular=min(segment["minutes"],room);overtime=segment["minutes"]-regular
            remaining[segment["week"]]-=regular
            allocated.append((segment,regular,overtime))
        buckets={}
        for segment,regular,overtime in allocated:
            shift=segment["shift"]
            bucket=buckets.setdefault(shift.pk if shift else None,{"regular":0,"overtime":0,"raw":0,"shift":shift,
                                                                   "policy_source":segment["policy_source"],"policy_version":segment["policy_version"],
                                                                   "rounding_mode":segment["rounding_mode"],"rounding_minutes":segment["rounding_minutes"]})
            bucket["regular"]+=regular;bucket["overtime"]+=overtime;bucket["raw"]+=segment["raw"]
        for bucket in buckets.values():
            shift=bucket["shift"]
            if shift:
                # Minutes this post was actually paid for, kept for the leave calculation: time the
                # officer worked inside an approved absence is worked time, and paying the displaced
                # hours again would pay the same hour twice.
                paid_minutes[shift.pk]=bucket["regular"]+bucket["overtime"]
            rates=effective_rates(shift,officer=person) if shift else {"pay_rate":Decimal(person.hourly_rate) if person.hourly_rate is not None else None,"pay_source":"officer" if person.hourly_rate is not None else "","bill_rate":None,"bill_source":""}
            regular=Decimal(bucket["regular"])/Decimal(60);overtime=Decimal(bucket["overtime"])/Decimal(60)
            hours=regular+overtime
            pay=rates["pay_rate"];bill=rates["bill_rate"]
            # PAY-5: the multiple comes from policy, not from the arithmetic. 1.50 stays the
            # default because a weekly-threshold employer without §7(k) is what the FLSA actually
            # requires; a contract paying double time over its own trigger sets 2.00 without a
            # code change. The value is revision-watched, so a stub stamped version 4 can be read
            # back against the premium that was in force when it was produced.
            premium=Decimal(policy.overtime_premium)
            estimated_pay=(regular*pay+overtime*pay*premium).quantize(Decimal("0.01")) if pay is not None else ""
            estimated_bill=(hours*bill).quantize(Decimal("0.01")) if bill is not None else ""
            site=shift.site if shift and shift.site_id else None
            # SCH-3's done-criterion: the reason a tour ran long reaches the timecard row, not only a
            # log nobody opens at payroll. The held-over hours are already inside raw_hours — the
            # punches recorded them — so this adds no time, it explains the time that is there.
            notes = (["Open punch"] if open_in else []) + (
                [describe_hold_over(entry) for entry in hold_overs.get(shift.pk, [])] if shift else [])
            rows.append({
                "employee_id":str(person.pk),"employee":person.full_name,
                # The job code the customer's payroll keys the line on, and which level set it.
                **(effective_pay_code(shift) if shift else {"pay_code":"","pay_code_name":"","cost_centre":"","pay_code_source":""}),
                "pay_category":"worked",
                "client":(site.client.name if site else ""),"site":(site.name if site else ""),"post":(shift.post_name if shift else ""),
                # The worked duration sits beside the paid one on purpose: a wage record that
                # shows only the rounded figure cannot be reconciled against the punches, and
                # the two differ on nearly every shift under any interval but "exact".
                "raw_hours":(Decimal(bucket["raw"])/Decimal(60)).quantize(Decimal("0.01")),
                "regular_hours":regular.quantize(Decimal("0.01")),"overtime_hours":overtime.quantize(Decimal("0.01")),"total_hours":hours.quantize(Decimal("0.01")),
                "rounding_mode":bucket["rounding_mode"],"rounding_minutes":bucket["rounding_minutes"],
                "policy_source":bucket["policy_source"],"policy_version":bucket["policy_version"],
                "pay_rate":pay if pay is not None else "","pay_rate_source":rates["pay_source"],
                "bill_rate":bill if bill is not None else "","bill_rate_source":rates["bill_source"],
                "estimated_pay":estimated_pay,"estimated_bill":estimated_bill,
                "margin":(Decimal(estimated_bill)-Decimal(estimated_pay)).quantize(Decimal("0.01")) if estimated_bill!="" and estimated_pay!="" else "",
                "exception":"; ".join(notes),
            })
            # PAY-2: the category lines for this post. A carved category carries hours that *left*
            # the worked row, so it is where they are priced; a premium category keeps its hours in
            # the worked row and adds only the excess over straight time. The two cases cannot share
            # a formula, and getting that wrong either double-counts an hour or loses it. Each line
            # names the rule version that priced it and the reason it was marked, because "we always
            # pay holiday at 1.5×" is a claim about a rule that can change and about a decision that
            # somebody made on a specific night.
            for kind,designation in (designations.get(shift.pk,{}) if shift else {}).items():
                category=pay_categories.get(kind)
                if category is None: continue
                overlay=category.paid and category.counts_toward_overtime
                carved_minutes=carved.get(shift.pk,{}).get(kind,0)
                category_hours=(designation.hours if overlay else Decimal(carved_minutes)/Decimal(60)).quantize(Decimal("0.01"))
                line=dict(rows[-1])
                line["pay_category"]=kind
                line["raw_hours"]=category_hours
                line["regular_hours"]=category_hours if (category.paid and not overlay) else Decimal("0.00")
                line["overtime_hours"]=Decimal("0.00")
                line["total_hours"]=Decimal("0.00") if overlay else category_hours
                if pay is None:
                    line["estimated_pay"]="";line["estimated_bill"]=""
                elif overlay:
                    line["estimated_pay"]=(designation.hours*pay*(Decimal(category.multiplier)-Decimal("1"))).quantize(Decimal("0.01"))
                    # Billed at the worked line's rate already: the hours never left it, so adding a
                    # bill here would invoice the client for the same hour twice.
                    line["estimated_bill"]=""
                else:
                    line["estimated_pay"]=((category_hours*pay*Decimal(category.multiplier)).quantize(Decimal("0.01"))
                                           if category.paid else Decimal("0.00"))
                    line["estimated_bill"]=((category_hours*Decimal(bill)).quantize(Decimal("0.01"))
                                            if (bill is not None and category.paid) else "")
                line["margin"]=""
                shortfall=(int(designation.hours*60)-carved_minutes) if not overlay else 0
                line["exception"]=(f"{category_hours}h {category.name} designated on this post — {designation.reason} "
                                    f"(rule v{category.revision} at {category.multiplier}×, "
                                    f"{'paid' if category.paid else 'unpaid'}, "
                                    f"{'counts' if category.counts_toward_overtime else 'excluded'} toward overtime)"
                                    + (f"; only {Decimal(carved_minutes)/Decimal(60)}h of {designation.hours}h fitted "
                                       "inside the recorded time" if shortfall>0 else ""))
                rows.append(line)
    # PAY-3, priced by PAY-2's ruling of 2026-10-03: approved leave is time the firm agreed to and the
    # clock does not show, and it is paid on **the hours it displaced** — not on the calendar span the
    # absence covers. That distinction is the whole calculation. A fortnight's vacation spans 336
    # calendar hours; multiplying those by a rate would pay an officer for every night and weekend
    # inside it, and the row would be a number no contract promises.
    #
    # So the basis is the officer's own scheduled posts inside the approved span, clipped to the window
    # being exported, minus the part of that span they actually worked and were paid for. Each displaced
    # post is priced at its own resolved rate (a leave that covered two properties at different rates
    # should not be averaged), and the whole thing is governed by the firm's `leave` pay category:
    # paidness and multiple as a rule with a version, the same shape every other kind got.
    #
    # Two cases produce no money, and the row says which. Nothing was scheduled — the honest answer for
    # a hire two periods out, where there is no post to displace and no basis to invent. Or it was all
    # worked — the absence was granted and the officer turned up anyway, which is a scheduling fact the
    # worked rows already carry. Both keep the row itself, because a granted absence with no trace at
    # payroll reads as unexcused, and that is the wrong answer in both directions.
    leave_category = pay_categories.get(PayCategory.Kind.LEAVE)
    for leave in TimeOffRequest.objects.filter(organization=organization,
            status=TimeOffRequest.Status.APPROVED, starts_at__lt=end, ends_at__gt=start
            ).select_related("person"):
        overlap_start = max(leave.starts_at, start)
        overlap_end = min(leave.ends_at, end)
        if overlap_end <= overlap_start:
            continue
        calendar_hours = (Decimal((overlap_end - overlap_start).total_seconds()) / Decimal(3600)).quantize(Decimal("0.01"))
        displaced = {}
        scheduled_minutes = 0
        for post in organization.shifts.filter(officer=leave.person, starts_at__lt=overlap_end,
                ends_at__gt=overlap_start).exclude(status=Shift.Status.CANCELLED):
            if post.status == Shift.Status.DRAFT:
                # A draft is a plan, and a plan is not a promise to pay somebody for staying away from
                # it. coverage_state applies the same exclusion to the schedule for the same reason.
                continue
            minutes = int((min(post.ends_at, overlap_end) - max(post.starts_at, overlap_start)).total_seconds() // 60)
            if minutes <= 0:
                continue
            scheduled_minutes += minutes
            worked = min(minutes, paid_minutes.get(post.pk, 0))
            if minutes - worked > 0:
                displaced[post] = minutes - worked
        displaced_hours = sum(displaced.values())
        pay = Decimal("0.00")
        if leave_category is not None and leave_category.paid:
            for post, minutes in displaced.items():
                rate = effective_rates(post, officer=leave.person)["pay_rate"]
                if rate is not None:
                    pay += (Decimal(minutes) / Decimal(60) * Decimal(rate) * Decimal(leave_category.multiplier))
        if leave_category is None:
            # Seeded lazily by the screens that edit it, so a firm that has never opened Hour
            # categories has no leave rule at all — which is a different statement from "this firm
            # pays nothing for leave", and saying the wrong one would let a missing configuration read
            # as a decision.
            reason = "this company has no leave pay rule configured yet, so no money is claimed for it"
        elif not displaced and scheduled_minutes:
            reason = "the scheduled time inside this span was worked and is on the worked rows"
        elif not displaced:
            reason = "no post was scheduled for this officer inside the span, so there is no payable basis"
        elif not leave_category.paid:
            reason = "leave is marked unpaid here"
        else:
            reason = (f"{Decimal(displaced_hours) / Decimal(60):.2f} displaced hours across "
                      f"{len(displaced)} post{'s' if len(displaced) != 1 else ''} "
                      f"(rule v{leave_category.revision} at {leave_category.multiplier}×)")
        rows.append({
            "employee_id": str(leave.person_id), "employee": leave.person.full_name,
            "pay_code": "", "pay_code_name": "", "cost_centre": "", "pay_code_source": "",
            "pay_category": "leave",
            "client": "", "site": "", "post": "Approved leave",
            # The payable basis, not the span: raw_hours on a leave row now means the same kind of
            # figure it means on a worked row — hours that count — and the calendar span it clipped
            # moves into the note where it is still readable but can no longer be mistaken for a total.
            "raw_hours": (Decimal(displaced_hours) / Decimal(60)).quantize(Decimal("0.01")),
            "regular_hours": (Decimal(displaced_hours) / Decimal(60)).quantize(Decimal("0.01")),
            # Never overtime, whatever the category says: see the limit stated in feature-status.md.
            "overtime_hours": Decimal("0.00"),
            "total_hours": (Decimal(displaced_hours) / Decimal(60)).quantize(Decimal("0.01")),
            "rounding_mode": "", "rounding_minutes": "", "policy_source": "", "policy_version": "",
            # No rate on the row: the posts it displaced can each have carried a different one, and
            # showing the first or the average would be an invented figure the export cannot defend.
            "pay_rate": "", "pay_rate_source": "", "bill_rate": "", "bill_rate_source": "",
            "estimated_pay": pay.quantize(Decimal("0.01")) if (displaced and leave_category is not None
                                                               and leave_category.paid) else Decimal("0.00"),
            # An absence is never billed on. The client is charged for the officer who stood the post,
            # and a leave row is the firm's cost, not a line on somebody's invoice.
            "estimated_bill": "", "margin": "",
            "exception": (f"Approved leave {overlap_start:%b %d} – {overlap_end:%b %d %H:%M}; "
                          f"covers {calendar_hours} calendar hours, which is not a payable figure — {reason}"),
        })
    return rows


def payroll_totals(rows, by="pay_code"):
    """Hours and money grouped by one dimension, for the screen and the export footer.

    ``docs/discovery-decisions.md`` requires reports to group "by employee, branch, site, and pay
    code". A pay code with hours but no rate on it is shown rather than dropped, because a line the
    clerk cannot price is exactly the thing they need to see.
    """
    totals = {}
    for row in rows:
        key = row.get(by) or ("(no " + by + ")" if by != "pay_category" else "(none)")
        bucket = totals.setdefault(key, {by if by != "pay_category" else "category": key,
            "pay_code_name": row.get("pay_code_name", ""), "cost_centre": row.get("cost_centre", ""),
            "people": set(), "total_hours": Decimal("0.00"), "regular_hours": Decimal("0.00"),
            "overtime_hours": Decimal("0.00"), "estimated_pay": Decimal("0.00"),
            "estimated_bill": Decimal("0.00"), "rows": 0})
        bucket["people"].add(row.get("employee", ""))
        bucket["rows"] += 1
        for field in ("total_hours", "regular_hours", "overtime_hours"):
            value = row.get(field, "")
            if value != "":
                bucket[field] += Decimal(value)
        for field in ("estimated_pay", "estimated_bill"):
            value = row.get(field, "")
            if value != "":
                bucket[field] += Decimal(value)
    return sorted(({"key": item[0], **{field: value for field, value in item[1].items() if field != "people"},
                    "employee_count": len(item[1]["people"])} for item in totals.items()),
                  key=lambda entry: -entry["total_hours"])

@transaction.atomic
def create_payroll_run(*,organization,start,end,actor):
    from .models import AuditEvent, PayrollRun, Punch
    rows=payroll_rows(organization,start,end)
    exceptions=[{"employee":row["employee"],"reason":row["exception"]} for row in rows if row["exception"]]
    pending=organization.punches.filter(occurred_at__gte=start,occurred_at__lt=end,review_status=Punch.Review.PENDING).count()
    if pending: exceptions.append({"employee":"Multiple","reason":f"{pending} punches await review"})
    serializable=[{key:str(value) if isinstance(value,Decimal) else value for key,value in row.items()} for row in rows]
    run,created=PayrollRun.objects.get_or_create(organization=organization,period_start=start,period_end=end,defaults={"created_by":actor,"snapshot":serializable,"exceptions":exceptions})
    if not created and run.status==PayrollRun.Status.DRAFT:
        run.snapshot=serializable;run.exceptions=exceptions;run.save(update_fields=["snapshot","exceptions"])
    AuditEvent.objects.create(organization=organization,actor=actor,action="payroll.generated",target_type="payroll_run",target_id=str(run.pk),metadata={"exceptions":len(exceptions)})
    return run

@transaction.atomic
def reopen_payroll_run(run, actor, reason):
    """Unlock an approved period so corrections and late punches can be reviewed again.

    ``docs/discovery-decisions.md`` allows a locked period to be reopened "when configuration
    allows it", requiring a reason, privileged approval, and an audit event — so the gate is the
    organization's own ``TimePolicy.allow_reopen`` (off by default), the actor is an owner or
    administrator rather than the payroll approver who locked it, and the reason has to be long
    enough to survive being read a quarter later. The approval itself is cleared rather than
    hidden: a draft run must not present a name that no longer stands behind it.
    """
    from .models import AuditEvent, PayrollRun, TimePolicy
    if run.status == PayrollRun.Status.DRAFT:
        raise ValidationError("This period is not locked.")
    policy = TimePolicy.objects.filter(organization=run.organization).first()
    if not policy or not policy.allow_reopen:
        raise ValidationError("Reopening a locked period is not enabled for this company (Time and payroll policy).")
    if len(reason.strip()) < 10:
        raise ValidationError("Give a reopening reason of at least 10 characters.")
    previous = run.status
    run.status = PayrollRun.Status.DRAFT
    run.approved_by = None; run.approved_at = None
    run.reopened_by = actor; run.reopened_at = timezone.now()
    run.reopen_reason = reason.strip(); run.reopen_count += 1
    # The old snapshot stays put — it is what the client was billed from, and erasing it would
    # destroy the record the reopening is about. An exception row is what forces it to be
    # regenerated: approval refuses while an exception is open, so nobody approves the reopened
    # period on numbers that predate the correction they unlocked it for.
    run.exceptions = list(run.exceptions or []) + [{"employee": "—", "reason": f"Reopened by {actor} for the period to be regenerated before it is approved again."}]
    run.save(update_fields=["status","approved_by","approved_at","reopened_by","reopened_at","reopen_reason","reopen_count","exceptions"])
    AuditEvent.objects.create(organization=run.organization, actor=actor, action="payroll.reopened",
        target_type="payroll_run", target_id=str(run.pk),
        metadata={"previous_status": previous, "reason": run.reopen_reason, "times_reopened": run.reopen_count,
                  "rows": len(run.snapshot), "exported": bool(run.exported_at)})
    queue_notice(organization=run.organization,recipients=payroll_recipients(run.organization)-{actor.pk},event_type="payroll.reopened",
        subject=f"Payroll reopened for {run.period_start.date()} – {run.period_end.date()}",
        body=(f"Reopened by {actor} (time {run.reopen_count}): {run.reopen_reason} "
              f"The approved figure for this period no longer stands; the snapshot has to be regenerated and re-approved."),
        dedup_key=f"payroll.reopened:{run.pk}:{run.reopen_count}")
    return run

@transaction.atomic
def approve_payroll_run(run,actor):
    from .models import AuditEvent, PayrollRun
    if run.status!=PayrollRun.Status.DRAFT: raise ValidationError("Only draft payroll runs can be approved.")
    if run.exceptions: raise ValidationError("Resolve payroll exceptions before approval.")
    run.status=PayrollRun.Status.APPROVED;run.approved_by=actor;run.approved_at=timezone.now();run.save(update_fields=["status","approved_by","approved_at"])
    AuditEvent.objects.create(organization=run.organization,actor=actor,action="payroll.approved",target_type="payroll_run",target_id=str(run.pk),metadata={"rows":len(run.snapshot)})
    # NTF-3: the lock and the unlock are payroll *state*, and today neither reaches anyone who is
    # not already looking at the payroll screen. The reopen matters most — it tells the approver
    # that the figure they approved is no longer the one that stands.
    queue_notice(organization=run.organization,recipients=payroll_recipients(run.organization)-{actor.pk},event_type="payroll.locked",
        subject=f"Payroll locked for {run.period_start.date()} – {run.period_end.date()}",
        body=f"{len(run.snapshot)} employee row(s) approved and locked by {actor}. Corrections now require the period to be reopened.",
        dedup_key=f"payroll.locked:{run.pk}")
    return run


# ── Partial payroll locks (roadmap §2, PAY-4) ────────────────────────────────────

def lock_subject(row):
    """The branch and contract a time row belongs to, or ``(None, None)`` when nothing says.

    Read through the post the punch was clocked against, because that is the only chain the product
    controls: person → branch is where a guard is *filed*, and a guard from North standing a post at
    the South gate is billed and locked with the work, not with the personnel file. Contract comes
    from the site's client for the same reason.
    """
    shift = getattr(row, "shift", None)
    site = getattr(shift, "site", None) if shift is not None else getattr(row, "site", None)
    if site is None:
        return None, None
    return site.branch_id, site.client_id


def payroll_lock_state(organization, at, branch_id=None, client_id=None):
    """Whether one row of time is frozen here, and what froze it. PAY-4's single answer.

    Every gate that used to ask ``PayrollRun.status`` asks this instead. The run's status can only ever
    say "the whole period", and since PAY-4 a period can be half agreed, so a gate reading the run
    alone would refuse a correction the firm has deliberately left open — or, worse, allow one inside a
    slice it has already paid.

    Precedence is specific-first: the branch's own segment, then the contract's, then a company-wide
    segment (the "everything else" row), then the run's status. A row that cannot be placed at a branch
    or a contract takes the run-level answer on purpose: an unclassifiable punch is not a licence to
    edit a locked period, and the conservative direction is the one that cannot destroy an approved
    figure.
    """
    from .models import PayrollLockSegment, PayrollRun
    run = PayrollRun.objects.filter(organization=organization, period_start__lte=at,
                                    period_end__gt=at).order_by("-period_end").first()
    if run is None:
        return {"locked": False, "run": None, "segment": None, "by": ""}
    segments = list(run.lock_segments.select_related("branch", "client", "decided_by"))
    segment = None
    for want, key in ((branch_id, "branch_id"), (client_id, "client_id")):
        if not want:
            continue
        segment = next((item for item in segments if getattr(item, key) == want), None)
        if segment is not None:
            break
    if segment is None:
        segment = next((item for item in segments if item.is_company_wide), None)
    if segment is not None:
        return {"locked": segment.status == PayrollLockSegment.Status.LOCKED, "run": run,
                "segment": segment,
                "by": f"{segment.subject_label} segment, {segment.get_status_display().lower()}"
                      f" by {segment.decided_by or '—'}"}
    locked = run.status in (PayrollRun.Status.APPROVED, PayrollRun.Status.EXPORTED)
    return {"locked": locked, "run": run, "segment": None,
            "by": f"the {run.get_status_display().lower()} period" if locked else ""}


@transaction.atomic
def set_payroll_lock_segment(run, actor, status, reason, branch=None, client=None):
    """Agree or re-open one slice of a period without touching the rest of it.

    The row is replaced rather than appended: two segments for one branch could disagree, and there is
    no defensible answer to "which one stands". The run's own history keeps the audit — every decision
    here writes an event, so the superseded state is still readable.
    """
    from .models import AuditEvent, PayrollLockSegment
    if status not in PayrollLockSegment.Status.values:
        raise ValidationError({"status": "Choose locked or open."})
    existing = PayrollLockSegment.objects.filter(run=run, branch=branch, client=client).first()
    row = existing or PayrollLockSegment(run=run, organization=run.organization, branch=branch, client=client)
    row.status = status
    row.reason = (reason or "").strip()[:255]
    row.decided_by = actor
    row.full_clean()
    row.save()
    AuditEvent.objects.create(
        organization=run.organization, actor=actor,
        action="payroll.segment_locked" if status == PayrollLockSegment.Status.LOCKED else "payroll.segment_opened",
        target_type="payroll_run", target_id=str(run.pk),
        metadata={"segment": str(row.pk), "subject": row.subject_label, "status": status,
                  "reason": row.reason, "run_status": run.status})
    return row


def open_lock_segments(run):
    """The slices somebody has deliberately left editable inside an agreed period.

    One function because the export gate and the payroll screen must agree about what they are warning
    about — a file handed to a customer while part of the period is still being corrected is the
    specific thing a partial lock makes possible, and the check has to be as obvious as the feature.
    """
    from .models import PayrollLockSegment
    return list(run.lock_segments.filter(status=PayrollLockSegment.Status.OPEN)
                .select_related("branch", "client", "decided_by"))

def payroll_csv(organization,start,end):
    output=StringIO(); writer=csv.DictWriter(output,fieldnames=PAYROLL_EXPORT_FIELDS); writer.writeheader(); writer.writerows({field:safe_cell(row.get(field,"")) for field in PAYROLL_EXPORT_FIELDS} for row in payroll_rows(organization,start,end)); return output.getvalue()

# The bill side sits beside the pay side on purpose: the vertical's whole argument is that
# overtime paid at a premium and billed at straight time is invisible until both rates are on
# the same line. raw_hours and the policy columns are here because a rounded figure cannot be
# audited without the duration it was rounded from and the rule, and version of it, that did it.
PAYROLL_EXPORT_FIELDS=["employee_id","employee","pay_category","pay_code","pay_code_name","cost_centre","pay_code_source",
                       "client","site","post","raw_hours","regular_hours","overtime_hours","total_hours",
                       "rounding_mode","rounding_minutes","policy_source","policy_version",
                       "pay_rate","pay_rate_source","bill_rate","bill_rate_source","estimated_pay","estimated_bill","margin","exception"]
PAYROLL_PDF_FIELDS=["employee","pay_category","pay_code","client","site","total_hours","overtime_hours","pay_rate","bill_rate","estimated_pay","estimated_bill","margin"]

def safe_cell(value):
    """Stop spreadsheet formula injection: personnel names and imported identifiers reach
    exports verbatim, and a leading =, +, -, @ or tab is executed by Excel and LibreOffice."""
    text="" if value is None else str(value)
    return "'"+text if text[:1] in ("=","+","-","@","\t","\r") else text

def payroll_snapshot_csv(rows):
    output=StringIO();writer=csv.DictWriter(output,fieldnames=PAYROLL_EXPORT_FIELDS);writer.writeheader();writer.writerows({field:safe_cell(row.get(field,"")) for field in PAYROLL_EXPORT_FIELDS} for row in rows);return output.getvalue().encode("utf-8")

def payroll_snapshot_xlsx(rows):
    """Create a small standards-compliant XLSX without adding a report dependency."""
    import zipfile
    from io import BytesIO
    from xml.sax.saxutils import escape
    values=[PAYROLL_EXPORT_FIELDS]+[[safe_cell(row.get(field,"")) for field in PAYROLL_EXPORT_FIELDS] for row in rows]
    xml_rows=[]
    for number,row in enumerate(values,1):
        cells=[]
        for column,value in enumerate(row,1):
            letters="";n=column
            while n:n,remainder=divmod(n-1,26);letters=chr(65+remainder)+letters
            cells.append(f'<c r="{letters}{number}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
        xml_rows.append(f'<row r="{number}">{"".join(cells)}</row>')
    sheet='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+"".join(xml_rows)+"</sheetData></worksheet>"
    files={"[Content_Types].xml":'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',"_rels/.rels":'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',"xl/workbook.xml":'<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Payroll" sheetId="1" r:id="rId1"/></sheets></workbook>',"xl/_rels/workbook.xml.rels":'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',"xl/worksheets/sheet1.xml":sheet}
    output=BytesIO()
    with zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED) as archive:
        for name,content in files.items():archive.writestr(name,content)
    return output.getvalue()

def payroll_snapshot_pdf(rows):
    """Render a portable, dependency-free text PDF for payroll handoff.

    The PDF carries the money view rather than all sixteen export columns: a fixed-width text
    page cannot fit them legibly, and the columns that matter on paper are who worked where and
    what it paid against what it billed.
    """
    lines=["Payroll report"," | ".join(PAYROLL_PDF_FIELDS)]
    lines.extend(" | ".join(str(row.get(field,"")) for field in PAYROLL_PDF_FIELDS) for row in rows)
    def pdf_escape(value): return value.replace("\\","\\\\").replace("(","\\(").replace(")","\\)")
    commands=["BT /F1 9 Tf 36 756 Td"]
    for index,line in enumerate(lines): commands.append(f"{'0 -14 Td ' if index else ''}({pdf_escape(line[:150])}) Tj")
    commands.append("ET");stream="\n".join(commands).encode()
    objects=[b"<< /Type /Catalog /Pages 2 0 R >>",b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",f"<< /Length {len(stream)} >>\nstream\n".encode()+stream+b"\nendstream",b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    output=bytearray(b"%PDF-1.4\n");offsets=[0]
    for number,obj in enumerate(objects,1):offsets.append(len(output));output.extend(f"{number} 0 obj\n".encode()+obj+b"\nendobj\n")
    xref=len(output);output.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode());return bytes(output)

ALLOWED_DOCUMENT_SIGNATURES = {
    ".pdf": (b"%PDF-",), ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",),
    ".docx": (b"PK\x03\x04",), ".xlsx": (b"PK\x03\x04",),
    ".csv": tuple(), ".txt": tuple(),
}

# One ceiling, named, because the validator and the re-encode below both have to enforce it: an image
# that shrinks or grows on re-save must still be under the size the storage path and the export zip
# assume.
DOCUMENT_MAX_BYTES = 25 * 1024 * 1024

# The MIME each extension was *proved* to be, recorded on the row at upload. Kept next to the
# signatures rather than in a form or a view because the pair must never disagree: an entry here with
# no signature above it would claim a verification that nothing performed.
#
# `.csv` and `.txt` have no magic to check, so their value is a statement about how the bytes will be
# *served* rather than about what they are: `text/plain` with `nosniff` means a file that is really
# markup renders as inert characters instead of being sniffed into a document. That is the only reason
# a signatureless extension belongs on this list at all.
VERIFIED_DOCUMENT_TYPES = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/plain", ".txt": "text/plain",
}

# Owner ruling of 2026-10-03: permission to read is permission to view, which supersedes the standing
# "uploads are never served inline" posture. The list is deliberately narrower than what may be
# *stored*, and the omissions are the point:
#
#   * Office documents are a zip container whose real contents cannot be reasoned about from sixteen
#     head bytes, so they stay download-only.
#   * Anything scriptable is absent outright. SVG would be the dangerous one — a valid signature of
#     its own and executable content by design — and it is not even storable.
#   * `text/plain` covers csv/txt for the serving reason recorded above.
#
# Mapped to a *kind* rather than a boolean so the template picks the right element (object, img, pre)
# without inventing its own idea of what is safe.
PREVIEW_INLINE_TYPES = {
    "application/pdf": "pdf",
    "image/png": "image",
    "image/jpeg": "image",
    "text/plain": "text",
}

# How long a preview link stays valid. Short on purpose: once the browser holds the signature, that
# signature is the only thing authorizing the byte fetch, so the window is the exposure. Ordinary
# storage URLs use AWS_QUERYSTRING_EXPIRE (300s); a preview earns less because it renders.
PREVIEW_URL_SECONDS = 90


class PreviewUnavailable(Exception):
    """This record cannot be shown in the page, so the caller offers the download instead."""


def validate_document_upload(upload):
    from pathlib import Path
    if upload.size > DOCUMENT_MAX_BYTES: raise ValidationError("Documents must be 25 MiB or smaller.")
    extension=Path(upload.name).suffix.lower()
    if extension not in ALLOWED_DOCUMENT_SIGNATURES: raise ValidationError("Unsupported document type.")
    head=upload.read(16); upload.seek(0)
    signatures=ALLOWED_DOCUMENT_SIGNATURES[extension]
    if signatures and not any(head.startswith(signature) for signature in signatures):
        raise ValidationError("File contents do not match the extension.")
    if b"<script" in upload.read(4096).lower(): upload.seek(0); raise ValidationError("Active content is not allowed.")
    upload.seek(0)
    # The caller records this as the row's `verified_type`. Returning it from inside the validator
    # rather than recomputing it at the write site is what keeps the claim honest: the only way to get
    # a verified MIME out of here is to have passed the signature check above.
    return VERIFIED_DOCUMENT_TYPES[extension]


# The two storable formats that carry a camera's diary. `.heic` and `.tif` are not on this list because
# the extension allowlist above will not accept them at all — an iPhone photo reaches this application
# only after the browser or the office has converted it, so there is no exotic container left to
# special-case here.
METADATA_BEARING_TYPES = {"image/jpeg": "JPEG", "image/png": "PNG"}


def strip_document_metadata(upload, verified_type):
    """Return the bytes to store, with an image's embedded metadata removed.

    Why this exists at all: a licence, a diploma or a registration photo is taken on a phone, in a
    doorway, at a kitchen table or at a vehicle, and the phone writes the place into the file. The
    stored artefact then defaults to `standard` sensitivity, whose staff list includes **auditor** —
    a role the ladder itself describes as often an outside accountant — and the same bytes travel into
    `personnel_file_zip`. So an external party can be handed the coordinates where an officer's
    personal documents were photographed, without any decision ever being taken to disclose them.

    The mechanism is the one `process_brand_image` already proves in this file — open, verify,
    re-open, `exif_transpose`, re-save with no metadata argument — with one difference that matters.
    A logo becomes a canonical PNG because it is *rendered*; a personnel record has to come back out of
    storage as the same kind of file it went in as, or the `verified_type` the preview allowlist trusts
    would be a lie about the bytes on disk. So the container is preserved, not normalised.

    Nothing is resized. The resolution of a certificate photographed for legibility is part of the
    evidence, and a control that quietly degrades records is a control people start working around.

    **Fail-closed.** If the pixels cannot be decoded, the metadata cannot be proved gone, and storing
    the original anyway would record a stripped file that still carries its geotag. The refusal names
    what to do about it. This does refuse an image that is merely unusual to Pillow but readable by a
    browser; the honest cost of that is one re-save from the office's desktop, and it is cheaper than
    the disclosure it prevents.
    """
    container = METADATA_BEARING_TYPES.get(verified_type)
    if container is None:
        # PDF, DOCX, XLSX, CSV, TXT. A PDF can carry XMP, but these uploads are documents whose bytes
        # *are* the record, and re-writing a PDF to clear a metadata packet would re-render the
        # artefact that an inspection is meant to read. Recorded as a limit in SECURITY.md rather than
        # quietly claimed as covered.
        return upload
    from io import BytesIO
    from django.core.files.base import ContentFile
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        image = Image.open(upload)
        image.verify()                      # `verify()` consumes the object, so re-open after it
        upload.seek(0)
        image = ImageOps.exif_transpose(Image.open(upload))
        if container == "JPEG":
            image = image.convert("RGB")    # Pillow cannot write JPEG for palette or alpha modes
        output = BytesIO()
        image.save(output, format=container)  # no exif=/pnginfo= argument: nothing is carried over
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        upload.seek(0)
        raise ValidationError(
            "This image could not be re-encoded, so its embedded location and camera details cannot "
            "be removed. Open it and save a copy, then upload that — photos are never stored with "
            "their metadata in this system.") from exc
    payload = output.getvalue()
    if len(payload) > DOCUMENT_MAX_BYTES:
        raise ValidationError("Re-encoding the image did not keep it under 25 MiB.")
    stripped = ContentFile(payload, name=upload.name)
    upload.seek(0)
    return stripped


def malware_scan(upload):
    import socket
    from django.conf import settings
    upload.seek(0)
    if settings.MALWARE_SCAN_MODE == "basic":
        infected=b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in upload.read()
        upload.seek(0)
        return not infected,"EICAR test signature" if infected else "basic validation"
    if not settings.CLAMAV_HOST: raise ValidationError("Malware scanner is unavailable.")
    with socket.create_connection((settings.CLAMAV_HOST,settings.CLAMAV_PORT),timeout=30) as sock:
        sock.sendall(b"zINSTREAM\0")
        while chunk:=upload.read(65536): sock.sendall(len(chunk).to_bytes(4,"big")+chunk)
        sock.sendall((0).to_bytes(4,"big")); result=sock.recv(4096).decode("utf-8","replace")
    upload.seek(0)
    return result.endswith("OK\0"),result.rstrip("\0")

@transaction.atomic
def store_person_document(*, organization, person, document_type, upload, actor, expires_on=None, supersedes=None):
    from .models import AuditEvent, PersonDocument
    verified_type=validate_document_upload(upload)
    # Captured before the re-encode, because `strip_document_metadata` hands back a fresh file object
    # that never went through a browser. This column is the uploader's *claim*, kept for display and
    # never used for a decision — `verified_type` above is the one the preview reads.
    declared_type=getattr(upload,"content_type","")[:100]
    stripped=strip_document_metadata(upload,verified_type)
    clean,scan_detail=malware_scan(stripped)
    digest=hashlib.sha256()
    for chunk in stripped.chunks(): digest.update(chunk)
    stripped.seek(0)
    retain_until=None
    if document_type.retention_days is not None: retain_until=timezone.localdate()+timedelta(days=document_type.retention_days)
    document=PersonDocument.objects.create(organization=organization,person=person,document_type=document_type,file=stripped,original_name=stripped.name,content_type=declared_type,verified_type=verified_type[:100],size=stripped.size,sha256=digest.hexdigest(),scan_status=PersonDocument.ScanStatus.CLEAN if clean else PersonDocument.ScanStatus.REJECTED,expires_on=expires_on,retain_until=retain_until,uploaded_by=actor,supersedes=supersedes)
    AuditEvent.objects.create(organization=organization,actor=actor,action="document.uploaded",target_type="person_document",target_id=str(document.pk),metadata={"type":document_type.code,"size":stripped.size,"sha256":document.sha256,"scan":scan_detail,"revision":document.revision_number,"supersedes":str(supersedes.pk) if supersedes else None,"metadata_stripped":verified_type in METADATA_BEARING_TYPES})
    if not clean: document.file.delete(save=False); raise ValidationError("The upload failed malware scanning.")
    return document


# ── In-page document preview (roadmap §6, DD §Documents) ──────────────────────────

# Derived from the two tables above rather than written out again: a magic list kept separately from
# the signatures that produced `verified_type` is a second opinion that can disagree, and the
# disagreement stays invisible until something is served wrongly. `.csv` and `.txt` land as an empty
# tuple, which reads below as "nothing to check" — for the reason recorded at VERIFIED_DOCUMENT_TYPES,
# their inertness comes from the response (text/plain plus nosniff), not from the bytes.
PREVIEW_MAGIC = {VERIFIED_DOCUMENT_TYPES[ext]: ALLOWED_DOCUMENT_SIGNATURES[ext]
                 for ext in (".pdf", ".png", ".jpg", ".jpeg", ".csv", ".txt")}

PREVIEW_EXTENSIONS = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg",
                      "text/plain": ".txt"}


def preview_filename(document):
    """A response-header name built from the digest, containing nothing the uploader wrote.

    `original_name` is user-supplied and Content-Disposition is a header; the storage key was already
    built from a dot-free stem for the same reason. The digest prefix keeps a preview and a download
    of the same bytes recognisably the same object.
    """
    extension = PREVIEW_EXTENSIONS.get(document.verified_type, ".bin")
    return f"record-{(document.sha256 or '')[:12]}{extension}"


def presigned_preview_url(document):
    """A short-lived signed fetch for the stored object, or ``None`` when storage cannot sign one.

    A URL is returned only where the backend really applies a signature. django-storages builds one
    through boto3's GetObject presigner, but with `custom_domain` set and no CloudFront signer its
    `url()` returns a **bare, unsigned, public** link — handing that out would turn a private record
    into an unauthenticated read for anyone who found it, in place of a stream that consults the
    access ladder on every request. That configuration therefore falls back to streaming.

    The two `Response*` parameters are the reason this function is worth having at all: they override
    the object's own stored metadata at fetch time, so the browser is told the content type *this
    application verified* rather than whatever the uploader's browser declared. Names are boto3's
    member names, which serialize to `response-content-type` / `response-content-disposition` and are
    honoured by both S3 and Spaces on a presigned GET.
    """
    storage = getattr(document.file, "storage", None)
    if storage is None or not hasattr(storage, "url"):
        return None
    if not getattr(storage, "querystring_auth", False):
        return None
    if getattr(storage, "custom_domain", None) and not getattr(storage, "cloudfront_signer", None):
        return None
    return storage.url(
        document.file.name,
        parameters={"ResponseContentType": document.verified_type,
                    "ResponseContentDisposition": f'inline; filename="{preview_filename(document)}"'},
        expire=PREVIEW_URL_SECONDS,
    )


def preview_source(document):
    """How to show one record inside the page, or why it cannot be shown.

    Returns ``(url, handle, content_type, kind)`` with exactly one of url/handle set. Raises
    `PreviewUnavailable` for anything that has to be downloaded instead, which the caller turns into a
    message rather than an error page — "no in-page view for a .docx" is not a failure.

    On local disk the head bytes are re-read here, so a file replaced in storage after upload is
    refused rather than served. On object storage that re-read is not free, and the presigned URL
    carries a different protection instead: the served content type is *overridden by us*, so mutated
    or lying object metadata cannot make the browser execute anything. The asymmetry is deliberate and
    recorded in feature-status.md rather than left as a surprise.
    """
    kind = PREVIEW_INLINE_TYPES.get(document.verified_type or "")
    if kind is None:
        raise PreviewUnavailable("only PDF, PNG, JPEG and plain text are shown in the page; "
                                 "this record has to be downloaded.")
    url = presigned_preview_url(document)
    if url is not None:
        return url, None, document.verified_type, kind
    handle = document.file.open("rb")
    signatures = PREVIEW_MAGIC.get(document.verified_type) or ()
    if signatures:
        head = handle.read(16)
        handle.seek(0)
        if not any(head.startswith(signature) for signature in signatures):
            handle.close()
            raise PreviewUnavailable("the stored bytes no longer match the type this record claims.")
    return None, handle, document.verified_type, kind


def _provider_secret(name):
    from django.conf import settings
    value=getattr(settings,name,"")
    if not value: raise RuntimeError(f"{name} is not configured")
    return value

def deliver_notification(notification):
    """Deliver one queued email/SMS. In-app notices become immediately visible."""
    import boto3
    import requests
    from django.conf import settings
    from .models import Notification, Organization
    if notification.status not in (Notification.Status.QUEUED,Notification.Status.FAILED): return notification
    # NTF-4. The consent and suppression check runs before the attempt counter and before any provider
    # call, so a refusal never spends a retry and never appears as a delivery failure. A notice that was
    # never sent to a carrier must not look like one the carrier rejected — the first is a policy
    # statement about this company's records, the second is a fact about a network.
    refusal = send_block_reason(notification)
    if refusal is not None:
        notification.status = Notification.Status.BLOCKED
        notification.last_error = refusal[:500]
        notification.save(update_fields=["status", "last_error"])
        AuditEvent.objects.create(organization=notification.organization, actor=None, action="message.blocked",
            target_type="notification", target_id=str(notification.pk),
            metadata={"channel": notification.channel, "event_type": notification.event_type,
                      "mandatory": notification.mandatory, "reason": refusal,
                      "destination": mask_destination(message_recipient_destination(notification),
                                                     notification.channel)})
        return notification
    notification.attempts += 1
    try:
        if notification.channel == Notification.Channel.IN_APP:
            pass
        elif notification.channel == Notification.Channel.EMAIL:
            recipient=notification.destination or (notification.recipient.email if notification.recipient else "")
            if not recipient: raise RuntimeError("Recipient has no email address")
            sender=notification.organization.email_from or settings.DEFAULT_FROM_EMAIL
            provider=notification.organization.email_provider
            if provider==Organization.EmailProvider.MAILJET:
                response=requests.post("https://api.mailjet.com/v3.1/send",auth=(_provider_secret("MAILJET_API_KEY"),_provider_secret("MAILJET_SECRET_KEY")),json={"Messages":[{"From":{"Email":sender,"Name":notification.organization.display_name},"To":[{"Email":recipient}],"Subject":notification.subject,"TextPart":notification.body,"HTMLPart":branded_email_html(notification)}]},timeout=15); response.raise_for_status()
            elif provider==Organization.EmailProvider.POSTMARK:
                response=requests.post("https://api.postmarkapp.com/email",headers={"X-Postmark-Server-Token":_provider_secret("POSTMARK_SERVER_TOKEN")},json={"From":sender,"To":recipient,"Subject":notification.subject,"TextBody":notification.body,"HtmlBody":branded_email_html(notification)},timeout=15); response.raise_for_status()
            elif provider==Organization.EmailProvider.SES:
                boto3.client("ses",region_name=settings.AWS_REGION).send_email(Source=sender,Destination={"ToAddresses":[recipient]},Message={"Subject":{"Data":notification.subject},"Body":{"Text":{"Data":notification.body},"Html":{"Data":branded_email_html(notification)}}})
        elif notification.channel == Notification.Channel.SMS:
            person=notification.organization.people.filter(user=notification.recipient).first() if notification.recipient else None
            destination=notification.destination or getattr(person,"mobile_phone","")
            if not destination: raise RuntimeError("Recipient has no mobile phone")
            if notification.organization.sms_provider==Organization.SmsProvider.SNS:
                boto3.client("sns",region_name=settings.AWS_REGION).publish(PhoneNumber=destination,Message=notification.body)
            else:
                sid=_provider_secret("TWILIO_ACCOUNT_SID"); token=_provider_secret("TWILIO_AUTH_TOKEN")
                response=requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",auth=(sid,token),data={"To":destination,"From":notification.organization.sms_from or _provider_secret("TWILIO_FROM_NUMBER"),"Body":notification.body},timeout=15); response.raise_for_status()
        notification.status=Notification.Status.SENT; notification.sent_at=timezone.now(); notification.last_error=""
    except Exception as exc:
        notification.status=Notification.Status.FAILED; notification.last_error=str(exc)[:500]
    notification.save(update_fields=["attempts","status","sent_at","last_error"])
    return notification

TRAINING_WARNING_DAYS = 60

# ── NTF-1: per-audience channel selection (roadmap §5) ─────────────────────────────
#
# A notice family reaches several kinds of person at once: the officer it is about, the dispatcher who
# can act on it, the payroll approver whose number it changes. DD asks for delivery channels to be
# configurable per audience; until this, every recipient got the same in-app + email pair and the SMS
# adapter NTF-4 built was never selected by anything. These three functions are the whole selection
# path, and `queue_notice` is the only caller that has to know they exist.

def recipient_roles(organization, user_ids):
    """One query for `{user_id: role}` across a recipient set.

    Built per notice rather than per recipient: the audience ladder reads exactly one role, and a
    loop that asked the database for each of forty dispatch recipients would turn a queue pass into
    forty queries. A user id with no membership here is not a member of this company — the value
    falls to `WORKER`, which is the audience with the fewest privileges, so an unexpected recipient
    gets the least-intrusive delivery rather than the most.
    """
    return dict(organization.memberships.filter(active=True, user_id__in=list(user_ids))
                .values_list("user_id", "role"))


def notification_audience(role, is_subject):
    """Which audience one recipient is, for one notice.

    Being the person the notice is about wins over the role, and that ordering is the design rather
    than a detail. The site supervisor whose own registration is about to lapse, the HR manager who
    is also the officer on the post, the dispatcher responding to a swap on their own name: each is
    being *told about themselves* first and acting as a manager second. A rule that texts
    "compliance" about credential lapses must not text that person in the voice of somebody chasing
    a colleague, and a rule that stays quiet about "officers" must not keep quiet about them.
    """
    if is_subject:
        return ChannelAudience.SUBJECT.value
    for audience, roles in AUDIENCE_ROLES.items():
        if role in roles:
            return audience
    return ChannelAudience.WORKER.value


def rule_channels(rules, event_type, audience, default):
    """Resolve one recipient's channels from rows already in hand.

    Takes the rule rows rather than the organization because a notice addresses many recipients and
    the nightly reminder pass queues one notice per credential: reading the table per recipient would
    turn a forty-person notice into forty reads for a table that holds a handful of rows, and the
    reminder pass would then out-query the deliveries it is deciding. It is also the only place the
    precedence rule lives, so nothing can disagree about which voice wins.
    """
    exact = family = None
    for rule in rules:
        if rule.audience != audience or not rule.active:
            continue
        if rule.event_type and rule.event_type == event_type:
            exact = rule
            break
        if not rule.event_type and rule.matches(event_type):
            family = rule
    rule = exact or family
    if rule is None:
        # No rule is not an empty rule: the caller's channels stand, which is what keeps a company
        # that never opened the page delivering exactly what it delivered before audiences existed.
        return list(default)
    chosen = [channel for channel in (rule.channels or []) if channel in dict(Notification.Channel.choices)]
    if Notification.Channel.IN_APP not in chosen:
        chosen.insert(0, Notification.Channel.IN_APP)
    return chosen


def audience_reach(organization, audience, channel):
    """How many people in one audience could actually receive this channel today.

    The settings page would otherwise be configuring in the dark: "text officers about credential
    reminders" means nothing to an operator until they see that nine of forty-one have given consent
    and two have texted STOP. Answered from the same NTF-4 ledger the send path consults, so the
    number on the screen and the decision in `send_block_reason` cannot tell different stories.

    Counted in two queries over the ledger rather than one per officer, because a page that renders
    forty officers must not run a hundred and twenty. The iteration is ascending so the newest answer
    per destination wins, which is exactly `current_consent`'s rule read in bulk — and the send path
    still re-decides per message, because this number is a forecast for a human, not a permission.
    """
    if channel != Notification.Channel.SMS:
        return None
    granted = {}
    for consent in organization.message_consents.filter(channel=MessageConsent.Channel.SMS).order_by(
            "decided_at", "created_at"):
        key = normalize_destination(consent.destination)
        if key:
            granted[key] = consent.state == MessageConsent.State.GRANTED
    blocked = {key for key in (normalize_destination(row.destination) for row in
             organization.suppressions.filter(channel=MessageConsent.Channel.SMS, cleared_at__isnull=True)) if key}
    people = list(organization.people.exclude(status=Person.Status.INACTIVE)
                  .filter(user__isnull=False).exclude(mobile_phone="").only("mobile_phone", "user_id"))
    role_by_user = recipient_roles(organization, [person.user_id for person in people])
    return sum(1 for person in people
               if audience_reaches_role(audience, role_by_user.get(person.user_id))
               and granted.get(normalize_destination(person.mobile_phone)) is True
               and normalize_destination(person.mobile_phone) not in blocked)


def audience_reaches_role(audience, role):
    """Whether a role falls in one audience when nobody is the subject — the settings-page twin.

    Separate from `notification_audience` because that one needs to know who the notice is *about*,
    and a reach estimate has no notice. `SUBJECT` is answered with "any officer-ish recipient", which
    is the honest approximation and is labelled as one on the page.
    """
    if audience == ChannelAudience.SUBJECT.value:
        return role == Membership.Role.OFFICER
    if audience == ChannelAudience.WORKER.value:
        return role not in {role for roles in AUDIENCE_ROLES.values() for role in roles}
    return role in AUDIENCE_ROLES.get(audience, ())


def queue_notice(*, organization, recipients, event_type, subject, body, dedup_key, channels=(Notification.Channel.IN_APP, Notification.Channel.EMAIL), mandatory=None, subject_user_ids=()):
    """Queue one durable notification per recipient and channel.

    Deduplication is what keeps a re-published schedule or a daily reminder pass from
    multiplying: the key is part of the stored row's unique constraint.

    ``mandatory`` is inferred from the event family when a caller does not state it, so the
    required/optional categorisation DD asks for exists at every notice rather than only at the
    handful that remember to declare it — and so a notice queued by future code cannot accidentally be
    born uncategorised. An explicit value always wins.

    ``subject_user_ids`` names the people this notice is *about*. It is optional because most call
    sites already address a flat set of colleagues; the ones that tell an officer something about
    themselves pass it, and that is what lets NTF-1 route their copy differently from the manager's.
    ``channels`` stays the caller's answer for any recipient no rule speaks for, so a company with no
    rules delivers exactly what it delivered before this function knew about audiences.
    """
    if mandatory is None:
        mandatory = event_is_mandatory(event_type)
    recipients = set(recipients)
    subjects = set(subject_user_ids or ()) & recipients
    roles = recipient_roles(organization, recipients - subjects)
    # One read of the rule table for the whole notice, not one per recipient: the nightly reminder pass
    # queues per credential, and a handful of rules re-queried once per officer per rung would cost
    # more than the deliveries it is deciding.
    rules = list(organization.channel_rules.all())
    created = 0
    for user_id in recipients:
        audience = notification_audience(roles.get(user_id), is_subject=user_id in subjects)
        for channel in rule_channels(rules, event_type, audience, channels):
            _, was_created = Notification.objects.get_or_create(
                organization=organization, recipient_id=user_id, channel=channel,
                deduplication_key=f"{dedup_key}:{channel}",
                defaults={"event_type": event_type, "subject": subject, "body": body,
                          "mandatory": mandatory},
            )
            created += int(was_created)
    return created


def shift_participants(shift):
    """User IDs to tell about a post: the assigned officer plus the managing roles."""
    recipients = set()
    if shift.officer_id and shift.officer.user_id:
        recipients.add(shift.officer.user_id)
    return recipients


def open_post_candidates(shift):
    """Sign-in IDs of active people who could lawfully take this unfilled post.

    Announcing coverage to the whole roster invites requests the company cannot accept; the
    same eligibility rule that gates a dispatcher's assignment decides who hears about it.
    """
    candidates = []
    requirements = post_requirements(shift)
    for person in shift.organization.people.exclude(status=Person.Status.INACTIVE).select_related("user"):
        if not person.user_id:
            continue
        allowed, _ = shift_eligibility(shift, officer=person, requirements=requirements)
        if allowed:
            candidates.append(person.user_id)
    return candidates, len(candidates)


def swap_candidates(shift, excluding=None):
    """Who could stand this post right now, and why the rest could not — in five queries.

    Deputy and When I Work both restrict the candidate list at offer time ("An employee can **only**
    swap with a teammate who is qualified for the shift and does not have a conflicting shift at the
    same time"); TrackTik re-checks at approval. Doing only one of the two is the failure: filter at
    approval only and an officer spends a colleague's attention on an offer that cannot be approved,
    filter at offer only and a licence that lapses overnight walks straight past the gate. This
    function is the first half; ``shift_eligibility`` at approval stays the second.

    It is written batched rather than as ``shift_eligibility`` per person because the offer screen
    lists the whole roster: at a 100-guard firm the per-person version is ~300 queries, and these
    five answer the same question.
    """
    organization = shift.organization
    people = list(organization.people.exclude(status=Person.Status.INACTIVE).select_related("user")
                  .order_by("first_name", "last_name"))
    if excluding is not None:
        people = [item for item in people if item.pk != excluding.pk]
    gating = [(credential_type, origin) for credential_type, origin in post_requirements(shift)
              if credential_type.blocks_scheduling]
    gating_ids = {credential_type.id for credential_type, _ in gating}
    today = timezone.localdate()
    holds = defaultdict(set)
    if gating_ids:
        for row in Credential.objects.filter(credential_type_id__in=gating_ids).values(
                "person_id", "credential_type_id", "status", "expires_on"):
            state = row["status"]
            if state == Credential.Status.ACTIVE and row["expires_on"] and row["expires_on"] < today:
                state = "expired"
            if state not in INVALID_CREDENTIAL_STATES:
                holds[row["person_id"]].add(row["credential_type_id"])
    busy = set(organization.shifts.exclude(status=Shift.Status.CANCELLED).filter(
        starts_at__lt=shift.ends_at, ends_at__gt=shift.starts_at).values_list("officer_id", flat=True)) - {None}
    on_leave = set(TimeOffRequest.objects.filter(status=TimeOffRequest.Status.APPROVED,
        starts_at__lt=shift.ends_at, ends_at__gt=shift.starts_at).values_list("person_id", flat=True))
    eligible, blocked = [], []
    for person in people:
        reasons = []
        if not person.user_id:
            reasons.append("has no sign-in, so they would never see the offer")
        if person.pk in busy:
            reasons.append("already stands a post across that time")
        if person.pk in on_leave:
            reasons.append("is on approved leave then")
        for credential_type, origin in gating:
            if credential_type.id not in holds[person.pk]:
                reasons.append(f"is missing a valid {credential_type.name}"
                               + ("" if origin == "post" else f" (required by the {origin})"))
        if reasons:
            blocked.append({"person": person, "reason": "; ".join(reasons)})
        else:
            eligible.append(person)
    return eligible, blocked


def assignment_impact(organization, moves):
    """Hours per officer per affected workweek, before and after a set of reassignments.

    Connecteam states the approval-screen invariant: "Before you decide, you can see how the request
    affects each employee's hours and shifts, including **hours before and after** … If approving
    would push an employee over their weekly hours or shift limit, **it is flagged in red. For a
    swap, both employees keep their hours**." That last clause is the point of showing this at all —
    in a two-way exchange the numbers should not move, and if they have, the manager is looking at a
    handoff dressed up as a swap. Weeks are attributed from the configured ``workweek_start`` so this
    agrees with the timecard about which week an hour belongs to.
    """
    if not moves:
        return []
    # Resolved the way the payroll side resolves it — get_or_create rather than a bare get — so a
    # firm that has never opened the time policy still shows the manager the hours a move costs.
    # An empty panel here would be a silent gap in the one number the approval decision turns on.
    policy,_=TimePolicy.objects.get_or_create(organization=organization,defaults={"timezone":organization.timezone})
    threshold = policy.overtime_after_hours

    def week_of(moment):
        day = timezone.localtime(moment).date()
        return day - timedelta(days=(day.weekday() - policy.workweek_start) % 7)

    def minutes(shift_row):
        return int((shift_row.ends_at - shift_row.starts_at).total_seconds() // 60)

    moved = {shift_row.pk: shift_row for shift_row, _from, _to in moves}
    affected = {week_of(shift_row.starts_at) for shift_row in moved.values()}
    people = {}
    before, after = defaultdict(int), defaultdict(int)
    for shift_row, source, target in moves:
        people[source.pk] = source
        people[target.pk] = target
    for person in people.values():
        for own in person.shifts.exclude(status=Shift.Status.CANCELLED):
            week = week_of(own.starts_at)
            if week in affected:
                before[(person.pk, week)] += minutes(own)
                after[(person.pk, week)] += minutes(own)
    for shift_row, source, target in moves:
        week, cost = week_of(shift_row.starts_at), minutes(shift_row)
        after[(source.pk, week)] -= cost
        after[(target.pk, week)] += cost
    rows = []
    for person in people.values():
        for week in sorted(affected):
            stood = Decimal(before[(person.pk, week)]) / Decimal(60)
            becomes = Decimal(after[(person.pk, week)]) / Decimal(60)
            rows.append({"person": person, "week_start": week,
                "before": stood.quantize(Decimal("0.01")), "after": becomes.quantize(Decimal("0.01")),
                "threshold": threshold, "over_before": stood > threshold, "over_after": becomes > threshold})
    return rows


def expire_stale_moves(organization=None, now=None):
    """Close open offers and exchanges whose post has already started.

    When I Work expires a request once "the specific day regarding the request has passed", and
    Deputy states the officer's rule flatly: "Until the approval notification is received, **the
    team member originally scheduled to work should assume they're working the shift.**" Both say
    the same thing from opposite ends — an unanswered move must not sit open past the moment it was
    worth anything, because the officer who was waiting to be released has now simply not turned up,
    and the post has no owner on it.
    """
    now = now or timezone.now()
    closed = 0
    swaps = ShiftSwap.objects.filter(status__in=SWAP_OPEN_STATUSES).select_related("shift", "requester", "replacement")
    exchanges = ShiftExchange.objects.filter(status__in=EXCHANGE_OPEN_STATUSES).select_related(
        "initiator_shift", "partner_shift", "initiator", "partner")
    if organization is not None:
        swaps = swaps.filter(organization=organization)
        exchanges = exchanges.filter(organization=organization)
    for swap in swaps:
        if swap.shift.starts_at > now:
            continue
        swap.status = ShiftSwap.Status.EXPIRED
        swap.decided_at = now
        swap.review_note = "The post started before this was answered."
        swap.save(update_fields=["status", "decided_at", "review_note"])
        queue_notice(organization=organization,
            recipients={swap.requester.user_id, swap.replacement.user_id} - {None},
            event_type="shift.swap_expired", subject="A swap offer expired",
            body=f"{swap.shift.site} on {swap.shift.starts_at:%a %b %d, %H:%M} started before this was "
                 "answered. The post stays with the officer scheduled on it.",
            dedup_key=f"shift-swap-expired:{swap.pk}")
        closed += 1
    for exchange in exchanges:
        earliest = min([row.starts_at for row in (exchange.initiator_shift, exchange.partner_shift) if row] or [None])
        if earliest is None or earliest > now:
            continue
        exchange.status = ShiftExchange.Status.EXPIRED
        exchange.decided_at = now
        exchange.review_note = "A post in this exchange started before it was answered."
        exchange.save(update_fields=["status", "decided_at", "review_note"])
        queue_notice(organization=organization,
            recipients={exchange.initiator.user_id, exchange.partner.user_id} - {None},
            event_type="shift.exchange_expired", subject="A shift trade expired",
            body=f"The trade between {exchange.initiator.full_name} and {exchange.partner.full_name} was not "
                 "answered before the post started. Both officers keep their current assignments.",
            dedup_key=f"shift-exchange-expired:{exchange.pk}")
        closed += 1
    return closed


def fill_active_series(organization=None):
    """Keep opted-in series filled, and tell a human about any date it could not place.

    An unattended run must not swallow a blocked date — the whole value of the preview screen is
    that a refused date is *visible*. So this computes the same plan, creates only the allowed rows,
    and reports each blocked date to the dispatchers whose granted authority covers the post. That
    is the scheduling equivalent of TrackTik's rule for a template conflict: "the shift will become
    vacant, and a yellow warning triangle will appear" — the hole is shown, never dropped.
    """
    from .models import Shift
    today = timezone.localdate()
    series = ShiftTemplate.objects.filter(active=True).exclude(auto_generate_days=None).select_related(
        "site__client", "officer", "organization")
    if organization is not None:
        series = series.filter(organization=organization)
    created_total = reported = 0
    for template in series:
        window = template.next_window(today, horizon=min(template.auto_generate_days, SERIES_MAX_DAYS))
        if window is None:
            continue
        status = Shift.Status.PUBLISHED if template.auto_publish else Shift.Status.DRAFT
        plan = apply_recurring_plan(template, window[0], window[1], status)
        created_total += plan["count"]
        for row in plan["rows"]:
            if row["created"]:
                continue
            probe = Shift(organization=template.organization, site_id=template.site_id,
                          officer=template.officer, starts_at=row["starts_at"], ends_at=row["ends_at"])
            queue_notice(organization=template.organization,
                recipients=dispatch_recipients_for_shift(probe, template.officer),
                event_type="shift.series_blocked",
                subject=f"A recurring post could not be placed — {template.site}",
                body=f"{template.name}: {timezone.localtime(row['starts_at']):%a %b %d, %H:%M} was not "
                     "generated — " + " ".join(row["reasons"]) + ". Fix the cause and the next run places it.",
                dedup_key=f"series-blocked:{template.pk}:{row['starts_at']:%Y%m%d}")
            reported += 1
    return created_total, reported


# A post that ended more than this long ago with no punch at all is a gap somebody has to act on,
# not a late synchronisation. The grace is deliberately generous: an offline device has up to twelve
# hours to sync (see `record_punch`), and noticing a missing clock-out before that window closes
# would queue a warning for every guard who worked without signal.
PUNCH_MISSING_GRACE_MINUTES = 12 * 60
PUNCH_MISSING_LOOKBACK_DAYS = 7


def queue_missing_punch_reports(now=None, days=None, organizations=None):
    """Notice a closed post that has no clock-in or no clock-out at all.

    The whole of NTF-3's argument in one pass: an unreviewed timecard is not a compliance item until
    a queue built from `Punch` rows can see it, and a guard who never punched produces no row to be
    noticed. The same defect the benchmark found for credentials — "the queue only noticed what
    someone had already filed a row about" — repeats here, and this is what pays for the missing
    entry rather than reporting a clean week.

    One notice per post per gap, deduplicated on the post, so the daily pass re-arms only when the
    gap actually changes (an officer who adds the clock-out stops being chased).
    """
    now = now or timezone.now()
    window = days or PUNCH_MISSING_LOOKBACK_DAYS
    created_total = 0
    scope = organizations or Organization.objects.all()
    for organization in scope:
        no_in = Punch.objects.filter(shift=OuterRef("pk"), kind=Punch.Kind.IN)
        no_out = Punch.objects.filter(shift=OuterRef("pk"), kind=Punch.Kind.OUT)
        posts = organization.shifts.filter(
            status=Shift.Status.PUBLISHED, officer__isnull=False,
            ends_at__lte=now - timedelta(minutes=PUNCH_MISSING_GRACE_MINUTES),
            starts_at__gte=now - timedelta(days=window),
        ).select_related("officer", "site", "site__client").annotate(has_in=Exists(no_in), has_out=Exists(no_out))
        for shift in posts:
            gaps = [label for present, label in ((shift.has_in, "clock-in"), (shift.has_out, "clock-out")) if not present]
            if not gaps:
                continue
            officer = shift.officer
            recipients = dispatch_recipients_for_shift(shift, officer)
            if officer.user_id:
                # The officer is the audience here, unlike an exception notice: they are the only
                # person who can say whether they worked the post.
                recipients.add(officer.user_id)
            created_total += queue_notice(organization=organization, recipients=recipients,
                event_type="punch.missing",
                subject=f"Missing {', '.join(gaps)} for {officer.full_name}",
                body=("{name} was stood at {post} from {start} to {end} with no {gaps} recorded."
                      " Correct the timecard or confirm the hours were not worked.").format(
                        name=officer.full_name,
                        post=f"{shift.site} · {shift.post_name}" if shift.post_name else str(shift.site),
                        start=shift.starts_at.strftime("%d %b %Y %H:%M"), end=shift.ends_at.strftime("%d %b %Y %H:%M"),
                        gaps=", ".join(gaps)),
                dedup_key=f"punch.missing:{shift.pk}:{'-'.join(gaps)}")
    return created_total


def approved_role_domains(organization):
    """The company's normalized allow-list, or an empty set meaning no restriction.

    Lowercased and stripped at read time rather than at write time, because the field is a JSON list
    somebody can also fill from a shell or a fixture: a stored "GuardCo.com " would otherwise fail to
    match a real address that is correct.
    """
    return {str(item).strip().lower().lstrip("@") for item in (organization.approved_role_domains or [])
            if str(item).strip()}


def role_domain_gate(organization, email, role):
    """Whether this identity may hold this role here. Returns ``(allowed, reason)``.

    AUTH-3's check, and it belongs at the one place a role is ever granted — accepting an invitation —
    rather than at sign-in. Restricting login would lock a person out of the account that already
    exists; the DD sentence is about who may hold an organizational role, and an officer standing a
    post is explicitly allowed to arrive on a personal identity. So Officer is never gated, and
    everything above it is: HR, scheduler, payroll approver, supervisor, auditor, administrator, owner.

    The address tested is the one the invitation was sent to and the account now presents as verified,
    which is why this runs after possession of the mailbox has been proved rather than trusting a
    username match — an email that merely *resembles* a member's address is the thing this rule exists
    to refuse.
    """
    allowed = approved_role_domains(organization)
    if not allowed or role == Membership.Role.OFFICER:
        return True, ""
    domain = str(email or "").rsplit("@")[-1].strip().lower() if "@" in str(email or "") else ""
    if domain and domain in allowed:
        return True, ""
    return False, (f"{organization.display_name} restricts the {Membership.Role(role).label} role to "
                   f"{', '.join(sorted(allowed))}. {email or 'This address'} is outside that list, so "
                   "the invitation cannot grant it. An officer position is still available on a "
                   "personal address.")


def role_recipients(organization, roles):
    """Active sign-ins holding one of these roles in this company.

    Every notice family needs this question and each copy of it drifts: the first three were
    written before anyone thought to ask whether the payroll approver hears about a reopened
    period, and the answer was different per call site.
    """
    return set(organization.memberships.filter(active=True, role__in=list(roles)).values_list("user_id", flat=True))


def payroll_recipients(organization):
    """Who owns a payroll figure once it is locked or unlocked.

    A reopen is the one that has to reach people who already saw a locked number: the notice is
    what tells the approver that the figure they approved is no longer the one being billed.
    """
    return role_recipients(organization, [Membership.Role.OWNER, Membership.Role.ADMIN,
                                          Membership.Role.HR, Membership.Role.PAYROLL])


def compliance_recipients(organization, person=None, managers=()):
    """Owner/admin/HR, the person concerned, and the field managers whose authority covers them.

    Peers route an expiring credential to a "manager of record". The compliance roles always
    hear about it, and a branch or contract supervisor hears about it when their granted
    scopes reach this person — the supervisor who dispatches a guard should not learn that the
    guard's registration lapsed from a report the following month.
    """
    recipients = set(organization.memberships.filter(
        active=True, role__in=[Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR]
    ).values_list("user_id", flat=True))
    if person is not None:
        if person.user_id:
            recipients.add(person.user_id)
        recipients.update(managers)
    return recipients


def reminder_level(credential_type, days):
    """The most urgent lead time ``days`` has already entered, or None.

    The ladder counts down, so the level to fire is the *smallest* configured lead the date
    has reached. Picking the largest would queue the 90-day notice and treat every later
    level as already sent.
    """
    return min((level for level in credential_type.reminder_levels if days <= level), default=None)


def queue_compliance_reminders(today=None):
    """Queue one notice per reminder level, per state, per recipient.

    Three cases have to reach a person: a credential inside its ladder, a credential in an
    invalid state, and an obligation with no record at all. The last one is invisible to a
    queue built from ``Credential`` rows — a guard who never had a certificate filed
    produces no row — so the requirement side is walked as well, once per month per
    person/requirement pair rather than once per day. A renewed credential carries a new
    expiry date in its dedup key, so the ladder re-arms instead of staying silent.
    """
    today = today or timezone.localdate()
    created = 0

    def queue(*, organization, event_type, subject, body, dedup_key, recipients, subject_user_ids=()):
        nonlocal created
        created += queue_notice(organization=organization, recipients=recipients, event_type=event_type,
                                subject=subject, body=body, dedup_key=dedup_key,
                                subject_user_ids=subject_user_ids)

    for organization in Organization.objects.all():
        types = list(organization.credential_types.filter(active=True))
        if not types:
            continue
        roster = {person.pk: person for person in organization.people.exclude(status=Person.Status.INACTIVE)}
        # One index for the whole organization: which field managers are responsible for which
        # person. Built here rather than per row so the pass costs it once.
        coverage = manager_recipients_by_person(organization)
        held = {}
        for credential in organization.credentials.select_related("credential_type"):
            held.setdefault(credential.person_id, {})[credential.credential_type_id] = credential

        for credential in organization.credentials.select_related("credential_type", "person", "person__user"):
            person = roster.get(credential.person_id)
            if person is None:
                continue
            state = credential.effective_status
            days = (credential.expires_on - today).days if credential.expires_on else None
            recipients = compliance_recipients(organization, person, managers=coverage.get(person.pk, ()))
            if not recipients:
                continue
            if state in INVALID_CREDENTIAL_STATES:
                queue(organization=organization, event_type="credential.reminder",
                      subject=f"Credential attention: {credential.credential_type.name}",
                      body=f"{person.full_name}'s {credential.credential_type.name} is {state}.",
                      dedup_key=f"credential:{credential.pk}:state:{state}:{today.strftime('%Y-%m')}",
                      recipients=recipients, subject_user_ids={person.user_id} if person.user_id else ())
                continue
            if days is None:
                if credential.credential_type.evidence_required:
                    queue(organization=organization, event_type="credential.reminder",
                          subject=f"No expiry recorded: {credential.credential_type.name}",
                          body=f"{person.full_name}'s {credential.credential_type.name} is {state} with no renewal date, so nothing can warn before it lapses.",
                          dedup_key=f"credential:{credential.pk}:no-expiry:{today.strftime('%Y-%m')}",
                          recipients=recipients, subject_user_ids={person.user_id} if person.user_id else ())
                continue
            if days < 0:
                continue
            level = reminder_level(credential.credential_type, days)
            if level is None:
                continue
            queue(organization=organization, event_type="credential.reminder",
                  subject=f"Credential renewal: {credential.credential_type.name}",
                  body=f"{person.full_name}'s {credential.credential_type.name} expires on {credential.expires_on} ({days} days).",
                  dedup_key=f"credential:{credential.pk}:renew:{credential.expires_on}:{level}",
                  recipients=recipients, subject_user_ids={person.user_id} if person.user_id else ())
            # NTF-2. The rung that fires is itself the proof that every rung above it went unanswered:
            # the ladder only reaches here while the expiry date is still the old one, so a renewal
            # recorded at 60 days stops the 90-day notice from ever becoming a missed one. The
            # escalation is a separate notice to a separate audience rather than the same notice sent
            # wider — the recipient has to see that this is new information about somebody's inaction,
            # and the base audience must not get a second copy of what they were just told.
            requirement = credential.credential_type
            if requirement.escalates_at(days):
                escalation = role_recipients(organization, requirement.escalation_roles) - set(recipients)
                if escalation:
                    missed = ", ".join(str(value) for value in requirement.missed_rungs(days) if value != level)
                    detail = ("{}'s {} expires on {} ({} days) and no renewal has been recorded after {}.".format(
                        person.full_name, requirement.name, credential.expires_on, days,
                        f"notices at {missed} days" if missed else "the earlier reminder")
                        + " Nobody in the reminder audience has closed it, so it reaches you.")
                    queue(organization=organization, event_type="credential.escalated",
                          subject=f"Escalated — still no renewal: {requirement.name}",
                          body=detail,
                          dedup_key=f"credential:{credential.pk}:escalated:{credential.expires_on}:{level}",
                          recipients=escalation)

        for person in roster.values():
            for credential_type, credential, state in credential_obligations(person, types, held.get(person.pk, {})):
                if credential is not None or state != Credential.Status.MISSING:
                    continue
                queue(organization=organization, event_type="credential.missing",
                      subject=f"Missing credential: {credential_type.name}",
                      body=f"{person.full_name} has no {credential_type.name} record, which their role requires. They cannot be assigned a post that blocks on it.",
                      dedup_key=f"credential-missing:{person.pk}:{credential_type.pk}:{today.strftime('%Y-%m')}",
                      recipients=compliance_recipients(organization, person, managers=coverage.get(person.pk, ())),
                      subject_user_ids={person.user_id} if person.user_id else ())

        for record in organization.training_records.select_related("person", "person__user"):
            if not record.expires_on or record.person_id not in roster:
                continue
            days = (record.expires_on - today).days
            if 0 <= days <= TRAINING_WARNING_DAYS:
                queue(organization=organization, event_type="training.reminder",
                      subject=f"Training renewal: {record.course_name}",
                      body=f"{record.person.full_name}'s {record.course_name} training expires on {record.expires_on} ({days} days).",
                      dedup_key=f"training:{record.pk}:renew:{record.expires_on}:{today.strftime('%Y-%m')}",
                      recipients=compliance_recipients(organization, record.person, managers=coverage.get(record.person_id, ())),
                      subject_user_ids={record.person.user_id} if record.person.user_id else ())
    return created


def outstanding_acknowledgments(document, people=None):
    """Active personnel who have not signed a company record issued to every worker.

    ``PersonDocument.acknowledged_at`` proves that *someone* signed; it is not a completion
    signal for a workforce record, and using it hid every worker after the first signature.
    """
    if people is None:
        people = document.organization.people.exclude(status=Person.Status.INACTIVE)
    signed = set(DocumentAcknowledgment.objects.filter(document=document).values_list("person_id", flat=True))
    return list(people.exclude(pk__in=signed).order_by("last_name", "first_name"))


def document_lineage(document):
    """Every version in this record's chain, oldest first.

    The chain is linear by construction — the upload form only offers a row nobody has already
    replaced as the thing being revised — so walking up to the root and down from it reaches every
    version, including ones filed *after* the row the report was opened from.
    """
    node = document
    while node.supersedes_id:
        node = PersonDocument.objects.select_related("supersedes").get(pk=node.supersedes_id)
    chain = []
    cursor = node
    while cursor is not None:
        chain.append(cursor)
        cursor = cursor.revisions.order_by("created_at", "id").first()
    return chain


def signature_lineage(document, people=None):
    """Who has signed *any* version of this record, and who has never signed any of them.

    The roster on one revision answers "who has not signed this text", which is the right question
    for a reminder and the wrong one for an auditor. A worker who signed the 2024 handbook and never
    opened the 2026 one sits on the current roster as outstanding forever, and a chain where nobody
    signed anything looks identical to one where everyone signed some version somewhere. The sentence
    an inspection actually asks for is REC-3's: these N people have never acknowledged any version —
    and separately, these N have acknowledged a version that has since been replaced, which is
    evidence of what was agreed at the time and not evidence that anyone read the current text.
    """
    chain = document_lineage(document)
    roster = list((people if people is not None
                   else document.organization.people.exclude(status=Person.Status.INACTIVE))
                  .order_by("last_name", "first_name"))
    signatures = defaultdict(list)
    for item in DocumentAcknowledgment.objects.filter(document_id__in=[row.pk for row in chain]).order_by("acknowledged_at"):
        signatures[item.person_id].append(item)
    ever = set(signatures)
    on_current = {item.person_id for item in DocumentAcknowledgment.objects.filter(document=document)}
    per_version = [{"document": row, "count": sum(1 for person_id, items in signatures.items()
                                                   if any(item.document_id == row.pk for item in items))}
                   for row in chain]
    return {
        "chain": chain,
        "roster": roster,
        "signatures": signatures,
        "per_version": per_version,
        "never_signed": [person for person in roster if person.pk not in ever],
        "signed_current": [person for person in roster if person.pk in on_current],
        "superseded_only": [person for person in roster if person.pk in ever and person.pk not in on_current],
    }


def queue_acknowledgment_reminders(document, actor):
    """Ask every outstanding worker to acknowledge a company record.

    Peers offer "send N reminders" on the acknowledgement screen; the durable queue is what
    carries it here, so a reminder about a worker who cannot sign in yet is raised to the
    compliance roles instead of being dropped.
    """
    if document.document_type.audience != DocumentType.Audience.WORKFORCE:
        raise ValidationError("Only company records issued to every worker have an acknowledgment queue.")
    if document.scan_status != PersonDocument.ScanStatus.CLEAN:
        raise ValidationError("A record that has not scanned clean cannot be issued for acknowledgment.")
    if document.archived_at:
        # Chasing signatures on a record the company has filed away under a retention decision is
        # how a reminder list starts lying about what is in force.
        raise ValidationError("This record has been archived under a retention decision, so nobody is being asked to acknowledge it.")
    outstanding = outstanding_acknowledgments(document)
    management = list(document.organization.memberships.filter(
        active=True, role__in=[Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR]
    ).values_list("user_id", flat=True))
    queued = 0
    stamp = timezone.now().strftime("%Y%m%d")
    for person in outstanding:
        recipients = [person.user_id] if person.user_id else management
        for user_id in recipients:
            _, was_created = Notification.objects.get_or_create(
                organization=document.organization, recipient_id=user_id, channel=Notification.Channel.IN_APP,
                deduplication_key=f"document-remind:{document.pk}:{person.pk}:{stamp}",
                defaults={"event_type": "document.acknowledgment_requested",
                          "subject": f"Acknowledgment required: {document.document_type.name}",
                          "body": (f"{person.full_name} has not acknowledged {document.document_type.name}. Review it from My documents."
                                   if person.user_id else
                                   f"{person.full_name} has no sign-in yet, so {document.document_type.name} cannot be acknowledged. Provision access from their profile.")},
            )
            queued += int(was_created)
    AuditEvent.objects.create(
        organization=document.organization, actor=actor, action="document.reminders_queued",
        target_type="person_document", target_id=str(document.pk), metadata={"outstanding": len(outstanding)},
    )
    return queued

IMPORT_COLUMNS={
 "branches":{"name"}, "people":{"first_name","last_name","email"}, "clients":{"name"},
 "sites":{"client","name","address"}, "credentials":{"person_email","type_code","status"},
 "training":{"person_email","course_name","completed_on"}, "shifts":{"client","site","starts_at","ends_at"},
}

def preview_csv_import(*,organization,entity,upload,actor):
    from .models import ImportBatch
    if upload.size>50*1024*1024: raise ValidationError("CSV imports must be 50 MiB or smaller.")
    raw=upload.read(); digest=hashlib.sha256(raw).hexdigest()
    try: text=raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc: raise ValidationError("CSV must be UTF-8 encoded.") from exc
    reader=csv.DictReader(StringIO(text)); fields=set(reader.fieldnames or []); required=IMPORT_COLUMNS.get(entity)
    if required is None: raise ValidationError("Unsupported import type.")
    missing=required-fields
    if missing: raise ValidationError("Missing columns: "+", ".join(sorted(missing)))
    rows=[]; errors=[]
    for number,row in enumerate(reader,start=2):
        cleaned={str(k).strip():str(v or "").strip() for k,v in row.items()}
        row_errors=[]
        for field in required:
            if not cleaned.get(field): row_errors.append(f"{field} is required")
        if entity in ("training",) and cleaned.get("completed_on"):
            try: date.fromisoformat(cleaned["completed_on"])
            except ValueError: row_errors.append("completed_on must use YYYY-MM-DD")
        if entity=="shifts":
            for field in ("starts_at","ends_at"):
                try: datetime.fromisoformat(cleaned[field].replace("Z","+00:00"))
                except ValueError: row_errors.append(f"{field} must be ISO-8601")
        rows.append(cleaned)
        if row_errors: errors.append({"row":number,"errors":row_errors})
    if len(rows)>10000: raise ValidationError("A single import may contain at most 10,000 rows.")
    batch,created=ImportBatch.objects.get_or_create(organization=organization,entity=entity,source_hash=digest,defaults={"source_name":upload.name,"rows":rows,"errors":errors,"status":ImportBatch.Status.INVALID if errors else ImportBatch.Status.PREVIEW,"created_by":actor})
    return batch,created

def _bool(value): return str(value).casefold() in ("1","true","yes","y")

@transaction.atomic
def apply_csv_import(batch,actor):
    from .models import AuditEvent, Branch, Client, Credential, CredentialType, ImportBatch, Person, Shift, Site, TrainingRecord
    if batch.status==ImportBatch.Status.APPLIED: return 0
    if batch.errors: raise ValidationError("Correct validation errors before applying this import.")
    org=batch.organization; count=0
    for row in batch.rows:
        if batch.entity==ImportBatch.Entity.BRANCHES:
            Branch.objects.update_or_create(organization=org,name=row["name"],defaults={"city":row.get("city","")})
        elif batch.entity==ImportBatch.Entity.PEOPLE:
            branch=org.branches.filter(name=row.get("branch","")).first()
            defaults={"first_name":row["first_name"],"last_name":row["last_name"],"branch":branch,"status":row.get("status") or Person.Status.ONBOARDING,"is_unarmed_officer":_bool(row.get("is_unarmed_officer")),"is_commissioned_officer":_bool(row.get("is_commissioned_officer")),"is_ppo":_bool(row.get("is_ppo")),"is_private_investigator":_bool(row.get("is_private_investigator")),"is_shareholder":_bool(row.get("is_shareholder"))}
            for field in ("employee_id","job_title","mobile_phone","address_line1","address_line2","city","state","postal_code","emergency_contact_name","emergency_contact_phone","hourly_rate","hire_date","termination_date","date_of_birth"):
                if row.get(field):defaults[field]=row[field]
            Person.objects.update_or_create(organization=org,email=row["email"],defaults=defaults)
        elif batch.entity==ImportBatch.Entity.CLIENTS:
            Client.objects.update_or_create(organization=org,name=row["name"],defaults={"contact_name":row.get("contact_name", ""),"contact_email":row.get("contact_email","")})
        elif batch.entity==ImportBatch.Entity.SITES:
            client=org.clients.get(name=row["client"]); branch=org.branches.filter(name=row.get("branch","")).first()
            Site.objects.update_or_create(organization=org,client=client,name=row["name"],defaults={"branch":branch,"address":row["address"],"latitude":row.get("latitude") or None,"longitude":row.get("longitude") or None,"geofence_radius_meters":int(row.get("geofence_radius_meters") or 200)})
        elif batch.entity==ImportBatch.Entity.CREDENTIALS:
            person=org.people.get(email=row["person_email"]); kind=org.credential_types.get(code=row["type_code"])
            Credential.objects.update_or_create(organization=org,person=person,credential_type=kind,number=row.get("number",""),defaults={"status":row["status"],"issued_on":row.get("issued_on") or None,"expires_on":row.get("expires_on") or None})
        elif batch.entity==ImportBatch.Entity.TRAINING:
            person=org.people.get(email=row["person_email"])
            TrainingRecord.objects.update_or_create(organization=org,person=person,course_name=row["course_name"],completed_on=row["completed_on"],defaults={"provider":row.get("provider",""),"expires_on":row.get("expires_on") or None,"certificate_number":row.get("certificate_number","")})
        elif batch.entity==ImportBatch.Entity.SHIFTS:
            site=org.sites.get(client__name=row["client"],name=row["site"]); officer=org.people.filter(email=row.get("officer_email","")).first()
            Shift.objects.update_or_create(organization=org,site=site,starts_at=datetime.fromisoformat(row["starts_at"].replace("Z","+00:00")),ends_at=datetime.fromisoformat(row["ends_at"].replace("Z","+00:00")),defaults={"officer":officer,"post_name":row.get("post_name", ""),"post_orders":row.get("post_orders",""),"status":row.get("status") or Shift.Status.DRAFT})
        count+=1
    batch.status=ImportBatch.Status.APPLIED; batch.applied_at=timezone.now(); batch.save(update_fields=["status","applied_at"])
    AuditEvent.objects.create(organization=org,actor=actor,action="import.applied",target_type="import_batch",target_id=str(batch.pk),metadata={"entity":batch.entity,"rows":count,"hash":batch.source_hash})
    # NTF-3: an import runs to completion in the background of whoever started it, and a batch that
    # applied 900 of 900 rows is a different fact from one that applied 3. The count is the notice.
    queue_notice(organization=org,recipients={actor.pk},event_type="import.completed",
        subject=f"Import finished: {count} {batch.entity} row(s)",
        body=f"{batch.source_name} applied {count} row(s) of {len(batch.rows)} parsed. The rows are live in the registers they touched.",
        dedup_key=f"import.completed:{batch.pk}")
    return count

@transaction.atomic
def execute_disposition(request,actor):
    from .models import AuditEvent, DispositionRequest
    document=request.document
    if request.status!=DispositionRequest.Status.PENDING: raise ValidationError("Disposition request is not pending.")
    if document.legal_hold: raise ValidationError("Document is under legal hold.")
    if request.requested_by_id == actor.pk:
        raise ValidationError("A second owner or administrator must authorize the disposition.")
    now=timezone.now()
    if request.action==DispositionRequest.Action.ARCHIVE:
        document.archived_at=now;document.save(update_fields=["archived_at"])
    else:
        storage_name=document.file.name
        if storage_name: document.file.storage.delete(storage_name)
        document.file="";document.deleted_at=now;document.save(update_fields=["file","deleted_at"])
    request.status=DispositionRequest.Status.EXECUTED;request.approved_by=actor;request.executed_at=now;request.save(update_fields=["status","approved_by","executed_at"])
    AuditEvent.objects.create(organization=request.organization,actor=actor,action=f"document.{request.action}d",target_type="person_document_tombstone",target_id=str(document.pk),metadata={"request":str(request.pk),"reason":request.reason,"original_name":document.original_name,"sha256":document.sha256,"person":str(document.person_id),"document_type":str(document.document_type_id)})
    # NTF-3: the requester asked, a second approver decided, and today the person who filed the
    # request learns the outcome by reopening the retention screen.
    queue_notice(organization=request.organization,recipients={request.requested_by_id}-{actor.pk},
        event_type=f"retention.disposition_{request.status.lower()}",
        subject=f"Retention request executed: {document.original_name}",
        body=(f"{actor} approved the {request.action.lower()} of “{document.original_name}” "
              f"({document.document_type.name}). Reason: {request.reason}"),
        dedup_key=f"retention.disposition:{request.pk}")
    return document

@transaction.atomic
def restore_disposition(request_row,actor,reason):
    """Bring an archived record back into the active file. A deleted file cannot be.

    DD asks for "soft deletion/recovery", and the two halves are not symmetric. Archive keeps every
    byte and only removes the record from the live file, so reversing it is a decision reversal:
    reason, actor, audit event, no drama. Permanent deletion deletes the stored object and clears the
    file pointer, so there is nothing to recover — the row survives as a tombstone with its SHA-256
    precisely so the absence can be evidenced. Pretending otherwise would be worse than the
    limitation, so this refuses and says which case it is.
    """
    from .models import AuditEvent, DispositionRequest
    document = request_row.document
    if request_row.status != DispositionRequest.Status.EXECUTED:
        raise ValidationError("Only an executed disposition can be reversed.")
    if len(reason.strip()) < 10:
        raise ValidationError("Give a restoration reason of at least 10 characters.")
    if document.deleted_at or not document.archived_at:
        raise ValidationError("This record's file was deleted rather than archived, so there is "
                              "nothing to restore — the tombstone and the audit chain remain.")
    document.archived_at = None
    document.save(update_fields=["archived_at"])
    request_row.status = DispositionRequest.Status.RESTORED
    request_row.restored_by = actor
    request_row.restored_at = timezone.now()
    request_row.restore_reason = reason.strip()
    request_row.save(update_fields=["status","restored_by","restored_at","restore_reason"])
    AuditEvent.objects.create(organization=request_row.organization,actor=actor,action="document.restored",
        target_type="person_document",target_id=str(document.pk),
        metadata={"request":str(request_row.pk),"reason":request_row.restore_reason,
                  "original_name":document.original_name,"sha256":document.sha256,
                  "file_present":bool(document.file.name)})
    return document


# A file export of one person's whole history can be enormous: uploads are capped at 25 MiB each,
# so a long-tenured officer with a few hundred scans could turn one click into a gigabyte response.
# Past this size the archive still carries every row and the manifest still names each withheld
# record, so what is missing is stated rather than silently dropped.
EXPORT_FILE_LIMIT_BYTES = 250 * 1024 * 1024

# Which of the person's records this reader may have at all is decided by the same ladder as every
# other read path, so "I exported the file" can never mean more than "I could open the file".
def personnel_file_bundle(organization, person, role, subject_person_id=None, sensitive_fields=False):
    """One person's file as one reader is allowed to see it: dossier, manifest, and the records.

    The bundle is returned before it is zipped so the authority decision is testable on its own — a
    ZIP that quietly contains a sealed investigation file fails no assertion about bytes.

    Two absences are deliberate and stated in the README rather than left to be discovered. Records
    the reader's rung does not open are **not listed at all**, not even as withheld: naming a sealed
    file to the person it is about, or to a role that may not see it, would itself be the disclosure
    the ladder exists to prevent. And wage figures are not here: the payroll export is the one
    source for what was worked and billed, and reproducing them inside a personnel file would give
    the same number two definitions on the same document.
    """
    visible = record_visibility_filter(role, subject_person_id)
    documents = list(person.documents.filter(deleted_at__isnull=True).filter(visible)
                     .select_related("document_type", "uploaded_by").order_by("document_type__name", "created_at"))
    credentials = list(person.credentials.select_related("credential_type").order_by("credential_type__name"))
    training = list(person.training_records.order_by("-completed_on"))
    leave = list(person.time_off_requests.order_by("-starts_at"))
    posts = list(person.shifts.select_related("site__client", "site__branch").order_by("-starts_at"))
    punches = list(person.punches.select_related("shift__site").order_by("-occurred_at"))
    availability = list(person.availability_rules.order_by("weekday", "starts_at"))
    acknowledgments = list(DocumentAcknowledgment.objects.filter(
        organization=organization, person=person).select_related("document__document_type").order_by("-acknowledged_at"))
    corrections = list(PunchAdjustment.objects.filter(
        organization=organization, punch__person=person).select_related("punch").order_by("-created_at"))
    values = {item.definition_id: item for item in person.custom_values.select_related("definition")}
    custom = []
    for definition in organization.custom_field_definitions.filter(active=True).order_by("name"):
        if definition.sensitive and not sensitive_fields:
            continue
        stored = values.get(definition.pk)
        custom.append({"field": definition.name, "key": definition.key, "kind": definition.kind,
                       "value": (stored.value if stored else None)})
    dossier = {
        "person": person_snapshot(person) | {
            "full_name": person.full_name, "branch": (person.branch.name if person.branch_id else None),
            "personnel_categories": list(person.personnel_categories)},
        "custom_fields": custom,
        "credentials": [{"requirement": item.credential_type.name, "authority": item.credential_type.authority_reference,
                         "number": item.number, "status": item.effective_status, "issued_on": item.issued_on,
                         "expires_on": item.expires_on} for item in credentials],
        "training": [{"course": item.course_name, "provider": item.provider, "completed_on": item.completed_on,
                      "expires_on": item.expires_on, "hours": str(item.hours)} for item in training],
        "time_off": [{"starts_at": item.starts_at, "ends_at": item.ends_at, "reason": item.reason,
                      "status": item.status} for item in leave],
        "posts": [{"starts_at": item.starts_at, "ends_at": item.ends_at, "site": str(item.site),
                   "client": str(item.site.client), "post": item.post_name, "status": item.status,
                   "shift_status": item.get_status_display()} for item in posts],
        "time_events": [{"kind": item.get_kind_display(), "occurred_at": item.occurred_at,
                         "review": item.review_status, "exception": item.exception_reason,
                         "offline": item.offline, "source": item.source} for item in punches],
        "availability": [{"weekday": item.get_weekday_display(), "starts_at": str(item.starts_at),
                          "ends_at": str(item.ends_at)} for item in availability],
        "acknowledgments": [{"record": item.document.document_type.name, "file": item.document.original_name,
                             "revision": item.document.revision_number, "signature": item.signature_name,
                             "acknowledged_at": item.acknowledged_at} for item in acknowledgments],
        "corrections": [{"punch": str(item.punch_id), "proposed_at": item.proposed_at, "reason": item.reason,
                         "status": item.status, "note": item.review_note} for item in corrections],
    }
    manifest, files, notes = [], [], []
    budget = EXPORT_FILE_LIMIT_BYTES
    for position, document in enumerate(documents, start=1):
        kind = document.document_type
        archived = bool(document.archived_at)
        include = bool(document.file.name) and not archived
        manifest.append({"row": position, "record_type": kind.name, "audience": kind.get_audience_display(),
                         "sensitivity": kind.get_sensitivity_display(), "file": document.original_name,
                         "revision": document.revision_number, "uploaded": document.created_at,
                         "uploaded_by": (document.uploaded_by.email if document.uploaded_by_id else None),
                         "expires_on": document.expires_on, "retain_until": document.retain_until,
                         "legal_hold": document.legal_hold, "archived": archived,
                         "scan_status": document.scan_status, "sha256": document.sha256,
                         "size_bytes": document.size, "included_in_archive": include})
        if not include:
            if archived:
                notes.append(f"{kind.name} ({document.original_name}) is archived under a retention decision, "
                             "so it is listed but not included.")
            elif not document.file.name:
                notes.append(f"{kind.name} ({document.original_name}) has no stored file to include.")
            continue
        if document.size > budget:
            notes.append(f"{kind.name} ({document.original_name}) is past the export size limit and was not included.")
            manifest[-1]["included_in_archive"] = False
            continue
        budget -= document.size
        # Names inside the archive are built, not trusted: an upload's original name is user text and
        # must not be able to escape the folder it lands in. Dots are dropped from the stem entirely
        # rather than filtered, because ".." is the case that matters and a partial dot filter was
        # the first attempt's mistake.
        stem = "".join(character for character in Path(document.original_name).stem
                       if character.isalnum() or character in " -_")[:70] or "file"
        suffix = Path(document.original_name).suffix
        suffix = suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,7}", suffix or "") else ""
        files.append((f"records/{position:02d}-rev{document.revision_number}-{stem}{suffix}", document))
    basis = ("whole-company view" if subject_person_id is None else
             ("your own personnel file" if person.pk == subject_person_id else "this person's file"))
    readme = "\n".join([
        f"Personnel file export — {person.full_name} ({organization.display_name})",
        f"Generated {timezone.now().isoformat()} as a {basis}.",
        "",
        "Included: profile and custom fields, credentials and their authority, training, leave and",
        "availability, every post and time event on record, the acknowledgments this person signed,",
        "and time-correction requests with their outcomes.",
        "",
        "Not included, by design:",
        "  * Records your role is not authorised to open are not listed here at all — not even as",
        "    withheld. Naming a sealed investigation file to someone who may not read it would be",
        "    the disclosure this control exists to prevent, so its absence is silent and complete.",
        "  * Wage and hour totals are not reproduced here. The payroll export is the single source",
        "    for what was worked, rounded, paid and billed, and a second copy of those numbers in a",
        "    personnel file would be a second definition of them.",
        "  * Archived records appear in the manifest marked as archived, and their files are not",
        "    packaged: they are out of the live file under a retention decision, which recovery",
        "    (Retention review) can bring back.",
        "",
        (f"Withheld from this archive:\n  " + "\n  ".join(notes) if notes else
         "Nothing was withheld from this archive."),
    ]) + "\n"
    return {"person": person, "dossier": dossier, "manifest": manifest, "files": files,
            "notes": notes, "readme": readme}


def personnel_file_zip(bundle):
    """Write a bundle as an archive. Every entry is a name this code built, never user text."""
    import zipfile
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", bundle["readme"])
        archive.writestr("dossier.json", json.dumps(bundle["dossier"], indent=2, sort_keys=True, default=str))
        csv_output = StringIO()
        columns = list(bundle["manifest"][0].keys()) if bundle["manifest"] else ["row"]
        writer = csv.DictWriter(csv_output, fieldnames=columns)
        writer.writeheader()
        for row in bundle["manifest"]:
            writer.writerow({key: safe_cell(value) for key, value in row.items()})
        archive.writestr("records-manifest.csv", csv_output.getvalue())
        for arcname, document in bundle["files"]:
            try:
                with document.file.open("rb") as handle:
                    archive.writestr(arcname, handle.read())
            except (FileNotFoundError, NotImplementedError, ValueError):
                # A row whose storage object is gone is a fact about the bucket, not a reason to
                # fail the whole export; the manifest already records what was meant to be there.
                continue
    output.seek(0)
    return output.getvalue()


def process_brand_image(upload):
    from io import BytesIO
    from django.core.files.base import ContentFile
    from PIL import Image,ImageOps,UnidentifiedImageError
    if upload.size>5*1024*1024: raise ValidationError("Brand images must be 5 MiB or smaller.")
    clean,_=malware_scan(upload)
    if not clean: raise ValidationError("The brand image failed malware scanning.")
    try:
        image=Image.open(upload);image.verify();upload.seek(0);image=Image.open(upload);image=ImageOps.exif_transpose(image).convert("RGBA")
    except (UnidentifiedImageError,Image.DecompressionBombError,Image.DecompressionBombWarning) as exc: raise ValidationError("Upload a safe PNG, JPEG, or WebP image.") from exc
    image.thumbnail((2048,2048));output=BytesIO();image.save(output,format="PNG",optimize=True)
    return ContentFile(output.getvalue(),name=f"brand-{uuid.uuid4().hex}.png")

def brand_snapshot(organization):
    return {"display_name":organization.display_name,"primary_color":organization.primary_color,"accent_color":organization.accent_color,"dark_primary_color":organization.dark_primary_color,"dark_accent_color":organization.dark_accent_color,"dark_mode_enabled":organization.dark_mode_enabled,"logo":organization.logo.name if organization.logo else "","support_email":organization.support_email,"support_phone":organization.support_phone,"timezone":organization.timezone}

@transaction.atomic
def create_brand_version(organization,actor):
    from django.db.models import Max
    from .models import BrandVersion
    version=(organization.brand_versions.aggregate(value=Max("version"))["value"] or 0)+1
    return BrandVersion.objects.create(organization=organization,version=version,snapshot=brand_snapshot(organization),created_by=actor)

def branded_email_html(notification):
    import html
    org=notification.organization
    # Mail clients fetch images from outside the app session, so a locally stored logo has no
    # URL a recipient can open; only a public object-storage URL is usable in email.
    stored_url=org.logo.url if org.logo else ""
    logo=f'<img src="{html.escape(stored_url)}" alt="{html.escape(org.display_name)} logo" style="max-height:56px">' if stored_url.startswith(("http://","https://")) else ""
    return f'<div style="font-family:Arial,sans-serif;color:#172033"><div style="border-bottom:4px solid {org.accent_color};padding:16px 0">{logo}<strong>{html.escape(org.display_name)}</strong></div><h1 style="color:{org.primary_color};font-size:22px">{html.escape(notification.subject)}</h1><p>{html.escape(notification.body).replace(chr(10),"<br>")}</p></div>'

def person_snapshot(person):
    fields=("employee_id","first_name","last_name","email","mobile_phone","job_title","hire_date","termination_date","date_of_birth","address_line1","address_line2","city","state","postal_code","emergency_contact_name","emergency_contact_phone","hourly_rate","status","is_unarmed_officer","is_commissioned_officer","is_ppo","is_private_investigator","is_shareholder","branch_id")
    return {field:str(getattr(person,field)) if getattr(person,field) is not None else None for field in fields}

def record_person_history(person,before,actor):
    from .models import AuditEvent,PersonHistory
    after=person_snapshot(person);changes={key:{"before":before.get(key),"after":value} for key,value in after.items() if before.get(key)!=value}
    if changes:
        PersonHistory.objects.create(person=person,organization=person.organization,changed_by=actor,changes=changes,snapshot=after)
        AuditEvent.objects.create(organization=person.organization,actor=actor,action="person.updated",target_type="person",target_id=str(person.pk),metadata={"fields":sorted(changes)})
    return changes

def coerce_custom_value(definition,raw):
    from .models import CustomFieldDefinition
    if raw in (None,""):
        if definition.required: raise ValidationError(f"{definition.name} is required.")
        return None
    if definition.kind==CustomFieldDefinition.Kind.NUMBER:
        try:return str(Decimal(raw))
        except Exception as exc:raise ValidationError(f"{definition.name} must be a number.") from exc
    if definition.kind==CustomFieldDefinition.Kind.DATE:
        try:return date.fromisoformat(raw).isoformat()
        except ValueError as exc:raise ValidationError(f"{definition.name} must be a date.") from exc
    if definition.kind==CustomFieldDefinition.Kind.BOOLEAN:return str(raw).casefold() in ("1","true","yes","on")
    return str(raw)[:2000]

def audit_seal_heads(organization):
    """``{last_hash: seal}`` for the periods whose rows have left the live table.

    A purged period leaves behind a chain whose first surviving event names a row that is gone. Without
    this map that reads as tampering — and it *is* the one legitimate reason a hash chain would stop
    matching, so the walk has to know which missing heads are accounted for rather than accept any.
    """
    return {row.last_hash: row for row in AuditSeal.objects.filter(
        organization=organization, status=AuditSeal.Status.PURGED)}


def verify_audit_chain(organization):
    """Every event whose stored hash does not follow from the row before it.

    The walk now continues *through* a sealed period: when the oldest live event claims a predecessor
    that matches a purged seal's ``last_hash``, the chain is intact and simply starts further back than
    the table does. Any other claim is still an error, so a forged "seal this gap" row is not enough to
    explain one away — the seal has to name the hash the surviving row actually carries.
    """
    previous = ""
    errors = []
    heads = audit_seal_heads(organization)
    for event in organization.audit_events.order_by("occurred_at", "id"):
        if previous == "" and event.previous_hash and event.previous_hash in heads:
            previous = event.previous_hash
        expected = audit_event_hash(id=event.pk, organization=event.organization_id, actor=event.actor_id,
            action=event.action, target_type=event.target_type, target_id=event.target_id,
            metadata=event.metadata, previous_hash=previous)
        if event.previous_hash != previous or event.event_hash != expected:
            errors.append(str(event.pk))
        previous = event.event_hash
    return errors


# ── Onboarding steps (roadmap §13, ONB-1) ────────────────────────────────────────

ONBOARDING_WAIVER_MIN_LENGTH = 10


def onboarding_items_for(person, items=None):
    """The active steps this person owes, by personnel category."""
    candidates = list(items) if items is not None else list(person.organization.onboarding_items.all())
    return [item for item in candidates if item.active and item.applies_to_person_or_all(person)]


def provision_onboarding_tasks(person, items=None):
    """Issue every applicable step to one person. Idempotent, and returns only what it created.

    The database constraint does the deduplication, not a check-then-create: ``(person, item)`` is
    unique, so the CSV import running beside the worker pass cannot produce the second task row that
    would then need to be completed twice. Callers that add steps later re-run this and get exactly
    the new rows.
    """
    created = []
    for item in onboarding_items_for(person, items):
        task, made = OnboardingTask.objects.get_or_create(
            organization=person.organization, person=person, item=item,
            defaults={"due_on": onboarding_due_on(person, item)})
        if made:
            created.append(task)
    return created


def onboarding_due_on(person, item, today=None):
    """Deadline for a step issued now: hire date plus the step's lead time.

    A person with no hire date recorded gets ``None`` rather than a date derived from today: the
    alternative invents a deadline for somebody whose start date is simply unknown, and an invented
    deadline is what turns a data-entry gap into an officer being chased for being late.
    """
    if not person.hire_date:
        return None
    return person.hire_date + timedelta(days=item.due_within_days)


def onboarding_evidence(organization, task, reader=None):
    """What the personnel file says about a step that names evidence, or None if it is not that kind.

    Three decisions already in the roadmap shape this, and none is invented here:

    * ``record_open_for`` is asked *before* existence, for the same reason ``compliance_duties`` asks
      it: reporting "no claim filed" about a claim that is filed and sealed from the reader turns a
      permission decision into a false compliance statement. A hidden record is therefore reported as
      hidden with ``satisfied`` left ``None`` — not counted, and not counted as missing.
    * The *step* is owed by the person whoever may read the record, so naming the step is not the
      disclosure REC-5 governs; naming the filed record is. That is why the row still shows its name
      and its date, and only its evidence state is withheld.
    * ``archived_at`` is honoured: a record filed and then archived under a retention decision does
      not satisfy a step, and it is the same column the register already reads.
    """
    item = task.item
    if item.kind == OnboardingItem.Kind.TASK:
        return None
    if item.kind == OnboardingItem.Kind.DOCUMENT:
        if not item.document_type_id:
            return {"state": "unmeasured", "detail": "no record type named for this step", "satisfied": False}
        document_type = organization.document_types.filter(pk=item.document_type_id).first()
        if document_type is None:
            return {"state": "unmeasured", "detail": "the record type this step named has been removed", "satisfied": False}
        if reader is not None and not record_open_for(document_type, task.person_id, reader[0], reader[1]):
            return {"state": "hidden", "detail": "this step's record type is not open to your role", "satisfied": None}
        found = organization.person_documents.filter(
            document_type_id=document_type.pk, person_id=task.person_id,
            deleted_at__isnull=True, archived_at__isnull=True).exists()
        return {"state": "on file" if found else "not on file",
                "detail": "" if found else "nothing filed of this type", "satisfied": found}
    if item.kind == OnboardingItem.Kind.CREDENTIAL:
        if not item.credential_type_id:
            return {"state": "unmeasured", "detail": "no requirement named for this step", "satisfied": False}
        credential = organization.credentials.filter(person_id=task.person_id, credential_type_id=item.credential_type_id).first()
        if credential is None:
            return {"state": "no record filed", "detail": "nothing filed against this requirement", "satisfied": False}
        state = credential.effective_status
        if state in INVALID_CREDENTIAL_STATES:
            return {"state": state, "detail": f"{credential.number or 'no number recorded'} — status is {state}", "satisfied": False}
        return {"state": state, "detail": credential.number or "", "satisfied": True}
    return None


def onboarding_board(organization, person, today=None, reader=None, tasks=None):
    """One person's checklist as rows, plus the totals the screen and the reminders agree on.

    The person tab, the officer's own page, the dashboard tile, and the chase pass all read this one
    function. The invariant the tests hold it to is that the counts and the rows come from the same
    walk: a tile that says "3 outstanding" beside a page listing 2 is how a missed onboarding step
    gets explained away as a display bug, and this product's whole argument is that a figure and the
    list behind it are one calculation (RPT-4).
    """
    today = today or timezone.localdate()
    rows = []
    source = tasks if tasks is not None else list(
        person.onboarding_tasks.select_related("item", "item__document_type", "item__credential_type"))
    for task in source:
        evidence = onboarding_evidence(organization, task, reader=reader)
        overdue = bool(task.due_on) and task.status == OnboardingTask.Status.OPEN and task.due_on < today
        block = ""
        if task.status == OnboardingTask.Status.OPEN and evidence and evidence.get("satisfied") is False:
            block = evidence["state"]
        rows.append({
            "task": task, "item": task.item, "status": task.status, "due_on": task.due_on,
            "overdue": overdue, "evidence": evidence, "block": block,
            "no_hire_date": task.due_on is None and task.status == OnboardingTask.Status.OPEN,
        })
    outstanding = [row for row in rows if row["status"] == OnboardingTask.Status.OPEN]
    # Compared by identity, not by count: a step deleted from the checklist after it was issued still
    # has its task row, and `len(items) - len(rows)` would then report a negative "unissued" that the
    # screen rounds to zero while a genuinely new step hides inside the arithmetic.
    applicable = {item.pk for item in onboarding_items_for(person)}
    issued = {row["item"].pk for row in rows}
    return {
        "rows": rows,
        "outstanding": outstanding,
        "overdue": [row for row in outstanding if row["overdue"]],
        "done": [row for row in rows if row["status"] == OnboardingTask.Status.DONE],
        "waived": [row for row in rows if row["status"] == OnboardingTask.Status.WAIVED],
        "total": len(rows),
        "unissued": len(applicable - issued),
    }


def onboarding_progress(organization, scope, today=None):
    """``{person_id: {"open": n, "overdue": n}}`` for the scoped roster, in one aggregate query.

    The dashboard tile, the people list and the checklist settings page all need a count per person,
    and running ``onboarding_board`` for each of them would walk every new hire's whole checklist —
    with the record-visibility lookups it makes, that is a page that gets slower with every step the
    company adds. This is the cheap twin: same two conditions (``status=open``, ``due_on`` in the
    past), one query. ``OnboardingTaskTest`` asserts the counts here equal the rows the board
    produces, because a tile and a page that disagree is how an overdue step gets explained away as
    a display bug.
    """
    today = today or timezone.localdate()
    rows = scope.filter_by_person(organization.onboarding_tasks.filter(
        person__status__in=[Person.Status.ONBOARDING, Person.Status.ACTIVE],
    )).values("person_id").annotate(
        open=Count("id", filter=Q(status=OnboardingTask.Status.OPEN)),
        overdue=Count("id", filter=Q(status=OnboardingTask.Status.OPEN, due_on__isnull=False, due_on__lt=today)),
    )
    return {row["person_id"]: {"open": row["open"], "overdue": row["overdue"]} for row in rows}


def decide_onboarding_task(task, actor, status, note=""):
    """Mark one step done or waived, refusing the ones whose evidence is not on file.

    A step that names a record or a registration is not complete because somebody ticked it — that
    is how "commission on file" ends up marked done on the day the commission lapsed. Waiving *is*
    allowed for any kind, because the office is entitled to decide a step does not apply to this
    person; it just has to say why, in a reason that outlives the argument.
    """
    note = (note or "").strip()
    if status == OnboardingTask.Status.WAIVED and len(note) < ONBOARDING_WAIVER_MIN_LENGTH:
        raise ValidationError(
            f"Waiving a step needs a reason of at least {ONBOARDING_WAIVER_MIN_LENGTH} characters — "
            "a box ticked with no explanation is not a decision anyone can defend later.")
    if status == OnboardingTask.Status.DONE:
        evidence = onboarding_evidence(task.organization, task)
        if evidence and evidence.get("satisfied") is False:
            raise ValidationError(
                f"{task.item.name} cannot be marked complete: {evidence['detail'] or evidence['state']}."
                " File the record first, or waive the step if it does not apply.")
    task.status = status
    task.decided_by = actor
    task.decided_at = timezone.now()
    task.note = note
    task.save(update_fields=["status", "decided_by", "decided_at", "note", "updated_at"])
    return task


def queue_onboarding_assignment(person, tasks):
    """Tell the new hire what they owe, once per person.

    One notice for the plan rather than one per step: five emails on a first login is how a checklist
    becomes noise, and the chase below is what covers a step added later. The dedup key is the
    person, so re-provisioning an existing plan never re-sends.
    """
    if not tasks or not person.user_id:
        return 0
    organization = person.organization
    names = ", ".join(task.item.name for task in tasks[:6])
    due = ", ".join(sorted({str(task.due_on) for task in tasks if task.due_on}))
    return queue_notice(
        organization=organization, recipients={person.user_id}, event_type="onboarding.assigned",
        subject=f"Your first steps at {organization.display_name or organization.legal_name}",
        body=(f"{len(tasks)} step{'' if len(tasks) == 1 else 's'} are owed: {names}"
              + (f". Due {due}." if due else ". No hire date is recorded yet, so no date is set.")),
        dedup_key=f"onboarding-plan:{person.pk}")


def queue_onboarding_reminders(today=None, organizations=None):
    """Chase every open step that is past its date, once per step per date.

    The dedup key names ``due_on``, which is what makes this a ladder rather than a loop: re-running
    the pass every minute must not send the same officer a reminder every minute, and if the plan is
    re-issued with a different date, the new date is a new fact worth one notice. Addressed to the
    step's declared owner — the officer when the step is theirs, the office when it is not — and a
    person with no login is skipped rather than dropped into an inbox that does not exist.

    Only people still on the roster are chased: a step that belonged to somebody now terminated is
    not late, it is moot, and a reminder pass that keeps reporting it teaches people to ignore the
    queue.
    """
    today = today or timezone.localdate()
    created_total = 0
    for organization in (organizations or Organization.objects.all()):
        rows = organization.onboarding_tasks.filter(
            status=OnboardingTask.Status.OPEN, due_on__isnull=False, due_on__lt=today,
            person__status__in=[Person.Status.ONBOARDING, Person.Status.ACTIVE],
        ).select_related("item", "person")
        for task in rows:
            person = task.person
            if task.item.owner == OnboardingItem.Owner.PERSON:
                recipients = {person.user_id} if person.user_id else set()
            else:
                # The office, minus the officer the step is about — they are the subject, not the
                # party who has to file it.
                recipients = set(role_recipients(organization, (
                    Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR))) - {person.user_id}
            if not recipients:
                continue
            days = (today - task.due_on).days
            created_total += queue_notice(
                organization=organization, recipients=recipients, event_type="onboarding.overdue",
                subject=f"{person.full_name}: {task.item.name} is past its date",
                body=(f"{task.item.name} was due {task.due_on:%d %b %Y} ({days} day{'' if days == 1 else 's'} ago) "
                      f"for {person.full_name}. The step is still open."),
                dedup_key=f"onboarding-overdue:{task.pk}:{task.due_on}")
    return created_total


# ── Registry verification (roadmap §8, CMP-3) ────────────────────────────────────

REGISTRY_ADVERSE_RESULTS = ("expired", "not_found", "attention")


def registry_checks_by_credential(organization, credential_ids):
    """The latest check per credential, in one query.

    Only the newest row decides the state — a check from March does not answer "is this valid today"
    — but the older rows stay on the table because the trail is the point of CMP-3, and an inspector
    who asks when we last looked wants the dates, not the conclusion.
    """
    latest = {}
    if not credential_ids:
        return latest
    rows = CredentialRegistryCheck.objects.filter(
        organization=organization, credential_id__in=list(credential_ids)
    ).order_by("credential_id", "-checked_on", "-created_at")
    for row in rows:
        latest.setdefault(row.credential_id, row)
    return latest


def credential_registry_state(credential_type, credential, check=None, today=None):
    """Whether this credential's registry check is current, and whether the last one was adverse.

    The two are different questions and are kept apart on purpose:

    * ``lapsed`` — a check is owed and nobody has done it recently — is **not** compliance attention.
      A registration the registry confirms as valid but that no one has looked up since June is a
      bookkeeping gap; counting it as a failed credential would put paperwork on the same axis as an
      expired licence, and the queue would then tell a dispatcher to stop posting somebody whose
      registration is fine.
    * ``adverse`` — a check was done and it said expired / not found / mismatch — **is** attention,
      because we looked and found the credential is not what the row claims. That is a fact about the
      officer, not about our filing.

    So the rate moves on ``adverse`` and never on ``lapsed``, and both are shown.
    """
    today = today or timezone.localdate()
    window = getattr(credential_type, "registry_check_within_days", None)
    if credential is None or window is None:
        return {"owed": False, "check": check, "lapsed": False, "adverse": False, "state": "not owed"}
    if check is None:
        return {"owed": True, "check": None, "lapsed": True, "adverse": False,
                "state": "never checked", "detail": f"no registry check recorded, and one is owed every {window} days"}
    age = (today - check.checked_on).days
    adverse = check.result in REGISTRY_ADVERSE_RESULTS
    state = "adverse" if adverse else ("overdue" if age > window else "current")
    return {
        "owed": True, "check": check, "lapsed": (not adverse) and age > window, "adverse": adverse,
        "state": state,
        "detail": (f"registry said {check.get_result_display().lower()} on {check.checked_on:%d %b %Y}" if adverse
                   else (f"checked {check.checked_on:%d %b %Y}, {age} days ago (every {window})")),
    }


def record_registry_check(credential, actor, checked_on, result, registry_reference="", note=""):
    """Append one registry lookup, and let a clean one finish the verification the row was waiting for.

    ``Credential.verified_at`` existed since migration 0003 and nothing wrote it — the same dead
    column shape ``archived_at`` had before REC-2, and the reason this function owns the write. A
    valid lookup stamps it and lifts a credential out of ``unverified``/``pending``, which is what
    the status was for: the number has been confirmed by a human against the state's own register.

    What it deliberately does **not** do is demote. An adverse result is recorded and surfaced as
    compliance attention, but moving an ``active`` row to expired/suspended/revoked stays a human
    decision — the registry page and the licence in front of you disagree, and the file should not
    silently pick a side. Only ``unverified``/``pending`` are promoted, never a status the office set
    itself, and never past an expiry date that has already passed.
    """
    check = CredentialRegistryCheck.objects.create(
        organization=credential.organization, credential=credential, checked_on=checked_on,
        result=result, registry_reference=(registry_reference or "").strip()[:120],
        note=(note or "").strip(), checked_by=actor)
    if result == CredentialRegistryCheck.Result.VALID:
        credential.verified_at = timezone.now()
        promote = credential.status in (Credential.Status.UNVERIFIED, Credential.Status.PENDING)
        not_expired = not credential.expires_on or credential.expires_on >= timezone.localdate()
        if promote and not_expired:
            credential.status = Credential.Status.ACTIVE
            credential.save(update_fields=["verified_at", "status", "updated_at"])
        else:
            credential.save(update_fields=["verified_at", "updated_at"])
    return check


# ── Pay categories and designated hours (roadmap §2, PAY-2) ───────────────────────

# The reading each default encodes, and the reason a default is not a rule: the DOL excludes
# training from hours worked only when it is outside normal working hours, voluntary, not directly
# job-related, *and* produces no work (29 CFR 785.27) — a mandatory pre-assignment briefing fails
# three of those four, so it is paid time. A bona fide meal period is unpaid only when the officer
# is completely relieved of duty (785.19); a guard eating at the desk while the phone is live is
# working. Travel between properties during a shift is hours worked (785.38); the drive from home
# to the first post is not (785.35). Holiday and double-time premiums are not wage law at all —
# the FLSA requires no premium pay for holidays or weekends — so their multipliers start at 1.0
# and the firm raises them by contract.
PAY_CATEGORY_DEFAULTS = (
    (PayCategory.Kind.BREAK, "Unpaid meal period", False, Decimal("1.00"), False,
     "Unpaid only where the officer is completely relieved of duty for the whole period; a desk-side meal is paid time."),
    (PayCategory.Kind.HOLIDAY, "Holiday hours", True, Decimal("1.00"), True,
     "No premium is required by the FLSA; raise the multiple to what each contract actually promises."),
    (PayCategory.Kind.TRAINING, "Mandatory training", True, Decimal("1.00"), True,
     "Paid, and part of the week's hours, because the exclusion in 29 CFR 785.27 does not reach required job-related training."),
    (PayCategory.Kind.TRAVEL, "Inter-site travel", True, Decimal("1.00"), True,
     "Travel between job sites during the workday is hours worked; the commute to the first post is not."),
    (PayCategory.Kind.DOUBLE_TIME, "Double time", True, Decimal("2.00"), True,
     "A contract term, not a statutory floor. Kept at 2.00 because a firm that designates these hours at all usually means it."),
    (PayCategory.Kind.DIFFERENTIAL, "Shift differential", True, Decimal("1.00"), True,
     "A premium on the hour, not extra hours: it prices only what sits above straight time, so a "
     "flat amount per hour is entered here as its equivalent multiple and the arithmetic behind the "
     "number goes in the note. No wage law requires one."),
    (PayCategory.Kind.LEAVE, "Approved leave", True, Decimal("1.00"), False,
     "Paid by contract, not by the FLSA — no federal rule requires paid leave at all. These hours do "
     "not count toward the weekly overtime threshold because they are not hours worked: the officer "
     "was absent, and paying an absence does not make a 40-hour week out of 30 worked hours plus 10 "
     "granted ones."),
)


def ensure_pay_categories(organization):
    """Give a company the kinds with their defaults, once. Created lazily rather than by
    migration so an installation that never designates a category is not shipped rows it did
    not ask for, and so a firm that edits a default keeps its edit — nothing here overwrites an
    existing row."""
    created = []
    for kind, name, paid, multiplier, counts, interpretation in PAY_CATEGORY_DEFAULTS:
        row, made = PayCategory.objects.get_or_create(
            organization=organization, kind=kind,
            defaults={"name": name, "paid": paid, "multiplier": multiplier,
                      "counts_toward_overtime": counts, "interpretation": interpretation})
        if made:
            # Version 1 is written at creation, not at first edit. A payroll line stamped
            # ``rule v1`` is the common case, and if only edited rules had history then the one
            # version most designations were priced under would be the version missing.
            revise_pay_category(row)
            created.append(row)
    return created


def revise_pay_category(category, actor=None, previous=None):
    """Append the version a pay category currently holds, and the one it just replaced.

    PAY-2's provenance half. ``PayCategory.save`` bumps ``revision`` whenever a watched value
    moves, on purpose, so that a shell or a data migration cannot edit a multiplier behind the
    settings screen and leave the number still claiming to be version 1. But a counter that climbs
    with nothing written behind it is worse than no counter: the category line on a timecard prints
    "rule v2 at 1.50x", and if ``RuleRevision`` holds no row for v2 that sentence names a version
    nobody can read back — the exact failure POL-1 exists to prevent, reproduced inside the feature
    that cites it. So every door that saves a category calls this.

    ``previous`` is the ``(revision, values)`` snapshot taken *before* the save, in the same shape
    ``record_rule_revision`` takes it, and it is the caller's job because only the caller still has
    it: by the time this runs, the old numbers are gone from the row. A door that did not snapshot
    gets a correct row for the version in front of it and an honest gap where the earlier one was —
    the same limit §4 of the roadmap records for rules that predate the history table. Unresolvable
    rather than invented.
    """
    return record_rule_revision(category, RuleRevision.Kind.PAY_CATEGORY, actor, previous=previous)


def set_shift_designation(shift, category, hours, reason, actor):
    """Record or correct how many of a post's hours are a given kind.

    One line per (post, kind): re-designating holiday hours for a post that already has them
    replaces the figure rather than stacking a second bucket, because a payroll row that summed two
    overlapping claims for the same hour would pay the hour twice. The reason is mandatory and the
    writer is recorded — this is the one place in the payroll path where a human asserts a fact the
    punches cannot show, so it is the place a dispute starts.
    """
    hours = Decimal(str(hours or "0")).quantize(Decimal("0.01"))
    if category.kind == PayCategory.Kind.LEAVE:
        raise ValidationError(
            "Approved leave is not marked on a post — it arrives from a decided time-off request, "
            "and a designation here would pay the same absence twice.")
    if hours <= 0:
        raise ValidationError("Designate a positive number of hours, or remove the line.")
    if not reason or len(reason.strip()) < 5:
        raise ValidationError("Say why these hours are this kind — five characters at least.")
    # The post's own span is the ceiling, measured on the schedule rather than the punches: a
    # designation for more hours than the post could contain is a keying error, and letting it
    # through would silently truncate at payroll time (``payroll_rows`` reports the shortfall it
    # could not place) while showing the payroll clerk a figure nobody entered.
    if shift.starts_at and shift.ends_at:
        span = Decimal((shift.ends_at - shift.starts_at).total_seconds()) / Decimal(3600)
        if hours > span:
            raise ValidationError(
                f"{hours}h does not fit this post — it is scheduled for {span.quantize(Decimal('0.01'))}h. "
                "Split the post or record a hold-over instead.")
    row, made = ShiftHourDesignation.objects.get_or_create(
        shift=shift, category=category,
        defaults={"organization": shift.organization, "hours": hours,
                  "reason": reason.strip()[:255], "recorded_by": actor})
    if not made:
        row.hours = hours
        row.reason = reason.strip()[:255]
        row.recorded_by = actor
        row.save(update_fields=["hours", "reason", "recorded_by"])
    return row, made


# ── Hold-overs and split tours (roadmap §1, SCH-3) ───────────────────────────────

def _punch_time(punch):
    """The instant a punch is paid at: an approved correction wins over the device's claim."""
    approved = next((item for item in punch.adjustments.all() if item.status == "approved"), None)
    return approved.proposed_at if approved else punch.occurred_at


def recorded_tour_end(shift):
    """When the officer actually came off the post, from the clock rather than the schedule.

    ``None`` when the post has no accepted clock-out — which is the case SCH-3 deliberately does not
    invent an answer for: a missing punch is ``punch.missing``'s problem, and guessing an end time
    here would turn an absent record into a present one.
    """
    punches = shift.punches.filter(kind=Punch.Kind.OUT, review_status=Punch.Review.ACCEPTED)\
        .select_related("shift").prefetch_related("adjustments")
    times = [_punch_time(item) for item in punches]
    return max(times) if times else None


def overrun_prompt(shift):
    """What the clock says about this post's end, and whether anybody has said why.

    The vertical's own sequence: relief does not turn up, the officer stays, and the only durable
    record is a clock-out at an hour nobody scheduled. That difference is visible from the punches
    — the *reason* is not, and it is the half an employment dispute asks for ("she was ordered to
    stay"), so this asks the question while the night is still recent.

    Deliberately an observation, never a refusal: the punch stands, the pay stands, and nothing here
    changes a figure. The scheduled end is the earliest hold-over on file when there is one, because
    that row is the record of what the post said before anybody extended it; with no row yet it has
    to be the post's own end, which is why editing ``ends_at`` instead of recording a hold-over
    erases the evidence — the limit is stated on the screen rather than hidden.
    """
    recorded = list(shift.hold_overs.all())
    scheduled = min((row.scheduled_ends_at for row in recorded), default=shift.ends_at)
    actual = recorded_tour_end(shift)
    if not scheduled or not actual or actual <= scheduled:
        return {"hours": 0.0, "scheduled_ends_at": scheduled, "actual_ends_at": actual,
                "recorded": bool(recorded), "text": ""}
    hours = round((actual - scheduled).total_seconds() / 3600, 2)
    if recorded:
        return {"hours": hours, "scheduled_ends_at": scheduled, "actual_ends_at": actual,
                "recorded": True, "text": ""}
    return {"hours": hours, "scheduled_ends_at": scheduled, "actual_ends_at": actual, "recorded": False,
            "text": (f"The clock has this tour ending at {timezone.localtime(actual):%a %H:%M}, "
                     f"{hours}h after the post was due to end at {timezone.localtime(scheduled):%a %H:%M}, "
                     "with no reason recorded. If relief did not arrive, say so here — a late punch-out "
                     "with no hold-over reads as a guard who left on her own, and that is the version "
                     "a claim will be argued against.")}


def describe_hold_over(row):
    """One hold-over as the timecard says it: hours, the end it ran past, the reason, and the relief."""
    overrun = row.overrun_hours
    scheduled = f"{timezone.localtime(row.scheduled_ends_at):%a %H:%M}"
    if overrun is None:
        span = f"past {scheduled} and still on post"
    else:
        span = f"{overrun}h past {scheduled}"
    text = f"Held over {span} — {row.get_reason_display().lower()}"
    if row.relief_id:
        text += f"; relief owed by {row.relief.full_name}"
    if row.note:
        text += f" ({row.note})"
    if row.recorded_by:
        text += f" — recorded by {row.recorded_by}"
    return text


@transaction.atomic
def record_hold_over(shift, reason, actor=None, held_until=None, relief=None, note="", scheduled_ends_at=None):
    """Store why a tour ran past its end. SCH-3's done-criterion, as one write.

    ``scheduled_ends_at`` defaults to the post's current end and is a parameter rather than a
    derivation because the commonest real sequence is the opposite one: the dispatcher extends the
    post first and remembers the reason later, by which time the schedule no longer says what was
    published. Whoever is typing gets to say; nothing here silently rewrites the clock.

    An unexplained ``reason=other`` is refused by the model, and a relief who *is* the officer who
    stayed is refused too — that pairing is how a no-show on the following post would be hidden
    inside the record of the tour it emptied.
    """
    if reason not in dict(HoldOver.Reason.choices):
        raise ValidationError({"reason": "Choose one of the recorded reasons."})
    row = HoldOver(
        shift=shift, organization=shift.organization, reason=reason,
        scheduled_ends_at=scheduled_ends_at or shift.ends_at, held_until=held_until,
        relief=relief, note=(note or "").strip(), recorded_by=actor)
    row.full_clean()
    row.save()
    AuditEvent.objects.create(
        organization=shift.organization, actor=actor, action="shift.held_over",
        target_type="shift", target_id=str(shift.pk),
        metadata={"hold_over": str(row.pk), "reason": reason,
                  "scheduled_ends_at": row.scheduled_ends_at.isoformat(),
                  "held_until": held_until.isoformat() if held_until else None,
                  "relief": str(relief.pk) if relief else None,
                  "overrun_hours": row.overrun_hours})
    return row


@transaction.atomic
def close_stale_hold_overs(reference=None):
    """Fill in the end of a hold-over the clock already knows about.

    A hold-over recorded at 22:15 with no ``held_until`` is an open question, and the dispatcher who
    meant to close it before going home is the same person who forgot. The officer's accepted
    clock-out answers it, so the sweep writes that rather than leaving the overrun as "still on post"
    forever. It never moves a time the office set by hand, and never closes one the clock has not:
    inventing an end for a tour that is genuinely still running would be the same fabrication the
    missing-punch notice exists to surface.
    """
    now = reference or timezone.now()
    closed = 0
    open_rows = HoldOver.objects.filter(held_until__isnull=True).select_related("shift")
    for row in open_rows:
        actual = recorded_tour_end(row.shift)
        if actual and actual > row.scheduled_ends_at and actual <= now:
            row.held_until = actual
            row.save(update_fields=["held_until"])
            closed += 1
    return closed


# ── Shared clock stations and clock PINs (roadmap §3, CLK-2) ─────────────────────
#
# ``docs/discovery-decisions.md`` lists "shared kiosk PIN" as a supported clock-evidence option and
# says registered-device binding is *not* required. A guard shack tablet is the normal case in this
# vertical: officers are not issued company phones, many have not accepted a sign-in invitation yet,
# and the post still has to be opened and closed by clock evidence that names a human.
#
# The two halves are deliberately separate, because they answer different questions:
#
#   the station (``ClockKiosk``, its UUID in ``Punch.device_id``) answers *where*;
#   the PIN (``Person.clock_pin``) answers *who*.
#
# Neither is enough alone, and the failure mode this exists to prevent has a name in the industry —
# buddy punching, one officer clocking another in. So the station can never say who is punching, and
# the PIN can never be presented at a station the clock policy has forbidden.

PIN_MIN_DIGITS = 4
PIN_MAX_DIGITS = 8
PIN_MAX_FAILURES = 5
PIN_LOCKOUT_SECONDS = 15 * 60
# How long a verified PIN keeps the station open to the officer who typed it. Ninety seconds is the
# difference between "type a PIN, then tap Clock in, then tap the patrol point" and "type a PIN twice
# for one tour". It is short enough that a station abandoned mid-interaction hands the next officer
# nothing but the pad, and the page returns itself to the pad rather than leaving a name on screen.
KIOSK_IDENTITY_SECONDS = 90
KIOSK_IDENTITY_SALT = "clock-kiosk-identity"
# A four-digit PIN is ten thousand possibilities, which is not a secret against an unbounded network
# guess — so the bound is the control, and it lives on the *station*, the one thing the company
# physically controls and can unlock. Twenty failures in five minutes pauses a station; a supervisor
# can clear it from the kiosk page without waiting.
KIOSK_FAILURE_LIMIT = 20
KIOSK_FAILURE_WINDOW = 300
# One wording for every refusal that does not want to confirm that a PIN, or an officer, exists.
PIN_REFUSAL = "Clock PIN not recognised."
KIOSK_PAUSED = "This clock station is paused after too many failed PIN attempts. A supervisor can clear it, or wait a few minutes."


def _kiosk_failure_key(kiosk):
    return f"clock-kiosk-failures:{kiosk.pk}"


def validate_clock_pin(pin):
    """Four to eight digits, and not a shape that hands itself to a guesser.

    Rejecting ``0000`` and ``1234`` is not fussiness: in a PIN system where several officers pick the
    same easy code, the weakest choice sets the cost of guessing *any* of them, because a guesser does
    not have to know whose PIN they are trying.
    """
    text = str(pin if pin is not None else "").strip()
    if not text.isdigit():
        raise ValidationError("A clock PIN has to be digits only.")
    if len(text) < PIN_MIN_DIGITS or len(text) > PIN_MAX_DIGITS:
        raise ValidationError(f"A clock PIN is between {PIN_MIN_DIGITS} and {PIN_MAX_DIGITS} digits.")
    if len(set(text)) == 1:
        raise ValidationError("A clock PIN cannot be one digit repeated.")
    digits = [int(ch) for ch in text]
    if all(b - a == 1 for a, b in zip(digits, digits[1:])) or all(a - b == 1 for a, b in zip(digits, digits[1:])):
        raise ValidationError("A clock PIN cannot be a run of consecutive digits.")
    return text


def clock_pin_index(organization, pin):
    """The lookup digest for a PIN: derived from the deployment secret, and not the PIN.

    A stretched hash cannot be searched, and searching it by trying every officer turns a lobby tablet
    into a PBKDF2 bench. This digest lets one query find the candidate; :func:`find_person_by_pin`
    still confirms against the stretched hash before it believes the match, so a digest collision or a
    half-written row cannot impersonate anyone.
    """
    message = f"clock-pin|{organization.pk}|{pin}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()


@transaction.atomic
def set_clock_pin(person, pin, actor=None, source="self"):
    """Give an officer their clock PIN. The PIN itself is never stored, logged, or echoed.

    ``source`` says how it arrived — the officer's own screen, a supervisor issuing one, or an import —
    because "who set this PIN" is the question an officer asks when they are locked out, and the audit
    chain is where that answer has to live.
    """
    text = validate_clock_pin(pin)
    digest = clock_pin_index(person.organization, text)
    if Person.objects.filter(organization=person.organization, clock_pin_index=digest).exclude(pk=person.pk).exists():
        # Checked here as well as by the model constraint, because MySQL does not enforce a
        # conditional unique index — and the human-facing answer has to be "pick another one", not an
        # IntegrityError. Two officers on one PIN is the end of the kiosk's evidence, not a detail.
        raise ValidationError("Another officer here already uses that PIN. Choose a different one.")
    person.clock_pin = make_password(text)
    person.clock_pin_index = digest
    person.pin_set_at = timezone.now()
    person.pin_failed_attempts = 0
    person.pin_locked_until = None
    person.save(update_fields=["clock_pin", "clock_pin_index", "pin_set_at", "pin_failed_attempts", "pin_locked_until"])
    AuditEvent.objects.create(organization=person.organization, actor=actor, action="person.pin_set",
        target_type="person", target_id=str(person.pk), metadata={"source": source, "digits": len(text)})
    return person


def clock_pin_lockout(person, now=None):
    """Seconds this PIN still refuses to be tried, or 0. Never negative."""
    if not person.pin_locked_until:
        return 0
    return max(0, int((person.pin_locked_until - (now or timezone.now())).total_seconds()))


def verify_clock_pin(person, pin, now=None):
    """Check a PIN against one *named* officer, charging the attempt to that officer.

    Attributable failures only exist when the identity came first — the officer's own change-PIN
    screen, where they are already signed in. A station that starts from digits cannot reach this
    function, because nothing is known about whom it is testing; the throttle that guards that path is
    :func:`find_person_by_pin`, and the lockout here is what stops someone who wrote down a
    colleague's PIN from wearing it out at the same pad.

    Deliberately not wrapped in a transaction. The refusal *is* the write: a caller that catches the
    ValidationError inside its own `transaction.atomic` would roll the savepoint back and lose the
    attempt count, which leaves a would-be guesser with an unlimited number of tries and a supervisor
    with no evidence anyone tried. Atomicity here would be a bug.
    """
    now = now or timezone.now()
    wait = clock_pin_lockout(person, now)
    if wait:
        raise ValidationError(f"This PIN is paused after too many attempts. Try again in {math.ceil(wait / 60)} minutes.")
    supplied = str(pin if pin is not None else "").strip()
    if not (person.clock_pin and supplied.isdigit() and check_password(supplied, person.clock_pin)):
        person.pin_failed_attempts = (person.pin_failed_attempts or 0) + 1
        if person.pin_failed_attempts >= PIN_MAX_FAILURES:
            person.pin_locked_until = now + timedelta(seconds=PIN_LOCKOUT_SECONDS)
            person.pin_failed_attempts = 0
            AuditEvent.objects.create(organization=person.organization, actor=None, action="person.pin_locked",
                target_type="person", target_id=str(person.pk), metadata={"lockout_seconds": PIN_LOCKOUT_SECONDS})
        person.save(update_fields=["pin_failed_attempts", "pin_locked_until"])
        raise ValidationError(PIN_REFUSAL)
    if person.pin_failed_attempts or person.pin_locked_until:
        person.pin_failed_attempts = 0
        person.pin_locked_until = None
        person.save(update_fields=["pin_failed_attempts", "pin_locked_until"])
    return person


def kiosk_pin_throttled(kiosk):
    return cache.get(_kiosk_failure_key(kiosk), 0) >= KIOSK_FAILURE_LIMIT


def register_kiosk_pin_failure(kiosk):
    """Count one refused PIN against the station, in a fixed window from the first failure.

    A refused guess at the pad is not attributable to an officer — the whole point of the digest
    lookup is that a wrong PIN names nobody — so the bound sits on the station instead. It writes no
    row per guess, because a guess that creates database work is a free way to fill a tenant's audit
    chain; the one row it does write is the moment the window trips, which is the fact a supervisor
    needs to know.
    """
    if kiosk is None:
        return 0
    key = _kiosk_failure_key(kiosk)
    cache.add(key, 0, KIOSK_FAILURE_WINDOW)
    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, KIOSK_FAILURE_WINDOW)
        count = 1
    if count == KIOSK_FAILURE_LIMIT:
        AuditEvent.objects.create(organization=kiosk.organization, actor=None, action="clock_kiosk.pin_throttled",
            target_type="clock_kiosk", target_id=str(kiosk.pk),
            metadata={"name": kiosk.name, "failures": count, "window_seconds": KIOSK_FAILURE_WINDOW})
    return count


def clear_kiosk_pin_failures(kiosk, actor=None):
    """A supervisor says the station is theirs again; the window is company state, not a sentence."""
    cache.delete(_kiosk_failure_key(kiosk))
    AuditEvent.objects.create(organization=kiosk.organization, actor=actor, action="clock_kiosk.pin_attempts_cleared",
        target_type="clock_kiosk", target_id=str(kiosk.pk), metadata={"name": kiosk.name})


def find_person_by_pin(organization, pin, kiosk=None, now=None):
    """Resolve typed digits to the officer who owns them, at a station that knows nothing else.

    A success is the only write worth keeping here, and there is none: the row is read, the counters
    are cleared, and the officer is handed back to the caller to punch with. Refusals are charged to
    the station by :func:`register_kiosk_pin_failure` before the raise, which is why this is *not*
    inside a transaction of its own — a rollback on the way out would erase the one record that a
    guess was ever made.
    """
    if kiosk is not None and kiosk_pin_throttled(kiosk):
        raise ValidationError(KIOSK_PAUSED)
    supplied = str(pin if pin is not None else "").strip()
    if not supplied.isdigit():
        register_kiosk_pin_failure(kiosk)
        raise ValidationError(PIN_REFUSAL)
    digest = clock_pin_index(organization, supplied)
    person = None
    for row in Person.objects.filter(organization=organization, clock_pin_index=digest):
        if row.clock_pin and check_password(supplied, row.clock_pin):
            person = row
            break
    if person is None or person.status == Person.Status.INACTIVE:
        # One wording for both. A lobby pad is not the place to confirm that a PIN belongs to someone
        # who no longer works here, and the difference is not one an honest officer needs.
        register_kiosk_pin_failure(kiosk)
        raise ValidationError(PIN_REFUSAL)
    wait = clock_pin_lockout(person, now)
    if wait:
        raise ValidationError(f"This PIN is paused after too many attempts. Try again in {math.ceil(wait / 60)} minutes.")
    if kiosk is not None:
        cache.delete(_kiosk_failure_key(kiosk))
    return person


def kiosk_identity_token(kiosk, person):
    """The short-lived proof that this officer, at this station, typed a PIN that worked."""
    return signing.dumps({"kiosk": str(kiosk.pk), "organization": str(kiosk.organization_id),
                          "person": str(person.pk)}, salt=KIOSK_IDENTITY_SALT)


def kiosk_identity(token, kiosk):
    """Who was verified at this station, while the window is open.

    The token names a person *and* a station, and both are re-checked: an identity proof carried to a
    second tablet would put this officer's time on a post the company never said they stood at, and
    the station is exactly the half the PIN says nothing about.
    """
    data = signing.loads(token, salt=KIOSK_IDENTITY_SALT, max_age=KIOSK_IDENTITY_SECONDS)
    if str(data.get("kiosk")) != str(kiosk.pk) or str(data.get("organization")) != str(kiosk.organization_id):
        raise ValidationError("This clock station's session was opened somewhere else. Enter the PIN again.")
    person = Person.objects.filter(pk=data.get("person"), organization_id=kiosk.organization_id).first()
    if person is None or person.status == Person.Status.INACTIVE:
        raise ValidationError("Enter the PIN again — that officer is no longer on the roster here.")
    return person


def kiosk_policy_refusal(organization, site=None):
    """What the resolved clock policy says about standing a shared station here, or ``None``.

    Checked twice by design: once when a manager opens the kiosk, so a forbidden post cannot be
    configured at all, and once on every punch, because a contract that takes the allowance away
    mid-week has to stop the station immediately rather than at the next enrolment.
    """
    state = effective_clock_policy(organization, site).kiosk
    if state["allowed"]:
        return None
    scope = state["source"]
    if scope == "site" and site is not None:
        return f"{site.name} has its own clock rule that does not allow a shared station. Each officer must clock from their own device there."
    if scope == "contract" and site is not None:
        return f"{site.client.name}'s contract does not allow a shared clock station. Each officer must clock from their own device on this contract."
    return "The company clock policy does not allow a shared clock station. Turn it on in Time policy to use one."


@transaction.atomic
def open_clock_kiosk(organization, name, site=None, actor=None):
    """Stand a shared station at a post, if the clock rule in force there allows one."""
    refusal = kiosk_policy_refusal(organization, site)
    if refusal:
        raise ValidationError(refusal)
    kiosk = ClockKiosk(organization=organization, name=(name or "").strip()[:120], site=site, created_by=actor)
    if not kiosk.name:
        raise ValidationError("Give the station a name the officers will recognise — 'Guard shack 2', not nothing.")
    kiosk.full_clean()
    kiosk.save()
    AuditEvent.objects.create(organization=organization, actor=actor, action="clock_kiosk.opened",
        target_type="clock_kiosk", target_id=str(kiosk.pk),
        metadata={"name": kiosk.name, "site": str(site.pk) if site else None})
    return kiosk


@transaction.atomic
def close_clock_kiosk(kiosk, actor=None):
    """Retire a station. Its punches stay — the row is what says where they were taken.

    Deactivating is the only form of deletion on offer. ``active`` is read from the database on every
    kiosk request, so a tablet that walks out stops working on the next keystroke, while the tour
    evidence it already recorded keeps its attribution and stays readable in the review screen.
    """
    kiosk.active = False
    kiosk.save(update_fields=["active"])
    clear_kiosk_pin_failures(kiosk, actor)
    AuditEvent.objects.create(organization=kiosk.organization, actor=actor, action="clock_kiosk.closed",
        target_type="clock_kiosk", target_id=str(kiosk.pk), metadata={"name": kiosk.name})
    return kiosk


def kiosk_shifts(person, now=None):
    """The posts this officer may clock, with the one to press already chosen.

    The kiosk earns its PIN by being faster than a phone. An officer who has just proved who they are
    should not have to read a list: their in-progress post goes first, and the answer says which row
    to tap. Every row here belongs to the person whose PIN was verified, which is also what makes a
    shared station safe to hand to the next officer standing in front of it.
    """
    now = now or timezone.now()
    rows = list(person.shifts.select_related("site__client").exclude(status=Shift.Status.CANCELLED).filter(
        starts_at__lte=now + timedelta(hours=12), ends_at__gte=now - timedelta(hours=12)).order_by("starts_at"))
    if not rows:
        return [], None
    inside = set()
    outside = set()
    for shift_id, kind in Punch.objects.filter(person=person, shift__in=[row.pk for row in rows]).values_list("shift_id", "kind"):
        if kind == Punch.Kind.IN:
            inside.add(shift_id)
        elif kind == Punch.Kind.OUT:
            outside.add(shift_id)
    offers = []
    for row in rows:
        if row.starts_at <= now <= row.ends_at:
            state = "on_post"
        elif row.starts_at > now:
            state = "upcoming"
        else:
            state = "ended"
        offers.append({"id": str(row.pk), "site": str(row.site), "client": row.site.client.name if row.site_id else "",
                       "starts_at": row.starts_at.isoformat(), "ends_at": row.ends_at.isoformat(),
                       "starts_label": timezone.localtime(row.starts_at).strftime("%a %d %b, %H:%M"),
                       "ends_label": timezone.localtime(row.ends_at).strftime("%H:%M"),
                       "state": state, "punched_in": row.pk in inside, "punched_out": row.pk in outside})
    recommended = next((row["id"] for row in offers if row["state"] == "on_post" and not row["punched_in"]), None)
    recommended = recommended or next((row["id"] for row in offers if row["state"] == "on_post"), None)
    recommended = recommended or next((row["id"] for row in offers if row["state"] == "upcoming" and not row["punched_in"]), None)
    return offers, recommended


# ── Audit retention: seal the period, then purge (roadmap §6, REC-4) ─────────────
#
# ``Organization.audit_retention_days`` was displayed on the audit page and enforced by nothing. It
# cannot be enforced with a ``DELETE``: every audit event stores the hash of the event before it, so
# removing the oldest rows leaves the first survivor claiming a predecessor that no longer exists, and
# the daily verification — or an insurer, or a court — reads that as tampering. The chain cannot be
# shortened. It can be continued from a head that is on record, which is what a seal is.
#
# Two steps, in this order, always: **seal** (write the period out and record the hashes it begins and
# ends on), then **purge** (delete the live rows, and only under the seal that accounts for them). On
# MySQL the delete is additionally refused by a trigger unless the session names a seal whose period
# covers the row, so "just this one old event, it's embarrassing" is not available to anybody with a
# query console.

AUDIT_MIN_RETENTION_DAYS = 90


def _month_start(value):
    local = timezone.localtime(value)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month_start(value):
    local = timezone.localtime(value)
    if local.month == 12:
        return local.replace(year=local.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return local.replace(month=local.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


def audit_retention_floor_note(organization):
    """Why nothing is due, when the company's own setting is too small to act on."""
    days = organization.audit_retention_days or 0
    if days < AUDIT_MIN_RETENTION_DAYS:
        return (f"Retention is set to {days} day(s), below the {AUDIT_MIN_RETENTION_DAYS}-day floor. "
                "Nothing is archived or purged until it is raised — a window that short is a wipe, not a policy.")
    return None


def audit_periods_due(organization, now=None):
    """Whole calendar months that sit entirely past the retention window, oldest first.

    Contiguity is enforced by construction: the walk starts at the previous seal's own end, so a period
    can only ever be sealed at the head of the chain. Months rather than "everything older than X",
    because a month is a period an auditor can name in a report and a boundary that shifts every day
    makes "which seal covers 2021?" unanswerable.
    """
    note = audit_retention_floor_note(organization)
    if note:
        return [], note
    cutoff = (now or timezone.now()) - timedelta(days=organization.audit_retention_days)
    boundary = _month_start(cutoff)
    last = organization.audit_seals.order_by("period_end").last()
    if last is not None:
        cursor = last.period_end
    else:
        earliest = organization.audit_events.order_by("occurred_at", "id").first()
        if earliest is None:
            return [], None
        cursor = _month_start(earliest.occurred_at)
    periods = []
    while cursor < boundary:
        following = _next_month_start(cursor)
        end = following if following <= boundary else boundary
        periods.append((cursor, end))
        cursor = end
    return periods, None


def _audit_manifest(events):
    """What kinds of action the period holds, so a seal reads as something without opening it."""
    counts = {}
    for event in events:
        counts[event.action] = counts.get(event.action, 0) + 1
    return [{"action": action, "count": count} for action, count in
            sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def _archive_line(row):
    return json.dumps(row, sort_keys=True, default=str)


def _archive_event_row(event):
    """One archived row, carrying exactly the fields its own hash was taken over.

    ``organization`` is included although the live export omits it, because a reader holding only the
    file has to be able to re-derive the hash, and the tenant id is inside it.
    """
    return {"kind": "event", "id": str(event.pk), "organization": str(event.organization_id),
            "actor": event.actor_id, "action": event.action, "target_type": event.target_type,
            "target_id": event.target_id, "metadata": event.metadata,
            "occurred_at": event.occurred_at.isoformat(), "previous_hash": event.previous_hash,
            "event_hash": event.event_hash}


def audit_archive_rows(seal):
    """The archived lines of a sealed period, read back from storage."""
    raw = seal.archive.read()
    if isinstance(raw, str):
        raw = raw.encode()
    return [json.loads(line) for line in raw.decode().splitlines() if line.strip()], raw


@transaction.atomic
def seal_audit_period(organization, period_start, period_end, actor=None):
    """Write one closed period out whole and record the hashes it begins and ends on."""
    if period_end <= period_start:
        raise ValidationError("A sealed period has to end after it starts.")
    errors = verify_audit_chain(organization)
    if errors:
        # The strongest reason to check before archiving rather than after: a seal hands a missing head
        # an official explanation. If the chain is broken for any other reason, sealing now launders it.
        raise ValidationError(f"The audit chain does not verify for {len(errors)} event(s). Nothing is "
                              "archived while a break is unexplained.")
    events = list(organization.audit_events.filter(
        occurred_at__gte=period_start, occurred_at__lt=period_end).order_by("occurred_at", "id"))
    if not events:
        raise ValidationError("Nothing to seal in that period.")
    last = organization.audit_seals.order_by("period_end").last()
    if last is not None and last.period_end != period_start:
        raise ValidationError("A seal has to continue the period before it, so the chain stays one line. "
                              f"The last seal ends at {last.period_end:%Y-%m-%d %H:%M}, not here.")
    first_previous = events[0].previous_hash
    if last is None:
        if first_previous:
            raise ValidationError("The first sealed period must start at the beginning of the chain.")
    elif first_previous != last.last_hash:
        raise ValidationError("The oldest event in this period does not continue the previous seal, so "
                              "there is a gap nobody has accounted for.")
    redactions = list(AuditRedaction.objects.filter(event__in=events).order_by("created_at"))
    lines = [{"kind": "seal", "organization": str(organization.pk), "period_start": period_start.isoformat(),
              "period_end": period_end.isoformat(), "first_previous": first_previous,
              "first_hash": events[0].event_hash, "last_hash": events[-1].event_hash,
              "event_count": len(events)},
             *[_archive_event_row(event) for event in events],
             *[{"kind": "redaction", "id": str(row.pk), "event": str(row.event_id), "fields": row.fields,
                "reason": row.reason, "legal_basis": row.legal_basis, "status": row.status,
                "requested_by": row.requested_by_id, "approved_by": row.approved_by_id,
                "created_at": row.created_at.isoformat()} for row in redactions]]
    data = ("\n".join(_archive_line(line) for line in lines) + "\n").encode()
    seal = AuditSeal(organization=organization, period_start=period_start, period_end=period_end,
        first_previous=first_previous, first_hash=events[0].event_hash, last_hash=events[-1].event_hash,
        event_count=len(events), redaction_count=len(redactions), manifest=_audit_manifest(events),
        archive_sha256=hashlib.sha256(data).hexdigest(), archive_bytes=len(data),
        retention_days=organization.audit_retention_days, sealed_by=actor)
    seal.full_clean()
    seal.save()
    seal.archive.save(audit_seal_path(seal, "seal.ndjson"), ContentFile(data), save=True)
    AuditEvent.objects.create(organization=organization, actor=actor, action="audit.sealed",
        target_type="audit_seal", target_id=str(seal.pk),
        metadata={"period_start": period_start.isoformat(), "period_end": period_end.isoformat(),
                  "events": len(events), "first_hash": seal.first_hash, "last_hash": seal.last_hash,
                  "archive_sha256": seal.archive_sha256, "archive_bytes": seal.archive_bytes,
                  "redactions": seal.redaction_count, "retention_days": seal.retention_days})
    return seal


def verify_seal_archive(seal):
    """Re-derive the chain from the archive alone, and say what does not follow."""
    if not seal.archive:
        return {"events": 0, "problems": ["the seal has no archive to check"]}
    lines, raw = audit_archive_rows(seal)
    problems = []
    digest = hashlib.sha256(raw).hexdigest()
    if digest != seal.archive_sha256:
        problems.append("the archive no longer hashes to what the seal recorded")
    events = [row for row in lines if row.get("kind") == "event"]
    header = next((row for row in lines if row.get("kind") == "seal"), None)
    previous = (header or {}).get("first_previous", seal.first_previous)
    for row in events:
        expected = audit_event_hash(id=row["id"], organization=row["organization"], actor=row["actor"],
            action=row["action"], target_type=row["target_type"], target_id=row["target_id"],
            metadata=row["metadata"], previous_hash=row["previous_hash"])
        if row["previous_hash"] != previous:
            problems.append(f"{row['id']} does not follow the row before it")
        if row["event_hash"] != expected:
            problems.append(f"{row['id']} does not hash to the fields recorded beside it")
        previous = row["event_hash"]
    if not events:
        problems.append("the archive holds no events")
    else:
        if events[0]["event_hash"] != seal.first_hash:
            problems.append("the archive does not begin on the hash the seal names")
        if events[-1]["event_hash"] != seal.last_hash:
            problems.append("the archive does not end on the hash the seal names")
    if header is not None and header.get("event_count") != len(events):
        problems.append("the archive holds a different number of rows than its header counts")
    if len(raw) != seal.archive_bytes:
        problems.append("the archive is a different size than the seal recorded")
    return {"events": len(events), "problems": problems, "sha256": digest, "bytes": len(raw)}


def _sql_datetime(value):
    """A DATETIME literal the driver accepts in a raw statement (UTC, no tzinfo object)."""
    return value.astimezone(utc_reference.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


@transaction.atomic
def purge_sealed_audit(seal, actor=None):
    """Remove a sealed period from the live table, under the seal that accounts for it.

    Every precondition is checked in Python as well as by the MySQL trigger: the hermetic leg never runs
    that SQL, and the service is the thing a management command and any future import path both go
    through. The period is re-counted inside the same transaction — a span that is not *exactly* the
    sealed rows aborts the whole delete rather than leaving a half-trimmed chain behind.
    """
    if seal.status == AuditSeal.Status.PURGED:
        return seal
    checked = verify_seal_archive(seal)
    if checked["problems"]:
        raise ValidationError("Nothing is purged until the archive reads back exactly as it was sealed: "
                              + "; ".join(checked["problems"]) + ".")
    errors = verify_audit_chain(seal.organization)
    if errors:
        raise ValidationError(f"The live chain does not verify for {len(errors)} event(s). Purging now "
                              "would destroy the only copy of a period that might explain them.")
    if seal.redaction_count:
        raise ValidationError(f"{seal.redaction_count} event(s) in this period carry a redaction decision. "
                              "A redaction protects its own event row, so settle those first — the decision "
                              "and the record it governs cannot be pulled apart.")
    live = seal.organization.audit_events.filter(
        occurred_at__gte=seal.period_start, occurred_at__lt=seal.period_end).count()
    if live != seal.event_count:
        raise ValidationError(f"{live} live events fall in this period but the seal recorded "
                              f"{seal.event_count}. Refusing: the seal and the table disagree about what "
                              "is being deleted.")
    with connection.cursor() as cursor:
        # The session names the seal and the trigger allows no delete its period does not cover. Both
        # ids are `.hex`: these columns are char(32) and a dashed UUID does not fit one.
        #
        # `SET @var` is MySQL syntax and there is no trigger to satisfy on any other backend, so the
        # statement is vendor-gated rather than issued everywhere. On sqlite the Python preconditions
        # above are the whole guard — which is why they are written in Python at all.
        if connection.vendor == "mysql":
            cursor.execute("SET @audit_purge_seal = %s", [seal.pk.hex])
        cursor.execute(
            "DELETE FROM core_auditevent WHERE organization_id=%s AND occurred_at >= %s AND occurred_at < %s",
            [seal.organization_id.hex, _sql_datetime(seal.period_start), _sql_datetime(seal.period_end)])
    remaining = seal.organization.audit_events.filter(
        occurred_at__gte=seal.period_start, occurred_at__lt=seal.period_end).count()
    if remaining:
        raise ValidationError(f"{remaining} of the period's events survived the delete. Rolled back — "
                              "a partly trimmed chain is worse than a long one.")
    seal.status = AuditSeal.Status.PURGED
    seal.purged_at = timezone.now()
    seal.purged_by = actor
    seal.save(update_fields=["status", "purged_at", "purged_by"])
    AuditEvent.objects.create(organization=seal.organization, actor=actor, action="audit.purged",
        target_type="audit_seal", target_id=str(seal.pk),
        metadata={"period_start": seal.period_start.isoformat(), "period_end": seal.period_end.isoformat(),
                  "events": seal.event_count, "archive_sha256": seal.archive_sha256,
                  "head_hash": seal.last_hash})
    return seal


def audit_retention_state(organization, now=None):
    """Everything the audit page has to admit about its own history.

    `pending_purges` is the resumable half of "seal, then purge". A period can be archived and left
    untrimmed — by ``--no-purge``, by a redaction that blocked the delete, or by a run that stopped
    between the two steps — and then `due_periods` comes back empty on the next pass, because the walk
    correctly continues from the last seal's end. Without naming the pending purge, retention would read
    as "nothing due" forever while the live table still held the whole period.
    """
    periods, note = audit_periods_due(organization, now)
    seals = list(organization.audit_seals.order_by("period_start"))
    purged = [row for row in seals if row.status == AuditSeal.Status.PURGED]
    pending = [row for row in seals if row.status == AuditSeal.Status.SEALED]
    oldest = organization.audit_events.order_by("occurred_at", "id").first()
    if note:
        message = note
    elif periods:
        message = (f"{len(periods)} closed period(s) are past the {organization.audit_retention_days}-day "
                   "retention window and can be sealed.")
    elif pending:
        message = (f"{len(pending)} archived period(s) are still in the live table. Sealing is only half of "
                   "retention; the trim has to run too.")
    else:
        message = f"Nothing is due under the {organization.audit_retention_days}-day window."
    return {"retention_days": organization.audit_retention_days, "floor_note": note,
            "due_periods": periods, "seals": seals, "purged_seals": purged,
            "pending_purges": pending, "pending_events": sum(row.event_count for row in pending),
            "sealed_events": sum(row.event_count for row in purged),
            "live_events": organization.audit_events.count(),
            "oldest_live": oldest.occurred_at if oldest else None,
            "chain_errors": verify_audit_chain(organization), "message": message}


# ── Messaging consent, suppression, and provider callbacks (roadmap §5, NTF-4) ───
#
# DD §Email, text messaging, and templates requires two things that had no code behind them: that
# "delivery, bounce, complaint, unsubscribe, and suppression events are processed and retained", and that
# mandatory operational messages are "categorised separately from optional" ones. RB §Email and SMS
# providers adds the warning this section is built around: do **not** assume email-unsubscribe rules and
# mandatory operational-SMS rules are identical. They are not, and the asymmetry is encoded once, in
# `send_block_reason`, rather than being re-litigated at each of the forty call sites that queue a notice.
#
# The owner's ruling of 2026-10-03 fixed the capture points: opt-in at the account's own start —
# invitation acceptance, first sign-in — and from the officer's page, with nothing sent to a number that
# has not affirmatively opted in, and an opt-out ending the send path immediately.

SMS_CONSENT_WORDING = ("Texts from {organization} about your assigned posts, clock reminders and "
    "timecard questions. Message and data rates may apply. Reply HELP for help or STOP to stop "
    "receiving texts anytime.")
INBOUND_STOP_WORDS = ("stop", "end", "quit", "cancel", "unsubscribe", "optout", "opt out")
INBOUND_START_WORDS = ("start", "begin", "subscribe", "unstop", "optin", "opt in", "hire")
INBOUND_HELP_WORDS = ("help", "assistance", "info")

# Twilio's documented Messaging error codes, narrowed to the ones that change whether this number may be
# texted again. An unlisted code is recorded and left alone: guessing that "code 30010" means a dead
# number would suppress a reachable officer off a number that was never in trouble.
TWILIO_UNREACHABLE_CODES = {
    "21612": (Suppression.Kind.UNKNOWN_NUMBER, "Invalid destination number"),
    "30001": (Suppression.Kind.UNKNOWN_NUMBER, "Unknown error from the carrier"),
    "30003": (Suppression.Kind.HARD_BOUNCE, "Carrier returned a hard error"),
    "30004": (Suppression.Kind.UNKNOWN_NUMBER, "Landline or carrier does not support messaging"),
    "30005": (Suppression.Kind.UNKNOWN_NUMBER, "Number is unreachable"),
    "30006": (Suppression.Kind.UNKNOWN_NUMBER, "Number is not in service"),
    "30007": (Suppression.Kind.UNSUBSCRIBE, "Recipient is unsubscribed from this sender"),
    "30008": (Suppression.Kind.UNKNOWN_NUMBER, "Number cannot receive messages"),
    "30009": (Suppression.Kind.UNKNOWN_NUMBER, "Number is unreachable"),
    "30010": (Suppression.Kind.UNKNOWN_NUMBER, "Carrier blocked the message"),
    "30024": (Suppression.Kind.UNKNOWN_NUMBER, "Number is invalid for messaging"),
}


def mask_destination(destination, channel):
    """What the audit chain gets to know about a contact point: enough to match, not enough to use.

    An audit event is append-only and survives every retention rule REC-4 has, so a full phone number or
    address written into the chain is PII that cannot be scrubbed later. The consent ledger holds the
    real destination because that is what the proof is about; the chain holds four bullets and the last
    four characters. The bullets are a fixed count rather than one per hidden character on purpose — a
    mask that reveals the number's length is revealing something too.
    """
    text = str(destination or "")
    if channel == MessageConsent.Channel.EMAIL:
        local, _, domain = text.partition("@")
        return f"{(local[:1] or '?')}***@{domain}" if domain else "***"
    return "••••" + text[-4:]


def normalize_destination(value, channel=MessageConsent.Channel.SMS):
    """The form a destination is stored and compared in, or "" when it cannot be one.

    Deliberately not a validator with country rules — this build has no number-validation service, and
    RB §Email and SMS providers lists number validation as part of the SMS interface still to have. What
    *is* enforced here is the thing that decides whether consent means anything: "+1 (214) 555-0142",
    "1 214 555 0142" and "+12145550142" must be one key. A ledger that stored three keys for one phone
    would tell the company the officer never opted in, three times over.

    So numbers are canonicalised to E.164 on the North American assumption this product's market implies
    — ten bare digits gain the country code, and the leading "+" is always present so the string handed
    to Twilio or SNS is the string that was consented to. A number from anywhere else keeps its digits
    with a "+" and stays that company's to correct: guessing a country code would text a stranger.
    """
    text = str(value or "").strip()
    if channel == MessageConsent.Channel.EMAIL:
        return text.lower() if "@" in text and " " not in text else ""
    digits = re.sub(r"\D", "", text)
    if not 7 <= len(digits) <= 15:
        return ""
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits


def sms_consent_wording(organization):
    return SMS_CONSENT_WORDING.format(organization=organization.display_name)


def organization_webhook_token(organization):
    """The address half of this company's callback endpoint, minted on first need."""
    from django.utils.crypto import get_random_string
    if not organization.webhook_token:
        organization.webhook_token = get_random_string(40)
        organization.save(update_fields=["webhook_token"])
    return organization.webhook_token


def rotate_webhook_token(organization, actor=None):
    organization.webhook_token = None
    token = organization_webhook_token(organization)
    AuditEvent.objects.create(organization=organization, actor=actor, action="organization.webhook_rotated",
        target_type="organization", target_id=str(organization.pk), metadata={})
    return token


def record_consent(*, organization, destination, state, channel=MessageConsent.Channel.SMS, person=None,
        source=MessageConsent.Source.PROFILE, wording="", evidence=None, actor=None):
    """Append one consent decision. It never rewrites the decision it reverses.

    ``state`` is a grant or a revocation; both are rows, newest wins. Suppression follows the decision
    and not the other way round: a revocation blocks the number, and a grant lifts only the block that a
    *decision* created (``unsubscribe``), never a bounce or a spam complaint — an address the carrier
    rejects is not made reachable by asking it nicely again.
    """
    normalized = normalize_destination(destination, channel)
    if not normalized:
        raise ValidationError({"destination": "That is not a usable number for text messages."
            if channel == MessageConsent.Channel.SMS else "That is not a usable email address."})
    if person is not None and person.organization_id != organization.pk:
        raise ValidationError("Consent must be recorded for an officer in this organization.")
    row = MessageConsent(organization=organization, person=person, channel=channel, destination=normalized,
        state=state, source=source, wording=(wording or sms_consent_wording(organization)) if channel == MessageConsent.Channel.SMS else wording,
        evidence=dict(evidence or {}), recorded_by=actor, decided_at=timezone.now())
    row.save()
    if state == MessageConsent.State.REVOKED:
        set_suppression(organization=organization, destination=normalized, channel=channel,
            kind=Suppression.Kind.UNSUBSCRIBE, reason="Opted out", provider=source,
            reference=str(row.pk), actor=actor)
    else:
        clear_suppression(organization=organization, destination=normalized, channel=channel,
            kinds=(Suppression.Kind.UNSUBSCRIBE,), actor=actor, why="Re-consented")
    AuditEvent.objects.create(organization=organization, actor=actor,
        action="message.consent_granted" if state == MessageConsent.State.GRANTED else "message.consent_revoked",
        target_type="person" if person else "organization", target_id=str(person.pk) if person else str(organization.pk),
        metadata={"channel": channel, "destination": mask_destination(normalized, channel), "source": source,
                  "has_wording": bool(row.wording), "ledger": str(row.pk)})
    return row


def current_consent(organization, destination, channel=MessageConsent.Channel.SMS):
    normalized = normalize_destination(destination, channel)
    if not normalized:
        return None
    return MessageConsent.objects.filter(organization=organization, channel=channel,
        destination=normalized).order_by("-decided_at", "-created_at").first()


def sms_opted_in(organization, destination, channel=MessageConsent.Channel.SMS):
    row = current_consent(organization, destination, channel)
    return bool(row and row.state == MessageConsent.State.GRANTED)


def set_suppression(*, organization, destination, kind, channel=MessageConsent.Channel.SMS, reason="",
        provider="", reference="", actor=None):
    """Block a destination, or restate why it is already blocked. One live row per destination."""
    normalized = normalize_destination(destination, channel)
    if not normalized:
        return None
    row, created = Suppression.objects.update_or_create(organization=organization, channel=channel,
        destination=normalized, defaults={"kind": kind, "reason": str(reason)[:255], "provider": provider,
            "source_reference": str(reference)[:120], "cleared_at": None, "cleared_by": None})
    if created:
        AuditEvent.objects.create(organization=organization, actor=actor, action="message.suppressed",
            target_type="suppression", target_id=str(row.pk),
            metadata={"channel": channel, "destination": mask_destination(normalized, channel),
                      "kind": kind, "reason": row.reason})
    return row


def clear_suppression(*, organization, destination, channel=MessageConsent.Channel.SMS, kinds=None,
        actor=None, why=""):
    """Lift a block — only the kinds the caller can justify lifting."""
    normalized = normalize_destination(destination, channel)
    if not normalized:
        return 0
    rows = Suppression.objects.filter(organization=organization, channel=channel, destination=normalized,
        cleared_at__isnull=True)
    if kinds:
        rows = rows.filter(kind__in=kinds)
    cleared = 0
    for row in rows:
        row.cleared_at = timezone.now()
        row.cleared_by = actor
        row.save(update_fields=["cleared_at", "cleared_by"])
        cleared += 1
        AuditEvent.objects.create(organization=organization, actor=actor, action="message.unsuppressed",
            target_type="suppression", target_id=str(row.pk),
            metadata={"channel": channel, "destination": mask_destination(normalized, channel),
                      "kind": row.kind, "why": why})
    return cleared


def active_suppression(organization, destination, channel=MessageConsent.Channel.SMS):
    normalized = normalize_destination(destination, channel)
    if not normalized:
        return None
    return Suppression.objects.filter(organization=organization, channel=channel,
        destination=normalized, cleared_at__isnull=True).first()


def message_recipient_destination(notification):
    """Where this notice would actually go, or "" when there is nowhere."""
    if notification.channel == Notification.Channel.IN_APP:
        return ""
    person = notification.organization.people.filter(user=notification.recipient).first() \
        if notification.recipient_id else None
    if notification.channel == Notification.Channel.SMS:
        return notification.destination or getattr(person, "mobile_phone", "") or ""
    return notification.destination or (notification.recipient.email if notification.recipient else "")


def send_block_reason(notification):
    """Why this notice must not reach a provider, or None to send it.

    The asymmetry RB asks for, stated in one place:

    * **Text** is consent-gated absolutely and permanently. Nothing this product sends by SMS may go to
      a number without a current grant, *including* a mandatory notice — "it was only a pay stub" is not
      a defence against a text to a number that never opted in, and a STOP ends the path rather than
      downgrading the category.
    * **Email** treats an unsubscribe as a rule about *optional* mail. A pay stub or a security alert
      still goes to a mailbox that unsubscribed from schedule notices, because CAN-SPAM carves
      transactional mail out; a dead mailbox or an administrator's block stops even mandatory mail,
      because those are facts about delivery, not about wishes.

    Returns a reason a recipient can act on, and which never quotes the full address back.
    """
    if notification.channel == Notification.Channel.IN_APP:
        return None
    channel = MessageConsent.Channel.SMS if notification.channel == Notification.Channel.SMS \
        else MessageConsent.Channel.EMAIL
    destination = message_recipient_destination(notification)
    if not destination:
        return "No address on file for this recipient."
    normalized = normalize_destination(destination, channel)
    if not normalized:
        return "The stored contact details are not usable for this channel."
    block = active_suppression(notification.organization, normalized, channel)
    if channel == MessageConsent.Channel.SMS:
        if not sms_opted_in(notification.organization, normalized, channel):
            return ("No text-message consent is on file for this number, so nothing was sent. "
                    "Ask the officer to opt in from their Text alerts page.")
        if block:
            return f"This number is blocked for text messages ({block.get_kind_display()})."
        return None
    if block and (not notification.mandatory or block.kind in
            (Suppression.Kind.UNKNOWN_NUMBER, Suppression.Kind.MANUAL)):
        return (f"This address is suppressed ({block.get_kind_display()})."
                if not notification.mandatory else
                f"Even a required notice is not sent to {block.get_kind_display().lower()} here.")
    return None


def callback_requires_signature(provider):
    """Whether this deployment expects the provider to have signed the post.

    A function rather than the view reading `settings` itself: `views.py` has a view named `settings`,
    which rebinds that module global, so `getattr(settings, "TWILIO_AUTH_TOKEN", "")` written in the
    view asks a Python function object for an attribute and quietly gets the default. The first version
    of this guard failed that way — an unsigned callback answered 200 — and only the test that asserted
    the refusal caught it, because nothing about the expression looks wrong.
    """
    return provider == Organization.SmsProvider.TWILIO and bool(getattr(settings, "TWILIO_AUTH_TOKEN", ""))


def twilio_signature(url, params, auth_token):
    """Twilio's documented scheme: base64(HMAC-SHA1(token, url + sorted(key+value) pairs))."""
    body = url + "".join(f"{key}{value}" for key, value in sorted((params or {}).items()))
    return base64.b64encode(hmac.new(auth_token.encode(), body.encode(), hashlib.sha1).digest()).decode()


def callback_is_signed(provider, request, params):
    """Whether the provider proved it sent this. Absent configuration is not the same as verified.

    Only Twilio signs. When ``TWILIO_AUTH_TOKEN`` is configured the signature is required — a wrong one
    is refused outright. When it is not, the payload is accepted on the organization's token in the path
    and marked unverified, because pretending otherwise would be a claim this build cannot support.
    """
    if provider != Organization.SmsProvider.TWILIO:
        return False
    token = getattr(settings, "TWILIO_AUTH_TOKEN", "")
    signature = request.headers.get("X-Twilio-Signature", "")
    if not token:
        return False
    return bool(signature) and hmac.compare_digest(signature, twilio_signature(request.build_absolute_uri(), params, token))


def _ts(value):
    """A provider's timestamp string, or None. Never a guess dressed up as a fact."""
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else timezone.make_aware(parsed)


def _event(destination, kind, detail="", reference="", occurred_at=None, channel=MessageConsent.Channel.SMS):
    return {"destination": destination, "kind": kind, "detail": detail, "reference": reference,
            "occurred_at": occurred_at, "channel": channel}


def normalize_provider_callback(provider, *, params=None, payload=None):
    """Every provider's callback shape, reduced to destination + kind + why.

    Written against what each provider documents rather than one assumed wire format: Twilio posts
    form-encoded status callbacks (and the same shape for an inbound message), Amazon SNS delivers an
    envelope whose ``Message`` is itself JSON, Postmark posts one record per ``RecordType``, Mailjet an
    array of events. Anything unrecognized comes back as an empty list rather than as a guess, and the
    caller still retains the raw body — an unmapped event is a gap to fix, not a reason to invent state.
    """
    params = params or {}
    payload = payload or {}
    events = []
    if provider == Organization.SmsProvider.TWILIO:
        status = (params.get("MessageStatus") or params.get("messageStatus") or "").lower()
        from_number = params.get("From") or params.get("from") or ""
        to_number = params.get("To") or params.get("to") or ""
        code = params.get("ErrorCode") or params.get("errorCode") or ""
        reference = params.get("MessageSid") or params.get("messageSid") or ""
        if status == "received" or (not status and from_number):
            events.append(_event(from_number, DeliveryEvent.Kind.INBOUND,
                (params.get("Body") or params.get("body") or "")[:255], reference))
        elif status:
            kind = {"delivered": DeliveryEvent.Kind.DELIVERED, "sent": DeliveryEvent.Kind.ACCEPTED,
                    "queued": DeliveryEvent.Kind.ACCEPTED, "in_progress": DeliveryEvent.Kind.ACCEPTED,
                    "sending": DeliveryEvent.Kind.ACCEPTED, "undelivered": DeliveryEvent.Kind.BOUNCE,
                    "failed": DeliveryEvent.Kind.FAILED}.get(status, DeliveryEvent.Kind.STATUS)
            mapped = TWILIO_UNREACHABLE_CODES.get(str(code))
            detail = f"{code}: {params.get('ErrorMsg') or ''}"[:255] if code else status
            events.append(_event(to_number, kind, detail, reference))
            if mapped:
                events[-1]["suppression"] = mapped[0]
                events[-1]["suppression_reason"] = mapped[1]
            if str(code) == "30007":
                events[-1]["kind"] = DeliveryEvent.Kind.UNSUBSCRIBE
    elif provider == "sns":
        inner = payload.get("Message")
        body = json.loads(inner) if isinstance(inner, str) else (payload or {})
        mail = body.get("mail") or {}
        notification_type = (body.get("notificationType") or payload.get("Type") or "").lower()
        recipients = [row.get("emailAddress", "") for row in
            ((body.get("bounce") or {}).get("bouncedRecipients") or [])] or \
            [str(item) for item in (mail.get("destination") or [])]
        for recipient in [r for r in recipients if r]:
            kind = {"bounce": DeliveryEvent.Kind.BOUNCE, "complaint": DeliveryEvent.Kind.COMPLAINT,
                    "delivery": DeliveryEvent.Kind.DELIVERED}.get(notification_type, DeliveryEvent.Kind.STATUS)
            bounce_type = str((body.get("bounce") or {}).get("bounceType") or "")
            events.append(_event(recipient, kind, bounce_type or notification_type,
                mail.get("messageId") or body.get("messageId") or "", _ts(mail.get("timestamp")),
                MessageConsent.Channel.EMAIL))
            if bounce_type.lower() == "permanent":
                events[-1]["suppression"] = Suppression.Kind.HARD_BOUNCE
    elif provider == "postmark":
        record_type = (payload.get("RecordType") or payload.get("HookMessageType") or "").lower()
        recipient = payload.get("Email") or payload.get("Recipient") or ""
        kind = {"bounce": DeliveryEvent.Kind.BOUNCE, "spamcomplaint": DeliveryEvent.Kind.COMPLAINT,
                "subscriptionchange": DeliveryEvent.Kind.UNSUBSCRIBE, "delivery": DeliveryEvent.Kind.DELIVERED,
                "open": DeliveryEvent.Kind.STATUS}.get(record_type, DeliveryEvent.Kind.STATUS)
        bounce_type = str(payload.get("BounceType") or "")
        if kind == DeliveryEvent.Kind.UNSUBSCRIBE and payload.get("SuppressSending") is False:
            events.append(_event(recipient, kind, "Resubscribed", payload.get("MessageID", ""),
                _ts(payload.get("DeliveredAt") or payload.get("ChangedAt")), MessageConsent.Channel.EMAIL))
            events[-1]["resubscribed"] = True
        elif recipient:
            events.append(_event(recipient, kind, bounce_type or record_type,
                payload.get("MessageID", ""), _ts(payload.get("DeliveredAt") or payload.get("BounceDate")),
                MessageConsent.Channel.EMAIL))
            if bounce_type.lower() in ("hardbounce", "spamcomplaint"):
                events[-1]["suppression"] = (Suppression.Kind.HARD_BOUNCE if bounce_type.lower() == "hardbounce"
                                             else Suppression.Kind.COMPLAINT)
    elif provider == "mailjet":
        rows = payload if isinstance(payload, list) else payload.get("Events") or []
        for row in rows:
            name = str(row.get("event") or row.get("Event") or "").lower()
            address = row.get("email") or row.get("Email") or row.get("dest") or ""
            kind = {"bounced": DeliveryEvent.Kind.BOUNCE, "hard_bounced": DeliveryEvent.Kind.BOUNCE,
                    "blocked": DeliveryEvent.Kind.BOUNCE, "spamcomplained": DeliveryEvent.Kind.COMPLAINT,
                    "unsubscribed": DeliveryEvent.Kind.UNSUBSCRIBE, "delivered": DeliveryEvent.Kind.DELIVERED,
                    "opened": DeliveryEvent.Kind.STATUS, "clicked": DeliveryEvent.Kind.STATUS,
                    "sent": DeliveryEvent.Kind.ACCEPTED}.get(name, DeliveryEvent.Kind.STATUS)
            if not address:
                continue
            events.append(_event(address, kind, name, str(row.get("id") or row.get("MessageID") or ""),
                _ts(row.get("time") or row.get("DateTro")), MessageConsent.Channel.EMAIL))
            if name in ("bounced", "hard_bounced", "blocked"):
                events[-1]["suppression"] = Suppression.Kind.HARD_BOUNCE
            elif name == "spamcomplained":
                events[-1]["suppression"] = Suppression.Kind.COMPLAINT
    return events


@transaction.atomic
def ingest_provider_events(organization, provider, events, *, verified=False, raw=None):
    """Retain each callback and act on the ones that change a future send.

    Retention is unconditional; action is not. A bounce produces a suppression *and* leaves the notice
    that bounced where it was — rewriting history to say "blocked" would misreport what the provider was
    actually asked to do. What changes is the next attempt, and the ledger for that is
    :func:`send_block_reason`.
    """
    stored = []
    for event in events:
        channel = event.get("channel", MessageConsent.Channel.SMS)
        destination = normalize_destination(event.get("destination"), channel)
        if not destination:
            continue
        applied = False
        kind = event["kind"]
        if kind == DeliveryEvent.Kind.UNSUBSCRIBE and not event.get("resubscribed"):
            record_consent(organization=organization, destination=destination, channel=channel,
                state=MessageConsent.State.REVOKED, source=MessageConsent.Source.PROVIDER_KEYWORD,
                wording=event.get("detail", ""), evidence={"provider": provider})
            applied = True
        elif event.get("resubscribed"):
            record_consent(organization=organization, destination=destination, channel=channel,
                state=MessageConsent.State.GRANTED, source=MessageConsent.Source.PROVIDER_KEYWORD,
                wording=event.get("detail", ""), evidence={"provider": provider})
            applied = True
        elif kind == DeliveryEvent.Kind.INBOUND:
            # An inbound reply is handled by `handle_inbound_message`, which has to answer the number;
            # it is retained here so the STOP and its answer sit in the same table.
            applied = False
        elif event.get("suppression"):
            set_suppression(organization=organization, destination=destination, channel=channel,
                kind=event["suppression"], reason=event.get("suppression_reason") or event.get("detail", ""),
                provider=provider, reference=event.get("reference", ""))
            applied = True
        elif kind == DeliveryEvent.Kind.COMPLAINT:
            set_suppression(organization=organization, destination=destination, channel=channel,
                kind=Suppression.Kind.COMPLAINT, reason=event.get("detail", "Marked as spam"),
                provider=provider, reference=event.get("reference", ""))
            applied = True
        stored.append(DeliveryEvent.objects.create(organization=organization, provider=provider,
            channel=channel, destination=destination, kind=kind,
            message_reference=str(event.get("reference") or "")[:160], detail=str(event.get("detail") or "")[:255],
            occurred_at=event.get("occurred_at"), applied=applied, verified=verified,
            raw=dict(raw or {}) if len(json.dumps(raw or {})) < 4000 else {"omitted": True}))
    return stored


# Amazon's confirmation endpoint, and nothing else. The China partition (`amazonaws.com.cn`) is absent
# on purpose rather than by oversight: it is a separate authority with its own DNS root, and an
# installation that genuinely runs there will find this refusal and name the missing case, which is a
# better failure mode than a pattern wide enough to be worth nothing.
SNS_CONFIRM_HOST_PATTERN = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com$")
SNS_CONFIRM_TIMEOUT_SECONDS = 10


def sns_confirmation_target(url):
    """The confirmation address this application will fetch, or a refusal naming what was wrong.

    This is the only outbound URL fetch in the product, and it exists because a `SubscribeURL` arrives
    **inside a request body that an unauthenticated caller wrote**. Following one blindly is the
    textbook SSRF primitive: post a `SubscriptionConfirmation` whose URL points at
    `169.254.169.254` and the application fetches its own cloud credentials with its own privileges.
    So the rule is an allow-list on the *host that the URL claims*, matched against Amazon's documented
    naming, applied before any socket is opened:

      * `https` only — a plain-http "SNS" endpoint is not Amazon's;
      * host must match `sns.<region>.amazonaws.com`, which rejects an IP literal, an internal name,
        `evil.example/?x=sns.us-east-1.amazonaws.com` (that string is the query, not the host), and the
        userinfo trick `https://sns.us-east-1.amazonaws.com@evil.example/`;
      * no port other than the default, no path — the real endpoint answers at `/`;
      * the query has to carry the action it claims, so a stored URL that got edited into something
        else is refused rather than fetched.

    Even with all four, the fetch does not follow redirects and its reply is read as text only.
    """
    from urllib.parse import urlsplit
    parts = urlsplit(str(url or ""))
    if parts.scheme != "https":
        raise ValidationError("A confirmation address has to be https.")
    if not SNS_CONFIRM_HOST_PATTERN.match(parts.hostname or ""):
        raise ValidationError("That confirmation address is not an Amazon SNS endpoint, so it will not "
                              "be opened.")
    if parts.port not in (None, 443):
        raise ValidationError("A confirmation address has to use the standard https port.")
    if parts.path not in ("", "/"):
        raise ValidationError("A confirmation address has to point at the SNS endpoint itself.")
    if "Action=ConfirmSubscription" not in (parts.query or ""):
        raise ValidationError("That address does not ask to confirm a subscription.")
    return url


def _sns_fetch(url):
    """Open one allow-listed confirmation address and report the status and the body, without walking."""
    import requests
    response = requests.get(url, timeout=SNS_CONFIRM_TIMEOUT_SECONDS, allow_redirects=False)
    return response.status_code, response.text[:4000]


def confirm_sns_subscription(organization, event, actor, *, fetch=None):
    """Click the confirmation AWS is waiting on, for a subscription that arrived on this tenant's token.

    AWS publishes nothing to an endpoint until the subscription behind its `SubscribeURL` is confirmed,
    so an SNS deployment without this action has a callback address and no events — the open end the
    NTF-4 slice recorded rather than pretending it was finished. The reason it stayed open is the
    reason it is still a human click and not an automatic one: the URL came from an unauthenticated
    body. An operator who can see the topic decides; the allow-list decides what that click may reach.

    **Deliberately not wrapped in `transaction.atomic`.** The record of the attempt has to survive a
    refused or failed confirmation — this schema's standing rule is "retention is unconditional, action
    is not" — and an atomic block here would roll the attempt's own row back on the way out of its own
    `ValidationError`, which is the bug this function shipped with once. The two writes that *should*
    be all-or-nothing (the event update and its audit event) are atomic inside
    :func:`_record_confirmation_attempt` instead.

    `fetch` is a seam for tests and nothing else: no caller passes one in production, and nothing here
    reaches a socket the allow-list above did not already accept.
    """
    if event.provider != "sns":
        raise ValidationError("Only an Amazon SNS subscription is confirmed with a click.")
    if event.applied:
        raise ValidationError("That subscription has already been confirmed.")
    url = str((event.raw or {}).get("subscribe_url") or "")
    if not url:
        raise ValidationError("This event did not carry a confirmation address to follow.")
    sns_confirmation_target(url)
    open_url = fetch or _sns_fetch
    arn = ""
    try:
        status, body = open_url(url)
    except Exception as exc:
        reached, note = False, f"Could not reach the SNS endpoint ({type(exc).__name__})"
    else:
        reached = status == 200 and "<ConfirmSubscriptionResponse" in (body or "")
        found = re.search(r"<SubscriptionArn>\s*([^<]+?)\s*</SubscriptionArn>", body or "")
        arn = found.group(1)[:200] if reached and found else ""
        note = "Subscription confirmed" if reached else \
            f"SNS answered {status} without a confirmation result"
    _record_confirmation_attempt(event, actor, confirmed=reached, note=note, subscription_arn=arn)
    if not reached:
        raise ValidationError("Amazon did not confirm this subscription. The usual reason is that the "
                              "token expired — publish a test message and confirm the event that "
                              "arrives, or confirm it in the AWS console.")
    return event


@transaction.atomic
def _record_confirmation_attempt(event, actor, *, confirmed, note, subscription_arn=""):
    """Write what the click did, keeping the token out of the record.

    The `SubscribeURL` embeds a `Token` that is enough to confirm *this* subscription, so the audit
    event names the host and the topic and never the address. The stored event keeps its raw URL — it
    has to, or a retry is impossible — and the messaging page prints the host, not the link.

    This is the only transaction in the confirmation path: the event update and its audit row are
    all-or-nothing together, and neither is rolled back by the refusal the caller raises afterwards.
    """
    event.detail = note[:255]
    event.applied = bool(confirmed)
    if subscription_arn:
        event.raw = {**(event.raw or {}), "subscription_arn": subscription_arn}
    event.save(update_fields=["detail", "applied", "raw"])
    AuditEvent.objects.create(organization=event.organization, actor=actor,
        action="message.subscription_confirmed" if confirmed else "message.subscription_confirm_failed",
        target_type="delivery_event", target_id=str(event.pk),
        metadata={"provider": event.provider, "topic": event.destination,
                  "host": (event.raw or {}).get("subscribe_url_host", ""), "note": note})


def handle_inbound_message(organization, from_number, body, *, provider="twilio", verified=False):
    """Act on a reply to the number itself, and say what came back to it.

    STOP has to work without a login, a screen, or anybody in the office: the carrier's short code is
    the only channel that reaches an officer mid-tour. START is deliberately not its mirror — a stranger
    texting a company's number does not manufacture consent to text them back, so re-in statement needs a
    prior grant on record and otherwise sends them to a supervisor.
    """
    words = " ".join(str(body or "").strip().lower().replace(".", " ").split())
    full = words.strip()
    destination = normalize_destination(from_number, MessageConsent.Channel.SMS)
    if any(word in INBOUND_STOP_WORDS for word in full.split()) or full in INBOUND_STOP_WORDS:
        if destination and current_consent(organization, destination):
            record_consent(organization=organization, destination=destination,
                state=MessageConsent.State.REVOKED, source=MessageConsent.Source.PROVIDER_KEYWORD,
                wording=f"inbound: {str(body)[:120]}", evidence={"provider": provider})
            reply = f"You will not receive more texts from {organization.display_name}. Reply START to opt in again."
        else:
            reply = "You are not set up for texts with us, so nothing was changed."
    elif full in INBOUND_HELP_WORDS:
        reply = sms_consent_wording(organization)
    elif any(word in INBOUND_START_WORDS for word in full.split()) or full in INBOUND_START_WORDS:
        prior = MessageConsent.objects.filter(organization=organization,
            destination=destination, state=MessageConsent.State.GRANTED).exists() if destination else False
        if prior:
            record_consent(organization=organization, destination=destination,
                state=MessageConsent.State.GRANTED, source=MessageConsent.Source.PROVIDER_KEYWORD,
                wording=f"inbound: {str(body)[:120]}", evidence={"provider": provider})
            reply = f"You will get texts from {organization.display_name} again. Reply STOP anytime."
        else:
            reply = ("We have no record of you opting in here. Ask your supervisor to set up text alerts "
                     "for your number.")
    else:
        reply = ("We can only read STOP, START and HELP on this number. Ask your supervisor about "
                 "anything else.")
    DeliveryEvent.objects.create(organization=organization, provider=provider,
        channel=MessageConsent.Channel.SMS, destination=destination or str(from_number),
        kind=DeliveryEvent.Kind.INBOUND, detail=str(body or "")[:255], applied=True, verified=verified,
        raw={"body": str(body or "")[:500], "reply": reply})
    return reply


def twiml_reply(text):
    """The response shape a Twilio messaging webhook expects."""
    escaped = (str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return (f'<?xml version="1.0" encoding="UTF-8"?><Response><Message><Body>{escaped}</Body></Message>'
            f"</Response>")


# Event families whose notices are required operational or security messages. Naming them by prefix
# rather than tagging forty call sites keeps the categorisation in one readable place, and a caller can
# still state `mandatory=` explicitly when a notice is neither.
MANDATORY_EVENT_PREFIXES = ("payroll.", "punch.", "credential.", "document.", "retention.",
                           "membership.invitation", "message.", "organization.", "audit.", "import.")


def event_is_mandatory(event_type):
    return str(event_type or "").startswith(MANDATORY_EVENT_PREFIXES)

