from decimal import Decimal

from .models import PayrollRun, Punch
from .timekeeping import pair_tours


def bank_payroll_rows(leave, start, end):
    from .services import PAYROLL_EXPORT_FIELDS
    allocations = leave.leave_days.filter(starts_at__lt=end, ends_at__gt=start)
    events = leave.organization.punches.filter(person=leave.person,
        review_status=Punch.Review.ACCEPTED).prefetch_related("adjustments")
    tours = pair_tours(events)
    rows = []
    for allocation in allocations:
        first, last = max(start, allocation.starts_at), min(end, allocation.ends_at)
        duration = Decimal(str((allocation.ends_at - allocation.starts_at).total_seconds()))
        before = allocation.hours * Decimal(str((first - allocation.starts_at).total_seconds())) / duration
        after = allocation.hours * Decimal(str((last - allocation.starts_at).total_seconds())) / duration
        approved_hours = after.quantize(Decimal("0.01")) - before.quantize(Decimal("0.01"))
        worked = Decimal("0.00")
        for tour in tours:
            if tour["ends_at"] is None:
                continue
            begin, finish = max(first, tour["starts_at"]), min(last, tour["ends_at"])
            if begin < finish:
                seconds = Decimal(str((finish - begin).total_seconds()))
                for rest in tour["breaks"]:
                    if not rest["paid"] and rest["ends_at"] is not None:
                        a, b = max(begin, rest["starts_at"]), min(finish, rest["ends_at"])
                        if a < b:
                            seconds -= Decimal(str((b - a).total_seconds()))
                worked += seconds / 3600
        hours = max(Decimal("0.00"), approved_hours - worked).quantize(Decimal("0.01"))
        blocker = ""
        if any(tour["starts_at"] < last and (tour["ends_at"] is None or tour["ends_at"] > first)
               and (tour["needs_review"] or any(change.status == "requested"
                    for event in tour["events"] for change in event.adjustments.all())) for tour in tours):
            blocker = "Resolve incomplete or inconsistent worked time overlapping bank leave before payroll approval."
        if leave.organization.payroll_runs.filter(status__in=(PayrollRun.Status.APPROVED, PayrollRun.Status.EXPORTED),
                period_start__lt=last, period_end__gt=first).exclude(period_start=start, period_end=end).exists():
            blocker = "Bank leave overlaps another locked payroll run; reopen/reconcile that run before approval."
        row: dict[str, str | Decimal | int] = {field: "" for field in PAYROLL_EXPORT_FIELDS}
        row.update({
            "employee_id": str(leave.person_id), "employee": leave.person.full_name, "pay_category": "leave",
            "post": "Approved bank leave", "raw_hours": hours, "regular_hours": hours, "overtime_hours": Decimal("0.00"),
            "total_hours": hours, "pay_rate": allocation.pay_rate, "pay_rate_source": "leave approval snapshot",
            "policy_source": "leave approval snapshot", "policy_version": allocation.policy_revision,
            "estimated_pay": (hours * allocation.pay_rate * allocation.multiplier).quantize(Decimal("0.01")),
            "exception": blocker, "note": f"Approved bank leave {allocation.day}: {allocation.hours} confirmed hours; "
                f"{worked.quantize(Decimal('0.01'))} worked hours deducted in this period. "
                "Hours and rate captured at approval; bank reservation is not an elapsed-day payment.",
            "leave_day_id": allocation.pk, "bank_reserved_hours": approved_hours,
        })
        rows.append(row)
    if not rows:
        row = {field: "" for field in PAYROLL_EXPORT_FIELDS}
        row.update(employee_id=str(leave.person_id), employee=leave.person.full_name, pay_category="leave",
                   exception="Approved bank leave has no confirmed day allocations; review this request before payroll.")
        rows.append(row)
    return rows


def settle_run(run, actor):
    from django.core.exceptions import ValidationError
    from django.db.models import Sum
    from django.utils import timezone
    from .leave import _entry, sync_account
    from .models import LeaveAccount, LeaveDay, LeaveUsage

    current = {}
    for item in run.organization.time_off_requests.filter(status="approved", use_leave_bank=True,
            starts_at__lt=run.period_end, ends_at__gt=run.period_start):
        current.update({row.get("leave_day_id"): row for row in bank_payroll_rows(item, run.period_start, run.period_end)})
    saved = {row["leave_day_id"]: row for row in run.snapshot if row.get("leave_day_id")}
    if saved.keys() != current.keys() or any(
            current[key]["exception"] or
            Decimal(str(saved[key]["total_hours"])) != current[key]["total_hours"] or
            Decimal(str(saved[key]["bank_reserved_hours"])) != current[key]["bank_reserved_hours"]
            for key in saved):
        raise ValidationError("Bank leave or overlapping worked time changed; regenerate this payroll draft before approval.")
    for key, row in saved.items():
        day = LeaveDay.objects.select_for_update().get(pk=key, request__organization=run.organization)
        reserved, paid = Decimal(row["bank_reserved_hours"]), Decimal(row["total_hours"])
        already = LeaveUsage.objects.filter(day=day).aggregate(total=Sum("reserved_hours"))["total"] or Decimal("0")
        if reserved + already > day.hours:
            raise ValidationError("Bank leave is already included in another locked period. Reconcile it before approval.")
        LeaveUsage.objects.create(day=day, run=run, reserved_hours=reserved, paid_hours=paid)
        if reserved > paid:
            account = sync_account(LeaveAccount.objects.get(person=day.request.person, organization=run.organization))
            _entry(account, f"settle:{run.pk}:{run.reopen_count}:{day.pk}", "release", reserved - paid,
                   timezone.localdate(), "Unused approved hours returned when payroll was locked", actor, day.request)
        day.consumed = True
        day.save(update_fields=["consumed"])


def undo_settlement(run, actor):
    from collections import defaultdict
    from django.core.exceptions import ValidationError
    from django.utils import timezone
    from .leave import _available, _entry, sync_account
    from .models import LeaveAccount, LeaveUsage

    usages = list(run.leave_usages.select_related("day__request__person"))
    refunds = defaultdict(lambda: Decimal("0"))
    accounts = {}
    for usage in usages:
        person = usage.day.request.person
        refunds[person.pk] += usage.reserved_hours - usage.paid_hours
        accounts[person.pk] = sync_account(LeaveAccount.objects.get(person=person, organization=run.organization))
    for key, refund in refunds.items():
        if refund > _available(accounts[key]):
            raise ValidationError("Previously refunded leave has been spent. HR must restore sufficient available hours "
                                  "with an audited adjustment before this payroll can be reopened.")
    for usage in usages:
        refund = usage.reserved_hours - usage.paid_hours
        if refund:
            _entry(accounts[usage.day.request.person_id],
                   f"unsettle:{run.pk}:{run.reopen_count}:{usage.day_id}", "reserve", -refund,
                   timezone.localdate(), "Payroll reopened; previous unused-hours refund reversed", actor, usage.day.request)
        day = usage.day
        usage.delete()
        day.consumed = LeaveUsage.objects.filter(day=day).exists()
        day.save(update_fields=["consumed"])
