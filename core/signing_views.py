from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .auth import membership_required
from .document_signing import (
    DocuSealClient, allowed_origin, encrypt_api_key, issue_signing_request, reconcile_signing_request,
)
from .models import (
    AuditEvent, Membership, OnboardingItem, OnboardingTask, SigningRequest, SigningSettings, record_open_for,
)
from .scope import scope_for
from .form_ui import WorkflowModelForm
from .workflow_actions import onboarding_actions

SIGNING_ADMIN = (Membership.Role.OWNER, Membership.Role.ADMIN)
SIGNING_STAFF = SIGNING_ADMIN + (Membership.Role.HR,)


class SigningSettingsForm(WorkflowModelForm):
    api_key = forms.CharField(
        required=False, strip=True, widget=forms.PasswordInput(),
        help_text="DocuSeal API key. Leave blank to keep the existing key; it is never displayed.",
    )

    class Meta:
        model = SigningSettings
        fields = ["base_url", "api_key", "enabled"]

    def clean_base_url(self):
        return allowed_origin(self.cleaned_data["base_url"])

    def clean(self):
        data = super().clean()
        key = data.get("api_key")
        if key:
            self.instance.encrypted_api_key = encrypt_api_key(key)
        if not self.instance.encrypted_api_key:
            self.add_error("api_key", "Enter the DocuSeal API key.")
        if self.instance.pk:
            old = SigningSettings.objects.get(pk=self.instance.pk)
            if old.base_url != data.get("base_url") and SigningRequest.objects.filter(organization=old.organization,
                status__in=[SigningRequest.Status.PREPARING, SigningRequest.Status.SENT],
            ).exists():
                self.add_error("base_url", "Outstanding signing requests still use the current backend.")
        if data.get("enabled") and not self.errors:
            self.instance.base_url = data["base_url"]
            try:
                result = DocuSealClient(self.instance).api("GET", "/templates", params={"limit": 1})
                if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                    raise ValidationError("DocuSeal returned an invalid template list.")
            except ValidationError as exc:
                self.add_error("api_key", exc)
        return data


@membership_required(*SIGNING_ADMIN)
@transaction.atomic
def signing_settings(request):
    config = SigningSettings.objects.select_for_update().filter(organization=request.organization).first()
    config = config or SigningSettings(organization=request.organization)
    form = SigningSettingsForm(request.POST or None, instance=config)
    if request.method == "POST" and form.is_valid():
        form.save()
        AuditEvent.objects.create(
            organization=request.organization, actor=request.user, action="signing.settings_saved",
            target_type="organization", target_id=str(request.organization.pk),
            metadata={"enabled": config.enabled, "origin": config.base_url, "key_replaced": bool(form.cleaned_data["api_key"])},
        )
        messages.success(request, "Document signing settings saved. Creating a checklist never sends documents automatically.")
        return redirect("signing_settings")
    return render(request, "core/signing_settings.html", {"form": form, "config": config})


def _task(request, task_id):
    task = request.organization.onboarding_tasks.select_related(
        "organization", "person__user", "item__document_type",
    ).filter(pk=task_id).first()
    if task is None or not scope_for(request).permits_person(task.person):
        raise Http404
    is_self = task.person.user_id == request.user.pk
    if request.membership.role not in SIGNING_STAFF and not is_self:
        raise Http404
    if task.item.kind != OnboardingItem.Kind.SIGNATURE or not task.item.document_type_id:
        raise Http404
    subject = request.organization.people.filter(user=request.user).values_list("pk", flat=True).first()
    if not record_open_for(task.item.document_type, task.person_id, request.membership.role, subject):
        raise Http404
    return task


def _return_to_task(task, request=None):
    if request is not None and request.POST.get("return_to") == "signing_queue" and request.membership.role in SIGNING_STAFF:
        return redirect("signing_queue")
    if request is not None and task.person.user_id == request.user.pk and request.membership.role not in SIGNING_STAFF:
        return redirect("my_onboarding")
    return redirect(reverse("person_detail", args=[task.person_id]) + "?tab=onboarding")


@membership_required(*SIGNING_STAFF)
@require_GET
def signing_queue(request):
    actions = onboarding_actions(request)
    stages = [("all", "All"), ("ready", "Ready to send"), ("awaiting", "Awaiting signer"),
              ("processing", "Processing / verification"), ("failed", "Needs attention"),
              ("completed", "Signed and filed")]
    stage = request.GET.get("stage", "all")
    if stage not in dict(stages):
        raise Http404
    rows = actions["signing_rows"]
    selected = request.GET.get("task")
    if selected:
        rows = [row for row in rows if str(row["task"].pk) == selected]
        if not rows:
            raise Http404
    return render(request, "core/signing_queue.html", {
        "queue": Paginator([row for row in rows if stage == "all" or row["category"] == stage], 25).get_page(request.GET.get("page")),
        "stage": stage, "stages": [{"value": value, "label": label,
                                   "count": len(rows) if value == "all" else actions["signing_counts"][value]}
                                  for value, label in stages],
    })


@membership_required(*SIGNING_STAFF)
@require_POST
def signing_send(request, task_id):
    task = _task(request, task_id)
    try:
        issue_signing_request(task, request.user,
                              return_url=request.build_absolute_uri(reverse("signing_return", args=[task.pk])))
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "Signing request created. The invitation is queued for delivery; the step stays open until signed documents are filed.")
    return _return_to_task(task, request)


@membership_required()
@require_POST
def signing_refresh(request, task_id):
    task = _task(request, task_id)
    signing = task.signing_requests.order_by("-attempt").first()
    if signing is None:
        messages.error(request, "The office has not sent this step for signature yet.")
    else:
        try:
            result = reconcile_signing_request(signing.pk)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, SigningRequest.Status(result.status).label)
    return _return_to_task(task, request)


@membership_required()
@require_GET
def signing_open(request, task_id):
    task = _task(request, task_id)
    if task.person.user_id != request.user.pk or task.status != OnboardingTask.Status.OPEN:
        raise Http404
    signing = task.signing_requests.order_by("-attempt").first()
    if signing is None or signing.status != SigningRequest.Status.SENT:
        messages.error(request, "No open signing invitation is available. Check status or ask the office to send it.")
        return _return_to_task(task, request)
    try:
        allowed_origin(signing.base_url)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return _return_to_task(task, request)
    return redirect(f"{signing.base_url}/s/{signing.signing_slug}")


@membership_required()
@require_GET
def signing_return(request, task_id):
    """Where DocuSeal sends the signer after they finish (the submitter's `completed_redirect_url`).

    Arriving here proves nothing: anyone can type the URL. It only triggers the same API reconcile the
    status button does, so the signed copies are usually filed by the time the checklist renders.
    """
    task = _task(request, task_id)
    if task.person.user_id != request.user.pk:
        raise Http404
    signing = task.signing_requests.order_by("-attempt").first()
    if signing is None:
        return _return_to_task(task, request)
    if signing.status == SigningRequest.Status.SENT:
        try:
            signing = reconcile_signing_request(signing.pk)
        except ValidationError:
            messages.info(request, "Thanks — your signature was submitted. Your documents will be filed shortly; "
                                   "use Check status if this step still shows as waiting.")
            return _return_to_task(task, request)
    if signing.status == SigningRequest.Status.COMPLETED:
        messages.success(request, "Thanks — your signed documents are filed in your personnel file.")
    elif signing.status == SigningRequest.Status.SENT:
        messages.info(request, "Thanks — DocuSeal is still finishing your documents. They will be filed shortly.")
    else:
        messages.info(request, SigningRequest.Status(signing.status).label)
    return _return_to_task(task, request)
