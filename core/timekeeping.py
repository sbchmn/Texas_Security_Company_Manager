"""Shared evidence pairing for timesheets and payroll."""
from collections import defaultdict
from decimal import Decimal

from .models import PayCategory, Punch

BREAK_KINDS = (Punch.Kind.BREAK_START, Punch.Kind.BREAK_END)


def effective_time(punch):
    approved = sorted(
        (item for item in punch.adjustments.all() if item.status == "approved"),
        key=lambda item: (item.reviewed_at or item.created_at, str(item.pk)), reverse=True,
    )
    return approved[0].proposed_at if approved else punch.occurred_at


def pair_tours(events):
    groups = defaultdict(list)
    for event in events:
        if event.review_status != Punch.Review.REJECTED and event.kind != Punch.Kind.CHECKPOINT:
            groups[event.person_id].append(event)
    tours = []
    for rows in groups.values():
        current = None
        active_break = None
        for event in sorted(rows, key=lambda row: (effective_time(row), row.received_at, str(row.pk))):
            at = effective_time(event)
            if event.kind == Punch.Kind.IN:
                if current:
                    current["issues"].append("Clock-in without closing the previous tour.")
                current = {"start": event, "end": None, "starts_at": at, "ends_at": None,
                           "breaks": [], "issues": [], "events": [event]}
                tours.append(current)
                active_break = None
            elif current is None:
                tours.append({"start": event, "end": None, "starts_at": at, "ends_at": None,
                              "breaks": [], "issues": ["Event has no matching clock-in."], "events": [event]})
            else:
                current["events"].append(event)
                if event.kind in BREAK_KINDS and event.shift_id != current["start"].shift_id:
                    current["issues"].append("Break punch belongs to a different shift from its clock-in.")
                    continue
                if event.kind == Punch.Kind.BREAK_START:
                    if active_break:
                        current["issues"].append("Another break started before the previous break ended.")
                    else:
                        active_break = {"start": event, "end": None, "starts_at": at,
                                        "ends_at": None, "paid": event.break_paid}
                        current["breaks"].append(active_break)
                elif event.kind == Punch.Kind.BREAK_END:
                    if active_break is None:
                        current["issues"].append("Break end has no matching break start.")
                    else:
                        active_break["end"], active_break["ends_at"] = event, at
                        active_break = None
                elif event.kind == Punch.Kind.OUT:
                    if event.shift_id is not None and event.shift_id != current["start"].shift_id:
                        current["issues"].append("Clock-out belongs to a different shift from its clock-in.")
                    current["end"], current["ends_at"] = event, at
                    if active_break:
                        current["issues"].append("Clock-out recorded before the break ended.")
                    current = None
                    active_break = None
        for tour in tours:
            for period in tour["breaks"]:
                if period["end"] is None and "Break has no end punch." not in tour["issues"]:
                    tour["issues"].append("Break has no end punch.")
    for tour in tours:
        tour["break_minutes"] = sum(
            Decimal(str((period["ends_at"] - period["starts_at"]).total_seconds())) / 60
            for period in tour["breaks"] if period["ends_at"] is not None
        )
        tour["unpaid_minutes"] = sum(
            Decimal(str((period["ends_at"] - period["starts_at"]).total_seconds())) / 60
            for period in tour["breaks"] if period["ends_at"] is not None and not period["paid"]
        )
        tour["elapsed_minutes"] = (
            Decimal(str((tour["ends_at"] - tour["starts_at"]).total_seconds())) / 60
            if tour["ends_at"] is not None else None
        )
        tour["paid_minutes"] = (
            tour["elapsed_minutes"] - tour["unpaid_minutes"] if tour["elapsed_minutes"] is not None else None
        )
        tour["needs_review"] = bool(tour["issues"] or tour["end"] is None or
                                   any(row.review_status == Punch.Review.PENDING for row in tour["events"]))
    return sorted(tours, key=lambda row: row["starts_at"], reverse=True)


def break_conflicts(organization, events):
    shifts = {row.shift_id for row in events if row.kind in BREAK_KINDS and row.shift_id
              and row.review_status != Punch.Review.REJECTED}
    return set(organization.hour_designations.filter(
        shift_id__in=shifts, category__kind=PayCategory.Kind.BREAK,
    ).values_list("shift_id", flat=True))


def invalidate_drafts(punch):
    from .models import PayrollRun
    from django.db.models import Q
    dates = {punch.occurred_at, *[item.proposed_at for item in punch.adjustments.filter(status="approved")]}
    periods = Q(pk__in=[])
    for at in dates:
        periods |= Q(period_start__lte=at, period_end__gt=at)
    runs = punch.organization.payroll_runs.filter(periods, status=PayrollRun.Status.DRAFT)
    for run in runs:
        notice = {"employee": punch.person.full_name, "reason": "Time evidence changed; regenerate this payroll draft before approval."}
        if notice not in run.exceptions:
            run.exceptions = [*run.exceptions, notice]
            run.save(update_fields=["exceptions"])
