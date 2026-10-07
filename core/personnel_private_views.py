from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from .auth import membership_required
from .form_ui import WorkflowForm
from .models import AuditEvent, Person, PrivatePersonnelDetails
from .personnel_private import PRIVATE_FIELDS, PRIVATE_PERSONNEL_ROLES, cipher, decrypt_details, encrypt_details, normalize_ssn
from .scope import scope_for


class PrivatePersonnelForm(WorkflowForm):
    field_sections = (
        ("Social Security number", ("ssn", "clear_ssn")),
        ("Driver's license", ("driver_license_number", "driver_license_state")),
        ("Accountability", ("reason",)),
    )
    ssn = forms.CharField(required=False, max_length=11, label="Replace / enter full SSN",
        widget=forms.PasswordInput(attrs={"autocomplete": "off", "inputmode": "numeric"}),
        help_text="Never prefilled. Blank keeps the existing number. SSNs are encrypted and excluded from ordinary exports and profile history.")
    clear_ssn = forms.BooleanField(required=False, label="Remove the stored SSN")
    driver_license_number = forms.CharField(required=False, max_length=120, label="Driver's license number")
    driver_license_state = forms.CharField(required=False, max_length=2, label="Driver's license state")
    reason = forms.CharField(min_length=10, max_length=200, label="Reason for updating",
        help_text="State the business purpose. Do not include SSNs or license numbers here.")

    @sensitive_variables()
    def clean_ssn(self):
        raw = self.cleaned_data["ssn"]
        return normalize_ssn(raw) if raw else ""

    def clean_driver_license_state(self):
        value = self.cleaned_data["driver_license_state"].upper()
        if value and (len(value) != 2 or not value.isascii() or not value.isalpha()):
            raise forms.ValidationError("Use a two-letter state abbreviation.")
        return value

    def clean(self):
        data = super().clean()
        if data.get("ssn") and data.get("clear_ssn"):
            self.add_error("clear_ssn", "Choose either replacement or removal, not both.")
        return data


class RevealSsnForm(WorkflowForm):
    reason = forms.CharField(min_length=10, max_length=200, label="Business purpose for revealing the SSN",
        help_text="Required and audited. Do not enter the SSN or other sensitive values here.")


@sensitive_post_parameters()
@membership_required(*PRIVATE_PERSONNEL_ROLES)
@require_http_methods(["GET", "POST"])
@never_cache
@transaction.atomic
@sensitive_variables()
def private_personnel(request, person_id):
    people = scope_for(request).filter_people(Person.objects.filter(organization=request.organization))
    person = get_object_or_404(people.select_for_update() if request.method == "POST" else people, pk=person_id)
    record = PrivatePersonnelDetails.objects.filter(organization=request.organization, person=person).first()
    masked = f"***-**-{record.ssn_last_four}" if record and record.ssn_last_four else "Not recorded"
    reveal_form = RevealSsnForm(prefix="reveal")
    form = None
    revealed = ""
    error = ""
    try:
        cipher()
        values = decrypt_details(person, record)
    except ValidationError as exc:
        error = " ".join(exc.messages)
    else:
        initial = {key: value for key, value in values.items() if key != "ssn"}
        form = PrivatePersonnelForm(initial=initial)
        if request.method == "POST":
            action = request.POST.get("action")
            if action == "reveal":
                reveal_form = RevealSsnForm(request.POST, prefix="reveal")
                if reveal_form.is_valid():
                    if values["ssn"]:
                        AuditEvent.objects.create(organization=request.organization, actor=request.user,
                            action="person.ssn_revealed", target_type="person", target_id=str(person.pk),
                            metadata={"encrypted_purpose": cipher().encrypt(reveal_form.cleaned_data["reason"].encode()).decode()})
                        digits = values["ssn"]
                        revealed = f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
                    else:
                        reveal_form.add_error(None, "No SSN is recorded.")
            elif action == "save":
                form = PrivatePersonnelForm(request.POST)
                if form.is_valid():
                    cleaned = form.cleaned_data
                    updated = {key: str(cleaned.get(key) or "") for key in PRIVATE_FIELDS}
                    updated["ssn"] = "" if cleaned["clear_ssn"] else cleaned["ssn"] or values["ssn"]
                    changed = [key for key in PRIVATE_FIELDS if updated[key] != values[key]]
                    if changed:
                        encrypted = encrypt_details(person, updated)
                        PrivatePersonnelDetails.objects.update_or_create(
                            organization=request.organization, person=person,
                            defaults={"encrypted_payload": encrypted, "ssn_last_four": updated["ssn"][-4:],
                                      "updated_by": request.user})
                        AuditEvent.objects.create(organization=request.organization, actor=request.user,
                            action="person.private_updated", target_type="person", target_id=str(person.pk),
                            metadata={"fields": changed, "encrypted_purpose": cipher().encrypt(cleaned["reason"].encode()).decode()})
                    messages.success(request, "Restricted personnel details saved." if changed else "Nothing changed.")
                    return redirect("private_personnel", person_id=person.pk)
            else:
                messages.error(request, "Unknown restricted-personnel action; nothing was saved or revealed.")
        AuditEvent.objects.create(organization=request.organization, actor=request.user,
            action="person.private_viewed", target_type="person", target_id=str(person.pk),
            metadata={})
    response = render(request, "core/private_personnel.html", {
        "person": person, "form": form, "reveal_form": reveal_form, "masked_ssn": masked,
        "revealed_ssn": revealed, "storage_error": error,
    })
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response
