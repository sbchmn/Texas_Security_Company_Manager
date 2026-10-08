"""Audited leave banks; all balance changes serialize on the employee account."""
import calendar
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_DOWN
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import (
    AuditEvent, LeaveAccount, LeaveDay, LeaveEntry, LeavePolicy, LeaveYear, Membership,
    LeaveUsage, Organization, PayCategory, PayrollRun, Person, Shift, TimeOffRequest, TimePolicy,
)

CENT = Decimal("0.01")
HR_ROLES = (Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR)


def lock_bank(organization):
    # Audit writes lock this same row; acquire it before account/request locks.
    Organization.objects.select_for_update().get(pk=organization.pk)


def _hr(organization, actor):
    if not organization.memberships.filter(user=actor, active=True, role__in=HR_ROLES).exists():
        raise PermissionDenied


def _audit(account, actor, action, metadata):
    AuditEvent.objects.create(organization=account.organization, actor=actor, action=action,
                             target_type="leave_account", target_id=str(account.pk), metadata=metadata)


def _entry(account, key, kind, hours, day, reason, actor=None, request=None):
    entry, created = LeaveEntry.objects.get_or_create(account=account, key=key, defaults={
        "kind": kind, "hours": hours, "effective_on": day, "reason": reason, "actor": actor, "request": request,
    })
    if created:
        _audit(account, actor, "leave." + kind, {"hours": str(hours), "date": day.isoformat(),
                                               "reason": reason, "entry": entry.pk})
    return entry


def _available(account):
    return account.entries.aggregate(total=Sum("hours"))["total"] or Decimal("0.00")


def _year(account, year, policy):
    clock = TimePolicy.objects.filter(organization=account.organization).first()
    weekday = clock.workweek_start if clock else 0
    anchor = clock.pay_period_anchor if clock else None
    return LeaveYear.objects.get_or_create(account=account, year=year, defaults={
        "annual_hours": policy.annual_hours, "grant_method": policy.grant_method,
        "carryover_cap": policy.carryover_cap,
        "pay_period_weeks": clock.pay_period_weeks if clock else 1,
        "pay_period_anchor": anchor or date(2000, 1, 3) + timedelta(days=weekday),
        "timezone": clock.timezone if clock else account.organization.timezone,
    })[0]


def _sync(account, through):
    policy = LeavePolicy.objects.filter(organization=account.organization).first()
    if policy is None or account.eligible_from > through:
        return
    for year in range(account.eligible_from.year, through.year + 1):
        terms = _year(account, year, policy)
        first, last = date(year, 1, 1), date(year + 1, 1, 1)
        eligible = max(first, account.eligible_from)
        if year > account.eligible_from.year:
            # Reservations are already debited; only unreserved hours can expire.
            expiry = max(Decimal("0.00"), _available(account) - terms.carryover_cap)
            _entry(account, f"expiry:{year}", "expiry", -expiry, first,
                   f"Year-end available-hours carryover capped at {terms.carryover_cap}")
        days_in_year = Decimal(366 if calendar.isleap(year) else 365)
        if terms.grant_method == LeavePolicy.GrantMethod.ANNUAL:
            amount = (terms.annual_hours * Decimal((last - eligible).days) / days_in_year).quantize(CENT)
            _entry(account, f"grant:{year}", "grant", amount, eligible, "Prorated calendar-year allowance")
        elif terms.grant_method == LeavePolicy.GrantMethod.ACCRUAL:
            length = timedelta(weeks=terms.pay_period_weeks)
            boundary = terms.pay_period_anchor + length * (((eligible - terms.pay_period_anchor).days // length.days) + 1)
            earned_before = Decimal("0.00")
            while boundary <= min(last, through):
                earned = (terms.annual_hours * Decimal((boundary - eligible).days) / days_in_year).quantize(CENT)
                _entry(account, f"accrual:{year}:{boundary}", "accrual", earned - earned_before, boundary,
                       "Eligible days in completed pay periods; annual allowance spread across calendar year")
                earned_before = earned
                boundary += length
            if last <= through:
                # Settle the final year fragment at rollover; never lose or double-credit it.
                earned = (terms.annual_hours * Decimal((last - eligible).days) / days_in_year).quantize(CENT)
                _entry(account, f"accrual:{year}:close", "accrual", earned - earned_before, last,
                       "Calendar-year accrual settlement")


@transaction.atomic
def sync_account(account, through=None):
    lock_bank(account.organization)
    account = LeaveAccount.objects.select_for_update().select_related("organization", "person").get(pk=account.pk)
    clock = TimePolicy.objects.filter(organization=account.organization).first()
    zone = ZoneInfo(clock.timezone if clock else account.organization.timezone)
    through = through or timezone.now().astimezone(zone).date()
    _sync(account, through)
    return account


def balance(account):
    account = sync_account(account)
    confirmed = LeaveDay.objects.filter(request__person=account.person, request__status="approved").aggregate(
        total=Sum("hours"))["total"] or Decimal("0.00")
    used = LeaveUsage.objects.filter(day__request__person=account.person).aggregate(
        reserved=Sum("reserved_hours"), paid=Sum("paid_hours"))
    credits = LeaveEntry.objects.filter(account=account, kind__in=("grant", "accrual", "adjustment")).aggregate(total=Sum("hours"))["total"] or Decimal("0.00")
    return {"available": _available(account), "reserved": confirmed - (used["reserved"] or Decimal("0")),
            "credited": credits, "used": used["paid"] or Decimal("0"),
            "account": account, "entries": LeaveEntry.objects.filter(account=account).select_related("actor")[:12]}


@transaction.atomic
def enroll(person, eligible_from, actor):
    _hr(person.organization, actor)
    lock_bank(person.organization)
    Person.objects.select_for_update().get(pk=person.pk)
    policy = LeavePolicy.objects.filter(organization=person.organization, enabled=True).first()
    if not policy:
        raise ValidationError("Enable the company leave bank before enrolling employees.")
    if eligible_from < date(2000, 1, 1):
        raise ValidationError("Eligibility cannot start before January 1, 2000.")
    account, created = LeaveAccount.objects.get_or_create(person=person, defaults={
        "organization": person.organization, "eligible_from": eligible_from, "enrolled_by": actor,
    })
    if not created:
        raise ValidationError("This employee is already enrolled. Use an audited adjustment; enrollment dates cannot be rewritten.")
    _audit(account, actor, "leave.enrolled", {"eligible_from": eligible_from.isoformat()})
    return sync_account(account)


@transaction.atomic
def adjust(account, hours, reason, actor):
    _hr(account.organization, actor)
    account = sync_account(account)
    if not hours.is_finite() or hours == 0 or hours != hours.quantize(CENT):
        raise ValidationError("Enter a nonzero hour adjustment with at most two decimal places.")
    if len(reason.strip()) < 5 or len(reason.strip()) > 255:
        raise ValidationError("Give an adjustment reason of 5-255 characters.")
    if _available(account) + hours < 0:
        raise ValidationError("An adjustment cannot reduce available hours below zero.")
    return _entry(account, f"adjustment:{uuid.uuid4()}", "adjustment", hours,
                  timezone.localdate(), reason.strip(), actor)


def request_days(item):
    if item.ends_at <= item.starts_at:
        raise ValidationError("Leave must end after it starts.")
    clock = TimePolicy.objects.filter(organization=item.organization).first()
    zone = ZoneInfo(clock.timezone if clock else item.organization.timezone)
    first = item.starts_at.astimezone(zone).date()
    last = (item.ends_at - timedelta(microseconds=1)).astimezone(zone).date()
    if (last - first).days > 365:
        raise ValidationError("Bank leave requests must span at most 366 calendar days.")
    for offset in range((last - first).days + 1):
        day = first + timedelta(days=offset)
        start = timezone.make_aware(datetime.combine(day, time.min), zone)
        end = timezone.make_aware(datetime.combine(day + timedelta(days=1), time.min), zone)
        yield day, max(start, item.starts_at), min(end, item.ends_at)


def _locked_absence(item):
    from .services import payroll_lock_state
    for run in item.organization.payroll_runs.filter(period_start__lt=item.ends_at, period_end__gt=item.starts_at):
        if run.status in ("approved", "exported"):
            return True
        at = max(run.period_start, item.starts_at)
        if payroll_lock_state(item.organization, at)["locked"]:
            return True
        for shift in item.organization.shifts.filter(officer=item.person, status="published",
                starts_at__lt=min(run.period_end, item.ends_at),
                ends_at__gt=max(run.period_start, item.starts_at)).select_related("site"):
            if payroll_lock_state(item.organization, at, shift.site.branch_id, shift.site.client_id)["locked"]:
                return True
    return False


def suggestion(item):
    policy = LeavePolicy.objects.filter(organization=item.organization, enabled=True).first()
    if not policy:
        raise ValidationError("The company leave bank is disabled.")
    days = list(request_days(item))
    shifts = item.organization.shifts.filter(officer=item.person, status=Shift.Status.PUBLISHED,
                                             starts_at__lt=item.ends_at, ends_at__gt=item.starts_at)
    hours = sum((Decimal(str((min(shift.ends_at, item.ends_at) - max(shift.starts_at, item.starts_at)).total_seconds())) / 3600
                 for shift in shifts), Decimal("0.00"))
    return (hours if hours else policy.daily_hours * len(days)).quantize(CENT)


@transaction.atomic
def reserve(item, hours, actor):
    from .services import effective_rates, payroll_lock_state
    membership = item.organization.memberships.filter(user=actor, active=True).first()
    from .scope import for_membership
    if not membership or membership.role not in (*HR_ROLES, Membership.Role.SCHEDULER, Membership.Role.SUPERVISOR):
        raise PermissionDenied
    if not for_membership(membership).permits_person(item.person):
        raise PermissionDenied
    lock_bank(item.organization)
    stored = TimeOffRequest.objects.select_for_update().get(pk=item.pk, organization=item.organization)
    if stored.status != TimeOffRequest.Status.REQUESTED or not stored.use_leave_bank:
        raise ValidationError("Only a pending bank request can reserve hours.")
    account = LeaveAccount.objects.filter(person=item.person, organization=item.organization).first()
    if account is None:
        raise ValidationError("HR must enroll this employee in the leave bank before approving bank hours.")
    account = sync_account(account)
    policy = LeavePolicy.objects.filter(organization=item.organization, enabled=True).first()
    category = item.organization.pay_categories.filter(kind=PayCategory.Kind.LEAVE, active=True).first()
    if not policy or not category or not category.paid:
        raise ValidationError("Enable the leave bank and configure a paid leave hour category before approving bank leave.")
    if hours is None or not hours.is_finite() or hours <= 0 or hours != hours.quantize(CENT):
        raise ValidationError("Confirm positive leave hours with at most two decimal places.")
    days = list(request_days(item))
    if days[0][0] < account.eligible_from:
        raise ValidationError("Leave cannot precede the employee's eligibility date.")
    if hours > _available(account):
        raise ValidationError(f"Insufficient available leave: { _available(account):.2f} hours available, {hours:.2f} requested.")
    if item.organization.time_off_requests.filter(person=item.person, status="approved", use_leave_bank=True,
            starts_at__lt=item.ends_at, ends_at__gt=item.starts_at).exclude(pk=item.pk).exists():
        raise ValidationError("This absence overlaps another approved bank request. Resolve that request first.")
    if _locked_absence(item):
        raise ValidationError("This absence overlaps a locked payroll period.")
    allocations = []
    weights = []
    for day, start, end in days:
        weights.append(sum((Decimal(str((min(shift.ends_at, end) - max(shift.starts_at, start)).total_seconds()))
            for shift in item.organization.shifts.filter(officer=item.person, status=Shift.Status.PUBLISHED,
                starts_at__lt=end, ends_at__gt=start)), Decimal("0")))
    if not sum(weights):
        weights = [Decimal("1")] * len(days)
    exact_cents = [hours * 100 * weight / sum(weights) for weight in weights]
    amounts = [int(value.to_integral_value(rounding=ROUND_DOWN)) for value in exact_cents]
    remainder = int(hours * 100) - sum(amounts)
    for index in sorted(range(len(days)), key=lambda i: exact_cents[i] - amounts[i], reverse=True)[:remainder]:
        amounts[index] += 1
    for index, (day, start, end) in enumerate(days):
        if payroll_lock_state(item.organization, start)["locked"] or payroll_lock_state(
                item.organization, end - timedelta(microseconds=1))["locked"]:
            raise ValidationError("This absence overlaps a locked payroll period.")
        amount = Decimal(amounts[index]) / 100
        if amount == 0:
            continue
        if amount > Decimal("24"):
            raise ValidationError("Confirmed leave cannot exceed 24 hours per calendar day.")
        rates: list[tuple[Decimal, Decimal]] = []
        for shift in item.organization.shifts.filter(officer=item.person, status=Shift.Status.PUBLISHED,
                                                     starts_at__lt=end, ends_at__gt=start).select_related("site__client", "officer"):
            rate = effective_rates(shift, officer=item.person)["pay_rate"]
            if payroll_lock_state(item.organization, start, shift.site.branch_id, shift.site.client_id)["locked"]:
                raise ValidationError("This absence overlaps a locked payroll slice.")
            if rate is None:
                raise ValidationError("A displaced shift is missing a pay rate.")
            weight = Decimal(str((min(end, shift.ends_at) - max(start, shift.starts_at)).total_seconds()))
            rates.append((weight, Decimal(rate)))
        rate = ((sum((weight * value for weight, value in rates), Decimal("0")) /
                 sum((weight for weight, _ in rates), Decimal("0"))).quantize(CENT)
                if rates else item.person.hourly_rate if item.person.hourly_rate is not None else policy.fallback_pay_rate)
        if rate is None:
            raise ValidationError("Set the employee hourly rate or the company leave-rate fallback before approval.")
        allocations.append(LeaveDay(request=item, day=day, starts_at=start, ends_at=end, hours=amount,
                                    pay_rate=rate, multiplier=category.multiplier, policy_revision=category.revision))
    if item.bank_entries.exists():
        raise ValidationError("This request already has a bank reservation.")
    LeaveDay.objects.bulk_create(allocations)
    _entry(account, f"reserve:{item.pk}", "reserve", -hours, timezone.localdate(),
           "Reviewer-confirmed leave hours reserved at approval", actor, item)
    item.confirmed_leave_hours = hours


@transaction.atomic
def cancel_approved(item, reason, actor):
    from .services import payroll_lock_state
    _hr(item.organization, actor)
    lock_bank(item.organization)
    item = TimeOffRequest.objects.select_for_update().select_related("person").get(pk=item.pk)
    if item.status != TimeOffRequest.Status.APPROVED:
        raise ValidationError("Only approved requests can be cancelled here.")
    if len(reason.strip()) < 5 or len(reason.strip()) > 255:
        raise ValidationError("Give a cancellation reason of 5-255 characters.")
    if _locked_absence(item):
        raise ValidationError("Leave overlaps locked payroll. Reopen payroll first.")
    for allocation in LeaveDay.objects.filter(request=item):
        if allocation.consumed or payroll_lock_state(item.organization, allocation.starts_at)["locked"]:
            raise ValidationError("Leave already included in locked payroll cannot be cancelled. Reopen payroll first.")
    if item.use_leave_bank:
        account = sync_account(LeaveAccount.objects.get(person=item.person, organization=item.organization))
        outstanding = -(LeaveEntry.objects.filter(request=item).aggregate(total=Sum("hours"))["total"] or Decimal("0"))
        _entry(account, f"release:{item.pk}", "release", outstanding, timezone.localdate(),
               reason.strip(), actor, item)
    item.status = TimeOffRequest.Status.CANCELLED
    item.review_note = reason.strip()
    item.decided_by = actor
    item.decided_at = timezone.now()
    item.save(update_fields=["status", "review_note", "decided_by", "decided_at"])
    AuditEvent.objects.create(organization=item.organization, actor=actor, action="timeoff.cancelled",
                             target_type="time_off_request", target_id=str(item.pk), metadata={"reason": reason})
    from .services import queue_notice
    if item.person.user is not None:
        queue_notice(organization=item.organization, recipients={item.person.user.pk},
                     event_type="timeoff.decided", subject="Approved time off cancelled",
                     body=f"Your approved absence was cancelled. Reason: {reason.strip()}",
                     dedup_key=f"timeoff-approved-cancelled:{item.pk}",
                     sms={"officer": item.person, "dates": (item.starts_at, item.ends_at),
                          "note": reason.strip(), "decision": "cancelled"})
    for run in PayrollRun.objects.filter(organization=item.organization, status="draft", period_start__lt=item.ends_at,
                                                    period_end__gt=item.starts_at):
        run.exceptions = [*run.exceptions, {"employee": item.person.full_name, "reason": "Leave changed; regenerate this draft before approval."}]
        run.save(update_fields=["exceptions"])
    return item
