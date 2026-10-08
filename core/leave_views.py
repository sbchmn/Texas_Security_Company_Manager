from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .auth import membership_required
from .leave import HR_ROLES, adjust, cancel_approved, enroll, lock_bank, sync_account
from .leave_forms import LeaveAdjustmentForm, LeaveEnrollmentForm, LeavePolicyForm
from .models import AuditEvent, LeaveAccount, LeavePolicy, Membership


@membership_required(Membership.Role.OWNER, Membership.Role.ADMIN)
@transaction.atomic
def leave_policy(request):
    lock_bank(request.organization)
    policy, _ = LeavePolicy.objects.get_or_create(organization=request.organization)
    policy = LeavePolicy.objects.select_for_update().get(pk=policy.pk)
    form = LeavePolicyForm(request.POST or None, instance=policy)
    if request.method == "POST" and form.is_valid():
        # Materialize existing year terms under the old saved policy before editing it.
        for account in request.organization.leave_accounts.select_related("organization"):
            sync_account(account)
        form.save()
        AuditEvent.objects.create(organization=request.organization, actor=request.user, action="leave.policy_changed",
                                 target_type="leave_policy", target_id=str(policy.pk),
                                 metadata={"changed_fields": form.changed_data})
        messages.success(request, "Leave policy saved. Existing bank-year terms remain fixed; changes apply to new enrollments and future years.")
        return redirect("leave_policy")
    return render(request, "core/leave_policy.html", {"form": form})


@require_POST
@membership_required(*HR_ROLES)
def employee_leave(request, person_id):
    person = get_object_or_404(request.organization.people, pk=person_id)
    action = request.POST.get("action")
    form = LeaveEnrollmentForm(request.POST) if action == "enroll" else LeaveAdjustmentForm(request.POST)
    if action not in ("enroll", "adjust"):
        messages.error(request, "Choose enrollment or an audited leave adjustment.")
    elif form.is_valid():
        try:
            if action == "enroll":
                enroll(person, form.cleaned_data["eligible_from"], request.user)
            else:
                account = get_object_or_404(LeaveAccount, person=person, organization=request.organization)
                adjust(account, form.cleaned_data["hours"], form.cleaned_data["reason"], request.user)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Leave bank updated.")
            return redirect(reverse("person_detail", args=[person.pk]) + "#leave-bank")
    if form.errors:
        from .leave import balance
        account = LeaveAccount.objects.filter(person=person, organization=request.organization).first()
        return render(request, "core/leave_edit.html", {"person": person, "form": form, "action": action,
                                                       "bank": balance(account) if account else None})
    return redirect(reverse("person_detail", args=[person.pk]) + "#leave-bank")


@require_POST
@membership_required(*HR_ROLES)
def leave_cancel(request, request_id):
    item = get_object_or_404(request.organization.time_off_requests, pk=request_id)
    try:
        cancel_approved(item, request.POST.get("reason", ""), request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "Approved leave cancelled." +
                         (" Its outstanding bank reservation was restored." if item.use_leave_bank else ""))
    return redirect(reverse("time_off") + "?show=decided")
