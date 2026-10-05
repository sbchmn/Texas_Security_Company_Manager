from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
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

SIGNING_ADMIN = (Membership.Role.OWNER, Membership.Role.ADMIN)
SIGNING_STAFF = SIGNING_ADMIN + (Membership.Role.HR,)


class SigningSettingsForm(forms.ModelForm):
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
            if old.base_url != data.get("base_url") and old.organization.signing_requests.filter(
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


def _return_to_task(task):
    return redirect(reverse("person_detail", args=[task.person_id]) + "?tab=onboarding")


@membership_required(*SIGNING_STAFF)
@require_POST
def signing_send(request, task_id):
    task = _task(request, task_id)
    try:
        issue_signing_request(task, request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "Signing request created. The invitation is queued for delivery; the step stays open until signed documents are filed.")
    return _return_to_task(task)


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
            messages.success(request, result.get_status_display())
    return _return_to_task(task)


@membership_required()
@require_GET
def signing_open(request, task_id):
    task = _task(request, task_id)
    if task.person.user_id != request.user.pk or task.status != OnboardingTask.Status.OPEN:
        raise Http404
    signing = task.signing_requests.order_by("-attempt").first()
    if signing is None or signing.status != SigningRequest.Status.SENT:
        messages.error(request, "No open signing invitation is available. Check status or ask the office to send it.")
        return _return_to_task(task)
    try:
        allowed_origin(signing.base_url)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return _return_to_task(task)
    return redirect(f"{signing.base_url}/s/{signing.signing_slug}")
