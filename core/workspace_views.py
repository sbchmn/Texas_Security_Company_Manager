"""Workspace-first landing pages organized around user jobs.

Each workspace provides a role-specific entry point surfacing the most urgent work first.
"""
from datetime import timedelta
from django.db.models import Q, Count
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from .auth import membership_required
from .models import (
    Person, Shift, Punch, ShiftSwap, ShiftExchange, OnboardingTask,
    ComplianceRule, AuditEvent
)
from .scope import scope_for
from .services import onboarding_board, schedule_week_start
from . import views


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
    
    context = {"person": person, "manager": manager, "today": today}
    
    if manager:
        # Manager "Today" workspace: open posts needing coverage, pending punches to review
        scope = scope_for(request)
        context.update({
            "open_posts": scope.filter_shifts(org.shifts.filter(
                officer__isnull=True,
                status__in=[Shift.Status.DRAFT, Shift.Status.PUBLISHED],
                ends_at__gte=now,
                starts_at__lt=now + timedelta(days=7)
            ).select_related("site__client")).order_by("starts_at")[:10],
            
            "pending_review_count": scope.filter_punches(org.punches.filter(
                review_status=Punch.Review.PENDING
            )).count(),
        })
    else:
        # Employee "Today": clock, next shift, onboarding, documents, offers
        if person:
            next_shift = person.shifts.filter(
                status=Shift.Status.PUBLISHED,
                ends_at__gte=now
            ).select_related("site__client", "post").order_by("starts_at").first()
            
            context.update({
                "next_shift": next_shift,
                "can_clock": True,
                "pending_onboarding": OnboardingTask.objects.filter(
                    person=person,
                    completed_at__isnull=True
                ).count(),
            })
    
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
    
    context = {
        "people_count": people.count(),
        "onboarding_count": onboarding.count(),
        "active_count": active.count(),
        
        "onboarding_list": onboarding[:20],
        "active_list": active[:20],
        
        "people": people[:50],
        "scope": scope,
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
    week_start = schedule_week_start(now)
    week_end = week_start + timedelta(days=7)
    
    shifts = scope.filter_shifts(org.shifts.filter(
        starts_at__gte=week_start,
        starts_at__lt=week_end
    ).select_related("site__client", "officer")).order_by("starts_at")
    
    open_posts = scope.filter_shifts(org.shifts.filter(
        officer__isnull=True,
        status__in=[Shift.Status.DRAFT, Shift.Status.PUBLISHED],
        ends_at__gte=now
    ).select_related("site__client")).order_by("starts_at")[:15]
    
    context = {
        "week_start": week_start,
        "week_end": week_end,
        
        "shifts": shifts,
        "open_posts_count": open_posts.count(),
        "open_posts": open_posts,
        
        "pending_changes": (
            scope.filter_shifts(org.shifts.filter(
                status=Shift.Status.PUBLISHED,
                ends_at__gte=now - timedelta(days=7)
            )).count()
        ),
    }
    
    return render(request, "core/workspaces/schedule.html", context)


@membership_required(*views.TIME_REVIEWERS)
@require_GET
def workspace_payroll(request):
    """Payroll processor and time reviewer: period management and exceptions.
    
    Shows pending reviews, open payroll periods, exceptions, and lock state.
    """
    org = request.organization
    scope = scope_for(request)
    now = timezone.now()
    
    # Pending punches needing review
    pending_punches = scope.filter_punches(
        org.punches.filter(review_status=Punch.Review.PENDING)
    ).order_by("-occurred_at")[:20]
    
    # Recent payroll runs
    latest_run = org.payroll_runs.order_by("-period_start").first()
    runs = org.payroll_runs.order_by("-period_start")[:10]
    
    context = {
        "pending_punches": pending_punches,
        "pending_count": scope.filter_punches(
            org.punches.filter(review_status=Punch.Review.PENDING)
        ).count(),
        
        "latest_run": latest_run,
        "runs": runs,
        
        "can_approve": request.membership.role in views.PRIVILEGED,
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
        "can_execute_disposition": request.membership.role in views.PRIVILEGED,
    }
    
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
        try:
            audit_events = org.audit_events.select_related("actor").order_by(
                "-created_at"
            )[:20]
        except Exception:
            audit_events = []
    
    context = {
        "can_capture": True,
        "audit_events": audit_events,
        "can_audit": request.membership.role in views.AUDIT_READERS,
    }
    
    return render(request, "core/workspaces/reports.html", context)
