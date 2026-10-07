"""Workspace-first landing pages organized around user jobs.

Each workspace provides a role-specific entry point surfacing the most urgent work first.
"""
from datetime import timedelta
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from .auth import membership_required
from .models import (
    Person, Shift, Punch, ShiftClaim, ShiftSwap, ShiftExchange, OnboardingItem,
    ComplianceRule, TimeOffRequest,
)
from .scope import scope_for
from .services import onboarding_board, onboarding_progress, schedule_week_start, current_consent, sms_consent_wording
from . import views
from .workflow_actions import onboarding_actions, compliance_actions, setup_readiness
from .time_workflow import pending_time_review_counts


def _scheduling_actions(organization, scope):
    """Outstanding requests, using the same authority boundaries as their decision pages."""
    claims = list(organization.shift_claims.filter(
        status=ShiftClaim.Status.REQUESTED,
        shift__in=scope.filter_shifts(organization.shifts.all()),
    ).select_related("shift__site__client", "officer"))
    swaps = views._open_swaps(organization, scope)
    exchanges = views._open_exchanges(organization, scope)
    leave = list(scope.filter_by_person(organization.time_off_requests.filter(
        status=TimeOffRequest.Status.REQUESTED,
    )).select_related("person"))
    rows = []
    for claim in claims:
        shift = claim.shift
        rows.append({
            "kind": "Open-post request", "person": str(claim.officer),
            "details": str(shift.site), "note": claim.note,
            "starts_at": shift.starts_at, "ends_at": shift.ends_at,
            "requested_at": claim.created_at, "waiting": False,
            "state": "Awaiting your review",
            "url": reverse("shift_requests", args=[shift.pk]),
        })
    for swap in swaps:
        rows.append({
            "kind": "Hand-off", "person": f"{swap.requester} to {swap.replacement}",
            "details": str(swap.shift.site), "note": swap.note,
            "starts_at": swap.shift.starts_at, "ends_at": swap.shift.ends_at,
            "requested_at": swap.created_at, "waiting": swap.status == ShiftSwap.Status.OFFERED,
            "state": ShiftSwap.Status(swap.status).label, "url": reverse("swaps") + f"?swap={swap.pk}",
        })
    for exchange in exchanges:
        rows.append({
            "kind": "Trade", "person": f"{exchange.initiator} with {exchange.partner}",
            "details": str(exchange.initiator_shift.site), "note": exchange.note,
            "starts_at": exchange.initiator_shift.starts_at, "ends_at": exchange.initiator_shift.ends_at,
            "requested_at": exchange.created_at, "waiting": exchange.status == ShiftExchange.Status.PROPOSED,
            "state": ShiftExchange.Status(exchange.status).label, "url": reverse("swaps") + f"?exchange={exchange.pk}",
        })
    for item in leave:
        rows.append({
            "kind": "Time off", "person": str(item.person),
            "details": "Check affected posts before deciding", "note": item.reason,
            "starts_at": item.starts_at, "ends_at": item.ends_at,
            "requested_at": item.created_at, "waiting": False,
            "state": "Awaiting your review", "url": reverse("time_off") + f"?request={item.pk}",
        })
    rows.sort(key=lambda row: (row["waiting"], row["starts_at"], row["requested_at"], row["url"]))
    waiting_count = sum(row["waiting"] for row in rows)
    return {
        "rows": rows, "claim_count": len(claims), "move_count": len(swaps) + len(exchanges),
        "leave_count": len(leave), "waiting_count": waiting_count,
        "review_count": len(rows) - waiting_count, "total_count": len(rows),
    }


@membership_required()
@require_GET
def workspace_today(request):
    """Employee-focused: what needs action now.
    
    Surfaces clock, next shift, pending tasks, and outstanding documents.
    """
    org = request.organization
    person = org.people.select_related("branch").filter(user=request.user).first()
    today = timezone.localdate()
    now = timezone.now()
    
    # Manager sees different content from their personal clock/onboarding page
    manager = request.membership.role in views.MANAGERS
    
    role = request.membership.role
    context = {
        "person": person, "manager": manager, "today": today,
        "can_review": role in views.TIME_REVIEWERS,
        # Audit is company-level; a branch-bounded supervisor cannot open /audit/, so Today must not read it for them.
        "recent_events": (org.audit_events.select_related("actor")[:6]
                          if manager and role in views.AUDIT_READERS else []),
        "text_prompt": None,
    }
    # NTF-4's first-login capture: asked once, only when a number is on file with no decision against it.
    # An imported phone list carries no consent, and either answer makes the prompt disappear for good.
    if person is not None and person.mobile_phone and current_consent(org, person.mobile_phone) is None:
        context["text_prompt"] = {"phone": person.mobile_phone, "wording": sms_consent_wording(org)}
    scope = scope_for(request)
    priorities = []
    
    if manager:
        # Manager "Today" workspace: open posts needing coverage, pending punches to review
        scope = scope_for(request)
        context["scheduling_actions"] = _scheduling_actions(org, scope)
        scheduling = context["scheduling_actions"]
        if scheduling["review_count"]:
            review_rows = [row for row in scheduling["rows"] if not row["waiting"]]
            priorities.append({
                "label": "Scheduling decisions", "count": len(review_rows), "owner": "Dispatcher / supervisor",
                "oldest": min(row["requested_at"] for row in review_rows),
                "past_date": sum(row["starts_at"] <= now for row in review_rows),
                "url": reverse("workspace_schedule") + "#schedule-actions",
                "next_step": "Review eligibility, consent, and coverage before deciding",
            })
        onboarding = onboarding_actions(request)
        if onboarding["total"]:
            priorities.append({
                "label": "Outstanding onboarding", "count": onboarding["total"], "owner": "Office and employee",
                "oldest": min((row["created_at"] for row in onboarding["rows"] if row["created_at"]), default=None),
                "past_date": onboarding["overdue"],
                "url": reverse("workspace_people") + "#onboarding-actions",
                "next_step": f'{onboarding["overdue"]} overdue; issue missing steps or resolve evidence',
            })
        context.update({
            "open_posts": scope.filter_shifts(org.shifts.filter(
                officer__isnull=True,
                status__in=[Shift.Status.DRAFT, Shift.Status.PUBLISHED],
                ends_at__gte=now,
                starts_at__lt=now + timedelta(days=7)
            ).select_related("site__client")).order_by("starts_at")[:10],
            
            "pending_move_count": context["scheduling_actions"]["move_count"],
        })
        attendance = views.compliance_attendance(org, scope, reader=views._record_reader(request))
        context["compliance_attention"] = sum(
            value["attention"] for name, value in attendance.items()
            if name != "documents" or role in views.RECORD_READERS
        )
        if context["compliance_attention"]:
            priorities.append({
                "label": "Compliance obligations", "count": context["compliance_attention"], "owner": "Office / personnel-record staff",
                "url": reverse("compliance"), "next_step": "Resolve visible evidence gaps before assigning posts",
            })
        if role in views.RECORD_WRITERS:
            signing = onboarding["signing_counts"]
            signing_attention = signing["ready"] + signing["failed"] + signing["processing"]
            if signing_attention:
                priorities.append({
                    "label": "Signing operations", "count": signing_attention, "owner": "Personnel-record staff",
                    "url": reverse("signing_queue"), "next_step": "Send, reconcile, or correct requests; never resend an uncertain submission",
                })
        if role in views.PRIVILEGED:
            dispositions = org.disposition_requests.filter(status="pending").exclude(requested_by=request.user)
            if dispositions.exists():
                priorities.append({
                    "label": "Retention decisions", "count": dispositions.count(), "owner": "Second owner / administrator",
                    "oldest": dispositions.order_by("created_at").first().created_at,
                    "url": reverse("retention_review"), "next_step": "Review holds before authorizing the requested disposition",
                })
        context["setup_checks"] = setup_readiness(request)

    from .attendance import scoped_cases
    attendance_cases = (scoped_cases(org, scope) if role in views.MANAGERS
                        else org.attendance_cases.filter(person__user=request.user))
    live_cases = attendance_cases.filter(status="open")
    if live_cases.exists():
        priorities.append({
            "label": "Live attendance", "count": live_cases.count(),
            "owner": "Dispatch / supervisor" if role in views.MANAGERS else "Your dispatch / supervisor",
            "oldest": live_cases.first().opened_at, "url": reverse("attendance_queue"),
            "next_step": "Confirm arrival, departure, and relief; missing punch evidence is not proof of absence",
        })
    if context["can_review"]:
        time_counts = pending_time_review_counts(org, scope)
        context["pending_review_count"] = time_counts["total"]
        context["pending_punch_count"] = time_counts["punches"]
        context["pending_correction_count"] = time_counts["corrections"]
        if time_counts["total"]:
            priorities.append({
                "label": "Time review", "count": time_counts["total"], "owner": "Payroll / supervisor",
                "oldest": time_counts["oldest"], "url": reverse("time_review"),
                "next_step": (
                    f'{time_counts["punches"]} punch{"es" if time_counts["punches"] != 1 else ""} and '
                    f'{time_counts["corrections"]} correction{"s" if time_counts["corrections"] != 1 else ""} awaiting review'
                ),
            })
    if role in views.PAYROLL:
        drafts = org.payroll_runs.filter(status="draft")
        if drafts.exists():
            priorities.append({
                "label": "Payroll drafts", "count": drafts.count(), "owner": "Payroll",
                "oldest": drafts.order_by("created_at").first().created_at,
                "url": reverse("workspace_payroll") + "#payroll-drafts",
                "next_step": "Select a period, resolve blockers, and review its snapshot before approval",
            })

    # Managers with a personnel record still need their own clock and checklist.
    if person:
        board = onboarding_board(org, person, today=today, reader=views._record_reader(request))
        context.update({
            "next_shift": person.shifts.filter(
                status=Shift.Status.PUBLISHED, ends_at__gte=now
            ).select_related("site__client").order_by("starts_at").first(),
            "pending_onboarding": len(board["outstanding"]),
            "onboarding_overdue": len(board["overdue"]),
            "unissued_onboarding": board["unissued"],
            "pending_signatures": sum(row["item"].kind == OnboardingItem.Kind.SIGNATURE
                                      for row in board["outstanding"]),
            "open_offers": (person.swaps_received.filter(status=ShiftSwap.Status.OFFERED).count()
                            + person.exchanges_invited.filter(status=ShiftExchange.Status.PROPOSED).count()),
        })
    
    priorities.sort(key=lambda row: (not row.get("past_date"), row.get("oldest") or now, row["label"]))
    context["work_priorities"] = priorities
    return render(request, "core/workspaces/today.html", context)


@membership_required(*views.MANAGERS)
@require_GET
def workspace_people(request):
    """HR-focused: people management and onboarding.
    
    Shows new hires, outstanding onboarding, credentials, training needs.
    """
    org = request.organization
    scope = scope_for(request)
    
    people = scope.filter_people(org.people.select_related("branch")).order_by("last_name", "first_name")
    
    # Count status groups
    onboarding = people.filter(status=Person.Status.ONBOARDING)
    active = people.filter(status=Person.Status.ACTIVE)
    progress = onboarding_progress(org, scope)
    actions = onboarding_actions(request)
    category = request.GET.get("work", "all")
    if category not in ("all", "office", "employee", "blocked", "unissued", "overdue"):
        raise views.Http404
    filtered = actions["rows"]
    if category == "overdue":
        filtered = [row for row in filtered if row["overdue"]]
    elif category in ("office", "employee"):
        filtered = [row for row in filtered if (row["owner"] == "Employee") == (category == "employee")]
    elif category != "all":
        filtered = [row for row in filtered if row["category"] == category]
    
    context = {
        "people_count": people.count(),
        "onboarding_count": onboarding.count(),
        "active_count": active.count(),
        "overdue_count": sum(item["overdue"] for item in progress.values()),
        
        "onboarding_list": onboarding[:20],
        "active_list": active[:20],
        
        "people": people[:50],
        "scope": scope,
        "onboarding_actions": actions,
        "onboarding_queue": Paginator(filtered, 25).get_page(request.GET.get("page")),
        "onboarding_filter": category, "show_onboarding_filters": True,
        "can_signing": request.membership.role in views.RECORD_WRITERS,
    }
    
    return render(request, "core/workspaces/people.html", context)


@membership_required(*views.MANAGERS)
@require_GET
def workspace_schedule(request):
    """Dispatcher-focused: coverage and scheduling.
    
    Shows coverage gaps, open posts, recurring series, and upcoming changes.
    """
    org = request.organization
    scope = scope_for(request)
    now = timezone.now()
    
    # Week view: default to current week
    week_start = schedule_week_start(now, organization=org)
    week_end = week_start + timedelta(days=7)
    
    shifts = scope.filter_shifts(org.shifts.filter(
        starts_at__gte=week_start,
        starts_at__lt=week_end
    ).select_related("site__client", "officer")).order_by("starts_at")
    
    open_posts = scope.filter_shifts(org.shifts.filter(
        officer__isnull=True,
        status__in=[Shift.Status.DRAFT, Shift.Status.PUBLISHED],
        ends_at__gte=now,
        starts_at__lt=week_end,
    ).select_related("site__client")).order_by("starts_at")
    templates = org.shift_templates.filter(active=True)
    if scope.restricted:
        templates = templates.filter(Q(site_id__in=scope.site_ids) | Q(officer_id__in=scope.person_ids))
    actions = _scheduling_actions(org, scope)
    from .services import coverage_report
    coverage = coverage_report(org, scope, now, week_end)
    
    context = {
        "week_start": week_start,
        "week_end": week_end,
        
        "shifts": shifts,
        "open_posts_count": open_posts.count(),
        "open_posts": open_posts[:15],
        "recurring_series_count": templates.count(),
        "scheduling_actions": actions,
        "attendance_count": _attendance_count(org, scope),
        "action_queue": Paginator(actions["rows"], 10).get_page(request.GET.get("page")),
        "now": now,
        "authority_scope": scope if scope.restricted else None,
        "draft_count": shifts.filter(status=Shift.Status.DRAFT).count(),
        "published_open_count": open_posts.filter(status=Shift.Status.PUBLISHED).count(),
        "at_risk": coverage["at_risk"],
    }
    
    return render(request, "core/workspaces/schedule.html", context)


def _attendance_count(org, scope):
    from .attendance import scoped_cases
    return scoped_cases(org, scope).filter(status="open").count()


@membership_required(*views.TIME_REVIEWERS)
@require_GET
def workspace_payroll(request):
    """Payroll processor and time reviewer: period management and exceptions.
    
    Shows pending reviews, open payroll periods, exceptions, and lock state.
    """
    org = request.organization
    scope = scope_for(request)
    
    # Pending punches needing review
    pending_punches = scope.filter_punches(
        org.punches.filter(review_status=Punch.Review.PENDING)
    ).select_related("person").order_by("-occurred_at")[:20]
    
    # Recent payroll runs
    can_payroll = request.membership.role in views.PAYROLL
    runs = org.payroll_runs.select_related("approved_by").order_by("-period_start")[:10] if can_payroll else []
    time_counts = pending_time_review_counts(org, scope)
    draft_runs = org.payroll_runs.filter(status="draft").order_by("created_at", "pk") if can_payroll else []
    
    context = {
        "pending_punches": pending_punches,
        "pending_count": time_counts["total"],
        "pending_punch_count": time_counts["punches"],
        "pending_correction_count": time_counts["corrections"],
        "draft_queue": Paginator(draft_runs, 10).get_page(request.GET.get("draft_page")),
        
        "latest_run": runs[0] if runs else None,
        "runs": runs,
        "can_payroll": can_payroll,
        "can_configure": request.membership.role in views.PRIVILEGED,
        "can_manage_kiosks": request.membership.role in views.MANAGERS,
        "can_approve": can_payroll,
        "can_review": request.membership.role in views.TIME_REVIEWERS,
    }
    
    return render(request, "core/workspaces/payroll.html", context)


@membership_required(*views.RECORD_READERS)
@require_GET
def workspace_compliance(request):
    """Compliance officer and HR: obligations, evidence, and records.
    
    Shows compliance gaps, outstanding onboarding, documents, training, and retention.
    """
    org = request.organization
    scope = scope_for(request)
    
    # Compliance rules count
    active_rules = ComplianceRule.objects.filter(
        organization=org, active=True
    ).count()
    
    context = {
        "rule_count": active_rules,
        "can_edit": request.membership.role in views.RECORD_WRITERS,
        "can_manage": request.membership.role in views.MANAGERS,
        "can_execute_disposition": request.membership.role in views.PRIVILEGED,
    }
    if context["can_manage"]:
        attendance = views.compliance_attendance(org, scope, reader=views._record_reader(request))
        context["total_attention"] = sum(value["attention"] for value in attendance.values())
        progress = onboarding_progress(org, scope)
        context["onboarding_total"] = sum(item["open"] for item in progress.values())
        context["onboarding_overdue"] = sum(item["overdue"] for item in progress.values())
        actions = onboarding_actions(request)
        context["onboarding_actions"] = actions
        context["onboarding_queue"] = Paginator(actions["rows"], 25).get_page(request.GET.get("page"))
        context["compliance_actions"] = compliance_actions(request, attendance)[:20]
        context["can_signing"] = request.membership.role in views.RECORD_WRITERS
    
    return render(request, "core/workspaces/compliance.html", context)


@membership_required(*views.MANAGERS)
@require_GET
def workspace_reports(request):
    """Management: reports and audit.
    
    Shows saved reports, recent snapshots, audit log, and custom report builder.
    """
    org = request.organization
    
    # Audit log preview - only fetch if user has audit permissions
    audit_events = []
    if request.membership.role in views.AUDIT_READERS:
        audit_events = org.audit_events.select_related("actor").order_by("-occurred_at")[:20]
    
    context = {
        "can_capture": request.membership.role in views.PRIVILEGED,
        "audit_events": audit_events,
        "can_audit": request.membership.role in views.AUDIT_READERS,
    }
    from .services import coverage_report, tour_completion
    now = timezone.now()
    scope = scope_for(request)
    attendance = views.compliance_attendance(org, scope, reader=views._record_reader(request))
    context["compliance_actions"] = compliance_actions(request, attendance)[:10]
    context["coverage"] = coverage_report(org, scope, now, now + timedelta(days=7))
    context["tour"] = tour_completion(org, scope, now - timedelta(days=7), now)
    
    return render(request, "core/workspaces/reports.html", context)
