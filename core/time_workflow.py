"""Shared query helpers for time-review queues."""

from django.db.models import Min

from .models import Punch, PunchAdjustment


def pending_time_review_counts(organization, scope=None, *, run=None):
    """Return organization- and optional authority-scoped pending queue counts."""
    pending_punches = organization.punches.filter(review_status=Punch.Review.PENDING)
    pending_corrections = organization.punch_adjustments.filter(
        status=PunchAdjustment.Status.REQUESTED
    )
    if scope is not None:
        pending_punches = scope.filter_punches(pending_punches)
        pending_corrections = scope.filter_adjustments(pending_corrections)
    if run is not None:
        pending_punches = pending_punches.filter(
            occurred_at__gte=run.period_start, occurred_at__lt=run.period_end,
        )
        pending_corrections = pending_corrections.filter(
            punch__occurred_at__gte=run.period_start, punch__occurred_at__lt=run.period_end,
        )
    punch_count = pending_punches.count()
    correction_count = pending_corrections.count()
    dates = [
        pending_punches.aggregate(oldest=Min("received_at"))["oldest"],
        pending_corrections.aggregate(oldest=Min("created_at"))["oldest"],
    ]
    return {
        "punches": punch_count,
        "corrections": correction_count,
        "total": punch_count + correction_count,
        "oldest": min((date for date in dates if date), default=None),
    }
