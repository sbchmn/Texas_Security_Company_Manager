from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from .attendance import MANAGERS, follow_up, scoped_cases
from .auth import membership_required
from .form_ui import WorkflowForm
from .models import AttendanceCase, AttendanceCaseAction, HoldOver, Person
from .scope import scope_for
from .sms import zone_for


class AttendanceFollowUpForm(WorkflowForm):
    field_sections = (
        ("Follow-up", ("action", "note")),
        ("For a hold-over only", ("expected_release_at", "reason", "relief")),
    )
    ACTION_CHOICES = (
        (AttendanceCaseAction.Kind.CONTACT, "Record contact / attempted contact"),
        (AttendanceCaseAction.Kind.RELIEF, "Record relief coordination"),
        (AttendanceCaseAction.Kind.HOLD_OVER, "Approve hold-over until an expected release"),
        (AttendanceCaseAction.Kind.CLOSED, "Close after manager review"),
    )
    action = forms.ChoiceField(choices=ACTION_CHOICES)
    note = forms.CharField(min_length=10, max_length=2000, widget=forms.Textarea(attrs={"rows": 3}),
                          help_text="Record the facts, contact result, relief plan, or reason for closing. This does not change assignments or punches.")
    expected_release_at = forms.DateTimeField(required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        help_text="For a hold-over: the authorized expected departure, not a clock-out.")
    reason = forms.ChoiceField(required=False, choices=[("", "Choose a hold-over reason")] + list(HoldOver.Reason.choices))
    relief = forms.ModelChoiceField(queryset=Person.objects.none(), required=False,
        help_text="For a hold-over: who is expected to relieve this post. This records coordination, not a schedule change.")

    def __init__(self, *args, organization, scope, case, **kwargs):
        super().__init__(*args, **kwargs)
        relief_field = self.fields["relief"]
        action_field = self.fields["action"]
        if not isinstance(relief_field, forms.ModelChoiceField) or not isinstance(action_field, forms.ChoiceField):
            raise TypeError("Attendance follow-up requires officer and action choice fields.")
        relief_field.queryset = scope.filter_people(organization.people.all()).exclude(pk=case.person_id)
        if case.kind != AttendanceCase.Kind.DEPARTURE:
            action_field.choices = [choice for choice in self.ACTION_CHOICES if choice[0] != "hold_over"]
            for name in ("expected_release_at", "reason", "relief"):
                self.fields.pop(name)

    def clean(self):
        data = super().clean()
        if data.get("action") == AttendanceCaseAction.Kind.HOLD_OVER:
            for field in ("expected_release_at", "reason"):
                if not data.get(field):
                    self.add_error(field, "Required when approving a hold-over.")
        return data


@membership_required()
@require_http_methods(["GET", "POST"])
def attendance_queue(request, case_id=None):
    with timezone.override(zone_for(request.organization)):
        return _attendance_queue(request, case_id)


def _attendance_queue(request, case_id):
    org = request.organization
    scope = scope_for(request)
    can_manage = request.membership.role in MANAGERS
    if not can_manage:
        if request.method == "POST":
            raise PermissionDenied
        if case_id:
            case = get_object_or_404(org.attendance_cases.filter(person__user=request.user), pk=case_id)
            return redirect(reverse("my_shifts") + f"?attendance={case.pk}#attendance-alerts")
        return redirect(reverse("my_shifts") + "#attendance-alerts")
    cases = scoped_cases(org, scope) if can_manage else org.attendance_cases.filter(person__user=request.user)
    cases = cases.select_related("shift__site__client", "person")
    selected = get_object_or_404(cases, pk=case_id) if case_id else None
    form = None
    if selected and can_manage and selected.status == AttendanceCase.Status.OPEN:
        form = AttendanceFollowUpForm(request.POST if request.method == "POST" else None,
                                     organization=org, scope=scope, case=selected)
    if request.method == "POST":
        if not can_manage:
            raise PermissionDenied
        if selected is None:
            raise Http404("Select an attendance case before recording follow-up.")
        if form is None:
            messages.error(request, "This case is already resolved; no follow-up was saved.")
            return redirect("attendance_detail", case_id=selected.pk)
        if form.is_valid():
            try:
                follow_up(selected, request.user, form.cleaned_data["action"], form.cleaned_data["note"],
                          expected_release_at=form.cleaned_data.get("expected_release_at"),
                          reason=form.cleaned_data.get("reason"), relief=form.cleaned_data.get("relief"))
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, "Follow-up recorded. Punches and pay are unchanged.")
                return redirect("attendance_detail", case_id=selected.pk)
    status = request.GET.get("status", AttendanceCase.Status.OPEN)
    kind = request.GET.get("kind", "")
    if status not in ("open", "resolved", "all") or kind not in ("", "arrival", "departure"):
        messages.error(request, "Unknown attendance filter; showing open cases of both kinds.")
        status, kind = "open", ""
    counts = {name: cases.filter(status="open", kind=name).count() for name in AttendanceCase.Kind.values}
    rows = cases
    if status != "all":
        rows = rows.filter(status=status)
    if kind:
        rows = rows.filter(kind=kind)
    search = request.GET.get("q", "").strip()
    if search:
        rows = rows.filter(Q(person__first_name__icontains=search) | Q(person__last_name__icontains=search)
                           | Q(shift__site__name__icontains=search))
    return render(request, "core/attendance.html", {
        "cases": Paginator(rows.order_by("-opened_at", "pk"), 25).get_page(request.GET.get("page")),
        "selected": selected, "form": form, "can_manage": can_manage, "counts": counts,
        "status_filter": status, "kind_filter": kind, "search": search,
        "history": Paginator(selected.actions.select_related("actor").order_by("-created_at", "-pk"), 25).get_page(request.GET.get("history_page")) if selected and can_manage else None,
        "authority_scope": scope if can_manage and scope.restricted else None,
    })
