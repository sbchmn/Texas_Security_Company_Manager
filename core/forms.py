import re

from django import forms
from django.contrib.auth.password_validation import validate_password
from .models import PERSONNEL_CATEGORIES, ROTATING_PRESETS, SERIES_MAX_DAYS, WEEKDAY_CHOICES, AuthorityScope, AvailabilityRule, ChannelRule, Checkpoint, Client, ClockKiosk, ComplianceRule, Credential, CredentialType, CustomFieldDefinition, DispositionRequest, DocumentType, Branch, ImportBatch, Membership, MessageConsent, Notification, OnboardingItem, Organization, PayCode, Person, PersonDocument, Shift, ShiftTemplate, Site, TimeOffRequest, TimePolicy, TimePolicyOverride, TrainingRecord
from .services import normalize_destination, sms_consent_wording, sms_enrollment_message, validate_clock_pin
from .form_ui import InheritedPostOrdersForm, WorkflowForm, WorkflowModelForm

class MembershipInvitationForm(WorkflowForm):
    email = forms.EmailField(help_text="The invitation is valid for 72 hours.")
    role = forms.ChoiceField(choices=((Membership.Role.OWNER, "Owner"), (Membership.Role.ADMIN, "Administrator")))

    def clean_email(self):
        return self.cleaned_data["email"].strip().casefold()

class PersonAccessForm(WorkflowForm):
    """Access level granted when a personnel record is given a sign-in.

    Owner and Administrator are deliberately absent: those are issued from Team access, and an
    HR user must not be able to mint one from a personnel profile.
    """
    role = forms.ChoiceField(
        choices=((Membership.Role.OFFICER, "Officer — clock in, own documents"),
                 (Membership.Role.SUPERVISOR, "Site supervisor"),
                 (Membership.Role.SCHEDULER, "Scheduler / dispatcher"),
                 (Membership.Role.PAYROLL, "Payroll approver"),
                 (Membership.Role.HR, "HR / Compliance")),
        initial=Membership.Role.OFFICER,
        help_text="Officer is the right level for a guard who only needs the time clock and their own records.")

class MembershipAccessForm(WorkflowForm):
    """Change a team member's role, or switch their access off and on.

    Only an owner may grant the Owner role or change an owner's access; the view enforces that,
    and keeps at least one active owner.
    """
    role = forms.ChoiceField(choices=Membership.Role.choices)
    active = forms.BooleanField(required=False, label="Access active",
                                help_text="Clear this to switch off their sign-in for this company without deleting anything.")

    def __init__(self, *args, actor_role=None, **kwargs):
        super().__init__(*args, **kwargs)
        if actor_role != Membership.Role.OWNER:
            self.fields["role"].choices = [item for item in Membership.Role.choices if item[0] != Membership.Role.OWNER]

class PersonLinkForm(WorkflowForm):
    """Attach an existing team member's sign-in to a personnel record, without changing their role.

    Only active members not already linked to another record are offered. HR / Compliance may
    link only non-privileged members: attaching a record to an owner or administrator's account
    is left to those roles.
    """
    member = forms.ModelChoiceField(queryset=Membership.objects.none(), label="Team member",
                                    help_text="Their role stays as it is. Change roles from Team access.")

    def __init__(self, *args, organization=None, actor_role=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organization is None:
            return
        linked = organization.people.filter(user__isnull=False).values("user_id")
        members = organization.memberships.filter(active=True).exclude(user_id__in=linked).select_related("user")
        if actor_role not in (Membership.Role.OWNER, Membership.Role.ADMIN):
            members = members.exclude(role__in=(Membership.Role.OWNER, Membership.Role.ADMIN))
        self.fields["member"].queryset = members.order_by("user__last_name", "user__first_name", "user__email")
        self.fields["member"].label_from_instance = str

class AvailabilityRuleForm(WorkflowModelForm):
    """One weekly window an officer is willing to stand."""
    class Meta:
        model = AvailabilityRule
        fields = ["weekday", "starts_at", "ends_at"]
        widgets = {"starts_at": forms.TimeInput(attrs={"type": "time"}), "ends_at": forms.TimeInput(attrs={"type": "time"})}
        help_texts = {
            "ends_at": "Set a time earlier than the start for a window that runs past midnight, such as 18:00 to 06:00.",
        }

class TimeOffRequestForm(WorkflowModelForm):
    class Meta:
        model = TimeOffRequest
        fields = ["starts_at", "ends_at", "reason", "use_leave_bank"]
        widgets = {"starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"}), "ends_at": forms.DateTimeInput(attrs={"type": "datetime-local"}), "reason": forms.Textarea(attrs={"rows": 3})}
        help_texts = {
            "use_leave_bank": "Request paid bank hours. A reviewer confirms the hours; insufficient available balance prevents approval.",
            "reason": "Optional. A manager deciding the request sees this, and the schedule warning names the posts it collides with.",
        }

class AuthorityScopeForm(WorkflowModelForm):
    """Grant one bounded scope of manager authority.

    Exactly one of the three selects is filled; the model's ``clean`` is what makes that a
    rule rather than a convention, so a half-filled grant cannot be saved from the form, the
    admin, or a fixture.
    """
    class Meta:
        model = AuthorityScope
        fields = ["branch", "client", "site", "follows_own_branch"]
        help_texts = {
            "branch": "The branch's personnel and every post stood at its sites.",
            "client": "Every post under this contract and the officers who stand them.",
            "site": "This single post and every officer assigned to it.",
            # The one option that is not a snapshot: it says "wherever this person's file says they
            # work", which is what a firm that reassigns guards means and what a frozen branch id
            # stops meaning the day somebody moves.
            "follows_own_branch": "Their authority follows the branch on their own personnel file, including after a reassignment. Use this instead of naming a branch, never alongside it.",
        }

class SmsConsentFormMixin:
    sms_consent_field = "opt_in"

    def __init__(self, *args, organization=None, **kwargs):
        self.sms_organization = organization
        super().__init__(*args, **kwargs)
        field = self.fields[self.sms_consent_field]
        field.label = "I agree to receive workforce text alerts"
        field.disabled = organization is None or not organization.sms_program_ready
        if organization is not None:
            from django.utils.html import format_html
            field.help_text = (format_html(
                '{} <a href="{}">SMS Terms</a> and <a href="{}">Privacy Policy</a>.',
                sms_consent_wording(organization), organization.sms_terms_url,
                organization.sms_privacy_url) if organization.sms_program_ready
                else sms_consent_wording(organization))

    def clean(self):
        data = super().clean()
        if (self.data.get(self.sms_consent_field)
                and (self.sms_organization is None or not self.sms_organization.sms_program_ready)):
            self.add_error(self.sms_consent_field, "New SMS opt-ins are unavailable until the company configures SMS terms, privacy and support information.")
        return data


class SmsProgramForm(WorkflowModelForm):
    class Meta:
        model = Organization
        fields = ["sms_privacy_url", "sms_terms_url", "support_email"]
        labels = {"sms_privacy_url": "SMS privacy policy URL", "sms_terms_url": "SMS terms URL",
                  "support_email": "SMS / company support email"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.fields:
            self.fields[name].required = True
        self.fields["support_email"].help_text = "A monitored support address shown in consent, enrollment and HELP messages."
        for name in ("sms_privacy_url", "sms_terms_url"):
            self.fields[name].help_text = "Public HTTPS URL for this company's published policy; no login required."

    def clean(self):
        data = super().clean()
        for name in ("sms_privacy_url", "sms_terms_url"):
            value = data.get(name, "")
            if value and not value.startswith("https://"):
                self.add_error(name, "Use a public HTTPS URL.")
        if not self.errors:
            organization = Organization(display_name=self.instance.display_name,
                support_email=data["support_email"], sms_terms_url=data["sms_terms_url"],
                sms_privacy_url=data["sms_privacy_url"])
            if not organization.sms_program_ready:
                raise forms.ValidationError("Set the company display name in Company settings before enabling SMS opt-ins.")
            if len(sms_consent_wording(organization)) > 4096 or len(sms_enrollment_message(organization)) > 1024:
                raise forms.ValidationError("The SMS disclosures are too long. Use shorter company-owned policy URLs or a shorter support address.")
        return data


class InvitationAcceptanceForm(SmsConsentFormMixin, WorkflowForm):
    sms_consent_field = "text_alerts"
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150)
    password = forms.CharField(widget=forms.PasswordInput, min_length=12, help_text="Use at least 12 characters.")
    password_confirmation = forms.CharField(widget=forms.PasswordInput, label="Confirm password")
    # NTF-4's capture point, on the form the person is filling anyway. The owner's ruling was that
    # consent is asked "at the account's own start", and asking it here costs one tick rather than a
    # second email, a second login, and a month of notices nobody opted anyone in to receive. The
    # number is on the same screen because consent belongs to it, and both are optional: a person who
    # is not interested answers nothing and is never texted.
    mobile_phone = forms.CharField(max_length=30, required=False, label="Mobile number (for text alerts)",
        help_text="Digits, with or without +. Needed only if you want texts.")
    text_alerts = forms.BooleanField(required=False, label="Text me about my posts, clock reminders and timecard questions")

    def clean_mobile_phone(self):
        value = str(self.cleaned_data.get("mobile_phone") or "").strip()
        if value and not normalize_destination(value, MessageConsent.Channel.SMS):
            raise forms.ValidationError("That is not a usable mobile number.")
        return value

    def clean(self):
        data = super().clean()
        if data.get("text_alerts") and not data.get("mobile_phone"):
            self.add_error("mobile_phone", "A number is needed before texts can be sent to you.")
        if data.get("password") != data.get("password_confirmation"):
            self.add_error("password_confirmation", "Passwords do not match.")
        elif data.get("password"):
            validate_password(data["password"])
        return data

class BrandForm(WorkflowModelForm):
    def __init__(self,*args,**kwargs):
        if args and args[0] is not None and kwargs.get("instance"):
            data=args[0].copy()
            for name in ("dark_primary_color","dark_accent_color"):
                if name not in data:data[name]=getattr(kwargs["instance"],name)
            args=(data,*args[1:])
        super().__init__(*args,**kwargs)
    class Meta:
        model = Organization
        fields = ["display_name", "primary_color", "accent_color", "dark_primary_color", "dark_accent_color", "dark_mode_enabled", "logo", "support_email", "support_phone", "timezone"]
        widgets = {name: forms.TextInput(attrs={"type": "color"}) for name in ("primary_color","accent_color","dark_primary_color","dark_accent_color")}
    @staticmethod
    def _luminance(color):
        values=[int(color[i:i+2],16)/255 for i in (1,3,5)]
        values=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in values]
        return .2126*values[0]+.7152*values[1]+.0722*values[2]
    @classmethod
    def _contrast(cls,a,b):
        high,low=sorted((cls._luminance(a),cls._luminance(b)),reverse=True);return (high+.05)/(low+.05)
    def clean(self):
        data=super().clean()
        for field,background in (("primary_color","#FFFFFF"),("dark_primary_color","#FFFFFF")):
            if data.get(field) and self._contrast(data[field],background)<4.5: self.add_error(field,"Color must provide at least 4.5:1 contrast with white text.")
        if data.get("accent_color") and data.get("primary_color") and self._contrast(data["accent_color"],data["primary_color"])<3: self.add_error("accent_color","Accent must provide at least 3:1 contrast with the primary color.")
        return data

class BranchForm(WorkflowModelForm):
    class Meta:
        model = Branch
        fields = ["name", "city", "active"]

class CheckpointForm(WorkflowModelForm):
    class Meta:
        model = Checkpoint
        fields = ["name", "latitude", "longitude", "radius_meters", "active"]
        help_texts = {"radius_meters": "How close a scan must be to this point to count."}

class PayCodeForm(WorkflowModelForm):
    class Meta:
        model = PayCode
        fields = ["name", "code", "cost_centre", "description", "active"]
        help_texts = {
            "code": "Exactly what the customer's payroll system expects to receive — casing and punctuation included.",
            "cost_centre": "Optional account or department the hours are charged to.",
        }


class PersonForm(WorkflowModelForm):
    field_sections = (
        ("Identity", ("first_name", "middle_name", "last_name", "name_suffix", "preferred_name", "employee_id", "date_of_birth", "birth_city", "birth_state")),
        ("Contact", ("email", "mobile_phone", "address_line1", "address_line2", "city", "county", "state", "postal_code")),
        ("Mailing address (if different)", ("mailing_address_line1", "mailing_address_line2", "mailing_city", "mailing_state", "mailing_postal_code")),
        ("Employment", ("branch", "job_title", "status", "hire_date", "termination_date", "eligible_for_rehire", "hourly_rate")),
        ("Emergency contact", ("emergency_contact_name", "emergency_contact_relationship", "emergency_contact_phone")),
        ("Personnel categories", ("is_unarmed_officer", "is_commissioned_officer", "is_ppo",
                                  "is_private_investigator", "is_shareholder")),
    )
    class Meta:
        model = Person
        fields = ["branch", "employee_id", "first_name", "last_name", "email", "mobile_phone", "job_title", "hire_date", "termination_date", "date_of_birth", "address_line1", "address_line2", "city", "state", "postal_code", "emergency_contact_name", "emergency_contact_phone", "hourly_rate", "status", "is_unarmed_officer", "is_commissioned_officer", "is_ppo", "is_private_investigator", "is_shareholder"]
        fields += ["middle_name", "name_suffix", "preferred_name", "county", "mailing_address_line1",
                   "mailing_address_line2", "mailing_city", "mailing_state", "mailing_postal_code",
                   "birth_city", "birth_state", "emergency_contact_relationship", "eligible_for_rehire"]
        widgets={name:forms.DateInput(attrs={"type":"date"}) for name in ("hire_date","termination_date","date_of_birth")}
        widgets["status"] = forms.Select(attrs={"data-personnel-status": ""})
        widgets["termination_date"] = forms.DateInput(attrs={"type": "date", "data-termination-date": ""})
        widgets["eligible_for_rehire"] = forms.NullBooleanSelect()
        widgets["eligible_for_rehire"].choices = (("unknown", "Not recorded"), ("true", "Yes"), ("false", "No"))
        labels = {
            "employee_id": "Employee ID", "address_line1": "Address line 1", "address_line2": "Address line 2",
            "is_unarmed_officer": "Unarmed officer", "is_commissioned_officer": "Commissioned officer",
            "is_ppo": "Personal protection officer (PPO)", "is_private_investigator": "Private investigator",
            "is_shareholder": "Shareholder",
            "first_name": "Legal first name", "last_name": "Legal last name", "name_suffix": "Suffix",
            "postal_code": "ZIP / postal code", "mailing_postal_code": "Mailing ZIP / postal code",
            "address_line1": "Home address (number and street)", "job_title": "Position / job title",
        }
        help_texts = {
            "mailing_address_line1": "Leave the mailing address blank when it is the same as the home address.",
            "termination_date": "Required when status is Terminated. Status changes take effect when saved, not automatically on this date.",
            "status": "Terminated personnel leave operational rosters. Linked team-user access is managed separately in Team access.",
            "eligible_for_rehire": "Optional employment decision. Leave blank if not assessed; this does not change status or team access.",
        }

class SelfContactForm(WorkflowModelForm):
    """What an employee may correct about themselves. Identity, employment, pay and licensing stay HR's."""
    field_sections = (
        ("Contact", ("email", "mobile_phone", "address_line1", "address_line2", "city", "county", "state", "postal_code")),
        ("Mailing address (if different)", ("mailing_address_line1", "mailing_address_line2", "mailing_city", "mailing_state", "mailing_postal_code")),
        ("Emergency contact", ("emergency_contact_name", "emergency_contact_relationship", "emergency_contact_phone")),
    )
    class Meta:
        model = Person
        fields = ["email", "mobile_phone", "address_line1", "address_line2", "city", "state", "postal_code",
                  "emergency_contact_name", "emergency_contact_phone", "emergency_contact_relationship", "county",
                  "mailing_address_line1", "mailing_address_line2", "mailing_city", "mailing_state", "mailing_postal_code"]
        labels = {"email": "Personal email", "address_line1": "Address line 1", "address_line2": "Address line 2"}
        help_texts = {
            "email": "Where the company reaches you. Your sign-in email is changed separately under Sign-in & security.",
            "mobile_phone": "A new number needs a fresh yes before the company texts it.",
        }

class CustomFieldDefinitionForm(WorkflowModelForm):
    class Meta:
        model=CustomFieldDefinition
        fields=["name","key","kind","required","sensitive","active"]

class DocumentAcknowledgmentForm(WorkflowForm):
    confirm=forms.BooleanField(label="I acknowledge that I reviewed this document")
    signature_name=forms.CharField(required=False,max_length=160,help_text="Required when the document type requests a signature.")

class CompanyPostOrdersForm(WorkflowModelForm):
    class Meta:
        model = Organization
        fields = ["default_post_orders"]
        widgets = {"default_post_orders": forms.Textarea(attrs={"rows": 8})}


class ClientForm(InheritedPostOrdersForm):
    class Meta:
        model = Client
        fields = ["name", "contact_name", "contact_email", "default_post_orders", "required_credentials", "default_pay_rate", "default_bill_rate", "default_pay_code", "active"]
        widgets = {"required_credentials": forms.CheckboxSelectMultiple(), "default_post_orders": forms.Textarea(attrs={"rows": 5})}
        help_texts = {
            "required_credentials": "Every officer standing any post for this client must hold these.",
            "default_bill_rate": "What the client is charged per hour, unless a site or post says otherwise.",
            "default_pay_code": "The job code every post under this contract is paid under, unless a site or post says otherwise.",
        }

class SiteForm(InheritedPostOrdersForm):
    orders_parent_field = "client"

    class Meta:
        model = Site
        fields = ["client", "branch", "name", "address", "default_post_orders", "latitude", "longitude", "geofence_radius_meters", "required_credentials", "default_pay_rate", "default_bill_rate", "default_pay_code", "active"]
        widgets = {"required_credentials": forms.CheckboxSelectMultiple(), "default_post_orders": forms.Textarea(attrs={"rows": 5})}
        help_texts = {
            "required_credentials": "Post requirements for this site, on top of the client's.",
            "default_bill_rate": "What the client is charged per hour at this site, unless a post says otherwise.",
            "default_pay_code": "Overrides the contract's code for every post at this site.",
        }

class CredentialTypeForm(WorkflowModelForm):
    applies_to = forms.MultipleChoiceField(choices=PERSONNEL_CATEGORIES, required=False, widget=forms.CheckboxSelectMultiple(), help_text="Only people in the selected categories are required to hold this credential.")
    reminder_days_before = forms.CharField(required=False, label="Reminder lead times (days)", help_text="Escalating notices before expiry, comma-separated — for example 90, 60, 30, 7. Leave empty to warn once, at the warning window.")
    # NTF-2. Both halves of the escalation sit on the requirement, beside the ladder they extend, so
    # one row answers "what do we warn about and who hears about it when the warning is ignored".
    # A separate notification-settings object would let the two disagree and the disagreement would
    # only surface in a dispute.
    escalate_to = forms.MultipleChoiceField(required=False, choices=Membership.Role.choices,
        widget=forms.CheckboxSelectMultiple(), label="Escalate to",
        help_text="Roles pulled in when a rung passes with nothing recorded. Leave empty for no escalation.")
    class Meta:
        model = CredentialType
        fields = ["name", "code", "jurisdiction", "authority_url", "authority_reference", "interpretation", "effective_from", "effective_until", "blocks_scheduling", "blocks_clock_in", "warning_days", "reminder_days_before", "escalate_after_days", "escalate_to", "evidence_required", "registry_check_within_days", "applies_to", "active"]
        widgets = {"effective_from": forms.DateInput(attrs={"type":"date"}), "effective_until": forms.DateInput(attrs={"type":"date"}), "interpretation": forms.Textarea(attrs={"rows":4})}
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.reminder_days_before:
            self.initial = {**(self.initial or {}), "reminder_days_before": ", ".join(str(item) for item in self.instance.reminder_days_before)}

    def clean_reminder_days_before(self):
        raw = self.cleaned_data.get("reminder_days_before", "")
        levels = []
        for part in str(raw).replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                days = int(part)
            except ValueError:
                raise forms.ValidationError(f"“{part}” is not a number of days.")
            if days < 1 or days > 3650:
                raise forms.ValidationError("Reminder lead times must be between 1 and 3650 days.")
            if days not in levels:
                levels.append(days)
        return levels

    def clean(self):
        data=super().clean()
        if data.get("effective_from") and data.get("effective_until") and data["effective_until"] < data["effective_from"]:
            self.add_error("effective_until", "End date must not precede the effective date.")
        return data

class ComplianceRuleForm(WorkflowModelForm):
    """Enter a duty that is not a credential, with the same evidence an approver needs."""

    applies_to = forms.MultipleChoiceField(choices=PERSONNEL_CATEGORIES, required=False, widget=forms.CheckboxSelectMultiple(), help_text="Only officers in the selected categories must satisfy this rule.")
    reminder_days_before = forms.CharField(required=False, label="Reminder lead times (days)", help_text="Escalating notices before the evidence expires, comma-separated — for example 90, 60, 30, 7. Leave empty to warn once, at the warning window.")
    class Meta:
        model = ComplianceRule
        fields = ["name", "code", "evidence", "applies_to_subject", "applies_to", "document_type", "required_hours",
                  "jurisdiction", "authority_url", "authority_reference", "interpretation",
                  "effective_from", "effective_until", "warning_days", "reminder_days_before", "active"]
        widgets = {"effective_from": forms.DateInput(attrs={"type":"date"}), "effective_until": forms.DateInput(attrs={"type":"date"}), "interpretation": forms.Textarea(attrs={"rows":4})}
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.reminder_days_before:
            self.initial = {**(self.initial or {}), "reminder_days_before": ", ".join(str(item) for item in self.instance.reminder_days_before)}

    def clean_reminder_days_before(self):
        raw = self.cleaned_data.get("reminder_days_before", "")
        levels = []
        for part in str(raw).replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                days = int(part)
            except ValueError:
                raise forms.ValidationError(f"“{part}” is not a number of days.")
            if days < 1 or days > 3650:
                raise forms.ValidationError("Reminder lead times must be between 1 and 3650 days.")
            if days not in levels:
                levels.append(days)
        return levels

    def clean(self):
        data=super().clean()
        if data.get("effective_from") and data.get("effective_until") and data["effective_until"] < data["effective_from"]:
            self.add_error("effective_until", "End date must not precede the effective date.")
        # An approval is a statement that the rule is enforceable, so it must not be grantable on a
        # row that names no evidence to enforce against — the same reason `is_approved` refuses.
        if data.get("evidence")==ComplianceRule.Evidence.DOCUMENT and not data.get("document_type"):
            self.add_error("document_type", "Name the record type that proves this duty, or the queue cannot tell who is missing it.")
        return data

class PersonBoundForm:
    """Bind a child record to the person whose profile opened the form.

    Dropping the field is what makes the binding safe: a record created from a personnel
    profile cannot name a different person, so the tenant check in the model's ``clean()``
    is not the only thing keeping the row on the right person.
    """

    def bind_person(self, person):
        self.bound_person = person
        self.fields.pop("person", None)

    def _apply_bound_person(self, instance):
        if getattr(self, "bound_person", None) is not None:
            instance.person = self.bound_person
        return instance


class CredentialForm(PersonBoundForm, WorkflowModelForm):
    class Meta:
        model = Credential
        fields = ["person", "credential_type", "number", "status", "issued_on", "expires_on",
                  "handgun_qualification", "shotgun_qualification", "notes"]
        widgets = {"issued_on": forms.DateInput(attrs={"type": "date"}), "expires_on": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bound_person = None

    def save(self, commit=True):
        instance = self._apply_bound_person(super().save(commit=False))
        if commit:
            instance.save()
        return instance

class ShiftForm(InheritedPostOrdersForm):
    orders_field = "post_orders"
    orders_parent_field = "site"

    field_sections = (
        ("Assignment", ("site", "post_name", "officer", "status", "starts_at", "ends_at")),
        ("Requirements & instructions", ("required_credentials", "post_orders")),
        ("Pay & billing overrides", ("pay_rate", "bill_rate", "pay_code")),
        ("Split tour", ("relief_for",)),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, label in (("officer", "Unassigned / open post"), ("relief_for", "Not a split tour")):
            field = self.fields[name]
            if isinstance(field, forms.ModelChoiceField):
                field.empty_label = label

    class Meta:
        model = Shift
        fields = ["site", "officer", "starts_at", "ends_at", "status", "post_name", "post_orders", "required_credentials", "pay_rate", "bill_rate", "pay_code", "relief_for"]
        widgets = {
            "starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "ends_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "required_credentials": forms.CheckboxSelectMultiple(),
            "post_orders": forms.Textarea(attrs={"rows": 4}),
        }
        labels = {"starts_at": "Shift start", "ends_at": "Shift end", "relief_for": "Tour being relieved"}
        help_texts = {
            "officer": "Leave empty to publish the post as open. Officers who qualify can ask to take it and a dispatcher approves the fill.",
            "pay_rate": "Only needed when this post pays differently from the site, contract, or officer rate.",
            "bill_rate": "Only needed when this post bills differently from the site or contract rate.",
            "pay_code": "Only needed when this post is charged to a different job code than its site or contract.",
            # The split tour is the normal shape of a long post, so it is entered where the post is
            # made — a fact nobody records at 01:00 is a fact nobody reconstructs at payroll.
            "relief_for": "Only for a half that takes over another officer's tour, or follows it at the same post. Leave empty for an ordinary post.",
        }

class ShiftTemplateForm(InheritedPostOrdersForm):
    orders_field = "post_orders"
    orders_parent_field = "site"

    """A repeating tour, stated the way a contract describes one: a post, a window, and days.

    No rate appears here on purpose. Each generated post resolves its own pay and bill rate from
    the site, contract, and officer when the schedule is read, so a contract renegotiated in
    November changes December's posts; a series that carried a rate would freeze June's number
    onto every row it had already produced.

    A rotating series is described as "day 1, 2, 5, 6, 7, 10, 11 of a 14-day cycle" rather than as
    weekdays, because that is the only way 2-2-3 and 4-on/4-off can be written down at all — an
    eight-day cycle drifts one weekday forward every round, so a weekday-anchored rule describes a
    different roster each week. The preset list is a convenience over the same two numbers, not a
    vocabulary: the industry does not agree on what to call these.
    """
    field_sections = (
        ("Assignment", ("name", "alias", "site", "officer", "post_name", "active")),
        ("Repeating pattern", ("start_time", "end_time", "pattern", "weekdays", "preset",
                              "cycle_days", "cycle_work_days", "series_start", "series_end")),
        ("Requirements & instructions", ("required_credentials", "post_orders")),
    )
    weekdays = forms.MultipleChoiceField(choices=WEEKDAY_CHOICES, required=False, widget=forms.CheckboxSelectMultiple(),
        help_text="Which days of the week a weekly or fortnightly series produces.")
    cycle_work_days = forms.CharField(required=False, label="Worked days of the cycle",
        help_text="Counted from the first day of the series, starting at 1 — for 2-2-3 that is 1,2,5,6,7,10,11. Pick a preset to fill this in.")
    preset = forms.ChoiceField(required=False, choices=(("", "— choose a named rotation —"),),
        help_text="Sets the cycle length and worked days above. Edit either field afterwards if the firm runs a variant.")

    class Meta:
        model = ShiftTemplate
        fields = ["name", "alias", "site", "officer", "start_time", "end_time", "pattern", "weekdays",
                  "preset", "cycle_days", "cycle_work_days", "series_start", "series_end",
                  "post_name", "post_orders", "required_credentials", "active"]
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
            "series_start": forms.DateInput(attrs={"type": "date"}),
            "series_end": forms.DateInput(attrs={"type": "date"}),
            "required_credentials": forms.CheckboxSelectMultiple(),
            "post_orders": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "officer": "Leave empty to generate every post open, so a qualified officer can ask to take it.",
            "series_start": "A weekly series is read from the Monday of this week; a rotating cycle counts its days from this date, so a second crew on the same post is the same cycle starting a few days later.",
            "series_end": "Leave empty for a standing contract. When set, the series generates nothing after this date and the generate screen can run to it in one action.",
            "required_credentials": "Added to each generated post, on top of what the contract and site already require.",
            "cycle_days": "Days before the pattern repeats: 7, 8, 14, 21 or 28 cover the usual rotations.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["preset"].choices = [("", "— choose a named rotation —"),
            *((label, f"{label} — {note}") for label, _cycle, _work, note in ROTATING_PRESETS),
            ("custom", "Neither — I will set the days")]
        if self.instance and self.instance.pk and self.instance.cycle_work_days:
            self.initial = {**(self.initial or {}),
                "cycle_work_days": ", ".join(str(value + 1) for value in self.instance.cycle_work_indexes)}

    def clean_weekdays(self):
        raw = self.cleaned_data.get("weekdays") or []
        try:
            return sorted({int(value) for value in raw})
        except (TypeError, ValueError):
            raise forms.ValidationError("Choose days of the week.")

    def clean_cycle_work_days(self):
        raw = str(self.cleaned_data.get("cycle_work_days", "")).replace(";", ",")
        days = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                raise forms.ValidationError(f"“{part}” is not a day number.")
            if value < 1:
                raise forms.ValidationError("Count the days of the cycle from 1.")
            days.append(value - 1)
        return sorted(set(days))

    def clean(self):
        data = super().clean()
        preset = data.get("preset")
        if preset and preset != "custom":
            for label, cycle, work, _note in ROTATING_PRESETS:
                if label == preset:
                    data["cycle_days"], data["cycle_work_days"] = cycle, list(work)
                    break
        if data.get("start_time") and data.get("start_time") == data.get("end_time"):
            self.add_error("end_time", "A tour cannot end at the same minute it starts.")
        if data.get("pattern") == ShiftTemplate.Pattern.CYCLE:
            if not data.get("cycle_days"):
                self.add_error("cycle_days", "A rotating series needs a cycle length in days.")
            elif not data.get("cycle_work_days"):
                self.add_error("cycle_work_days", "Say which days of the cycle are worked.")
            elif any(value >= data["cycle_days"] for value in data["cycle_work_days"]):
                self.add_error("cycle_work_days",
                    f"Day numbers must be within the {data['cycle_days']}-day cycle.")
        elif not data.get("weekdays"):
            self.add_error("weekdays", "Choose at least one day, or set the pattern to a rotating cycle.")
        return data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if instance.pattern != ShiftTemplate.Pattern.CYCLE:
            instance.cycle_days, instance.cycle_work_days = None, []
        else:
            instance.weekdays = []
        if commit:
            instance.save()
            self.save_m2m()
        return instance

class ShiftGenerationForm(WorkflowForm):
    """Which dates of a series to produce, and whether they land as drafts or as published posts."""
    range_start = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}),
        help_text="The first day to generate. Posts are produced for every pattern day up to the day after this one.")
    range_end = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}),
        help_text=f"At most {SERIES_MAX_DAYS} days after the start, so one run stays a roster and not a data import.")
    status = forms.ChoiceField(choices=[(Shift.Status.DRAFT, "Draft — held on the schedule without telling anyone"),
                                         (Shift.Status.PUBLISHED, "Published — officers and open-post candidates are told")],
        initial=Shift.Status.DRAFT,
        help_text="Drafts hold the coverage without announcing it; publishing an open post offers it to every officer who qualifies.")

    def clean(self):
        data = super().clean()
        start, end = data.get("range_start"), data.get("range_end")
        if start and end:
            if end < start:
                self.add_error("range_end", "The last day cannot precede the first.")
            elif (end - start).days > SERIES_MAX_DAYS:
                self.add_error("range_end",
                    f"Generate at most {SERIES_MAX_DAYS} days at a time ({(end - start).days + 1} here).")
        return data

class ShiftSwapForm(WorkflowForm):
    """Ask one colleague to take a post the officer is already scheduled to stand."""
    replacement = forms.ModelChoiceField(queryset=Person.objects.none(),
        label="Ask which colleague",
        help_text="They decide first, then a manager whose authority covers the post approves the move. "
                  "Their credentials are checked again at that approval, not only here.")
    note = forms.CharField(max_length=255, required=False,
        help_text="Optional reason. Both the colleague and the manager read it.")

class ShiftExchangeForm(WorkflowForm):
    """Propose a two-way trade with one named colleague."""
    partner = forms.ModelChoiceField(queryset=Person.objects.none(), label="Trade with which colleague",
        help_text="They choose which of their own posts to put in — proposing a trade does not let you "
                  "pick a colleague's shift for them.")
    note = forms.CharField(max_length=255, required=False,
        help_text="Optional reason. Both the colleague and the manager read it.")

class ExchangeAcceptForm(WorkflowForm):
    """The colleague answers a trade by putting one of their own future posts into it."""
    partner_shift = forms.ModelChoiceField(queryset=Shift.objects.none(), label="Which of your posts do you trade",
        help_text="The manager sees both posts, each officer's hours before and after, and whether either "
                  "would qualify for the other's post, before anything moves.")
    note = forms.CharField(max_length=255, required=False)

class ClockKioskForm(WorkflowModelForm):
    """Standing a shared clock station at a post (CLK-2)."""
    class Meta:
        model = ClockKiosk
        fields = ["name", "site"]
        help_texts = {
            "name": "What the officers see on the screen — 'Guard shack 2', not 'Tablet ABC'.",
            "site": "Where the station physically stands. A punch taken here is placed at this post.",
        }

class TextAlertsForm(SmsConsentFormMixin, WorkflowForm):
    """The officer's own text-message decision (NTF-4), number and consent in one submit.

    Split from ``PersonForm`` because the two are answered by different people for different reasons:
    a manager may correct a phone number without touching the consent on file for it, and the officer
    who opts in has to be shown the wording their answer is about. Asking for a number the record
    already holds would be the friction that makes people skip the screen — so it is prefilled, and
    *changing* it is what invalidates the old consent rather than a checkbox.
    """
    mobile_phone = forms.CharField(max_length=30, required=False, label="Mobile number",
        help_text="The number that receives texts. Consent belongs to the number, not to you: "
                  "if you change it, the new one has to be opted in again.")
    opt_in = forms.BooleanField(required=False, label="Text me about my posts, clock reminders and timecard questions")

    def clean_mobile_phone(self):
        value = str(self.cleaned_data.get("mobile_phone") or "").strip()
        if value and not normalize_destination(value, MessageConsent.Channel.SMS):
            raise forms.ValidationError("That is not a usable mobile number.")
        return value

    def clean(self):
        data = super().clean()
        if data.get("opt_in") and not data.get("mobile_phone"):
            self.add_error("mobile_phone", "A number is needed before texts can be sent to you.")
        return data

class ClockPinForm(WorkflowForm):
    """Set or change a clock PIN (CLK-2), used by the officer and by a supervisor alike.

    One form for both paths rather than two that can disagree about what a valid PIN is: the shape
    rules live in `services.validate_clock_pin`, so the station that checks a PIN and the screen that
    issues one cannot end up trusting different things. A supervisor issuing a PIN does not have the
    officer's current one, which is why `ask_current` is a parameter and not a second class.
    """
    current_pin = forms.CharField(required=False, strip=False, widget=forms.PasswordInput(render_value=False, attrs={"autocomplete":"off","inputmode":"numeric"}),
        label="Current PIN")
    pin = forms.CharField(strip=False, widget=forms.PasswordInput(render_value=False, attrs={"autocomplete":"new-password","inputmode":"numeric"}),
        label="New clock PIN", help_text="4 to 8 digits. Not one digit repeated, and not a run like 1234.")
    confirm = forms.CharField(strip=False, widget=forms.PasswordInput(render_value=False, attrs={"autocomplete":"new-password","inputmode":"numeric"}), label="Type it again")

    def __init__(self, *args, ask_current=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not ask_current:
            del self.fields["current_pin"]

    def clean_pin(self):
        try:
            return validate_clock_pin(self.cleaned_data.get("pin"))
        except forms.ValidationError as exc:
            raise forms.ValidationError(exc.messages[0], code="invalid")

    def clean(self):
        data = super().clean()
        if data.get("pin") and data.get("confirm") and data["pin"] != data["confirm"]:
            self.add_error("confirm", "The two PINs do not match.")
        return data

class PersonnelAssignmentsForm(WorkflowForm):
    clients = forms.ModelMultipleChoiceField(queryset=Client.objects.none(), required=False,
        widget=forms.CheckboxSelectMultiple, label="Clients (all their sites)")
    sites = forms.ModelMultipleChoiceField(queryset=Site.objects.none(), required=False,
        widget=forms.CheckboxSelectMultiple, label="Individual sites")

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["clients"].queryset = organization.clients.filter(active=True).order_by("name")
        self.fields["sites"].queryset = organization.sites.filter(active=True, client__active=True).select_related(
            "client").order_by("client__name", "name")
        self.fields["sites"].label_from_instance = lambda site: f"{site.client.name} / {site.name}"

    def clean(self):
        data = super().clean()
        if not self.errors and not data.get("clients") and not data.get("sites"):
            raise forms.ValidationError("Choose at least one client or site.")
        return data


class TimePolicyForm(WorkflowModelForm):
    field_sections = (
        ("Workweek and payroll", ("timezone", "workweek_start", "pay_period_weeks", "pay_period_anchor", "overtime_after_hours", "rounding_mode", "rounding_minutes", "allow_reopen")),
        ("Clock evidence", ("require_geofence", "allow_kiosk", "flag_spoof_risk", "require_selfie")),
        ("Live attendance", ("arrival_alert_enabled", "arrival_grace_minutes", "departure_alert_enabled", "departure_grace_minutes")),
    )
    workweek_start = forms.TypedChoiceField(
        choices=WEEKDAY_CHOICES, coerce=int, label="Workweek starts on",
        help_text="Overtime is counted from this day.",
    )
    rounding_minutes = forms.TypedChoiceField(
        choices=[(value, f"{value} minute{'s' if value != 1 else ''}") for value in (1, 5, 6, 10, 15, 30)],
        coerce=int, label="Rounding interval",
    )
    class Meta:
        model = TimePolicy
        fields = ["timezone", "workweek_start", "overtime_after_hours", "rounding_mode", "rounding_minutes", "require_geofence", "allow_kiosk", "flag_spoof_risk", "require_selfie", "allow_reopen",
                  "arrival_alert_enabled", "arrival_grace_minutes", "departure_alert_enabled", "departure_grace_minutes",
                  "pay_period_weeks", "pay_period_anchor"]
        widgets = {"pay_period_anchor": forms.DateInput(attrs={"type": "date"})}
        labels = {"arrival_alert_enabled": "Late-arrival alerts", "departure_alert_enabled": "Overdue-departure alerts",
                  "arrival_grace_minutes": "Arrival grace (minutes)", "departure_grace_minutes": "Departure grace (minutes)"}
        help_texts = {
            "workweek_start": "Monday is 0; Sunday is 6. Overtime is counted from this day.",
            "allow_reopen": "Off means an approved period stays locked for ever: reopening needs this switch, an owner or administrator, and a written reason.",
            # The switch is explained as what it is — a review decision, not a gate — because an
            # operator who fears it will strand officers at a gate turns it off and never looks again.
            "flag_spoof_risk": "Send a punch whose location reading is impossible (a 0-metre fix, or a 60 km gap crossed in 5 minutes) to time review. It never blocks the clock; the time is still recorded.",
            # What the photo is for, what it costs the officer, and where it goes — a face is the one
            # clock setting a person can object to on their own behalf, so the screen says what it
            # does rather than leaving it to be inferred from a checkbox label.
            "require_selfie": "Ask for a photo at clock-in and clock-out. The officer's own device camera, one tap, no upload of anything else. "
                              "A punch without one is still recorded and goes to time review. Frames are held as personnel records "
                              "under their own retention window (90 days to start, editable, blank keeps them permanently) and opened by "
                              "the officer, owner, administrator, HR and dispatch — not by an outside auditor. "
                              "Not asked for at a shared station that has already verified the officer's PIN.",
            "arrival_grace_minutes": "Minutes after scheduled start before raising a missing clock-in case: 0 through 240.",
            "departure_grace_minutes": "Minutes after scheduled or authorized hold-over end before raising a missing clock-out case: 0 through 240.",
        }

    def __init__(self, *args, **kwargs):
        # Existing API/form clients omitted this newly added default; preserve their weekly policy.
        if args and args[0] is not None:
            data = args[0].copy()
            instance = kwargs.get("instance")
            if "pay_period_weeks" not in data:
                data["pay_period_weeks"] = str(instance.pay_period_weeks if instance else 1)
            if "pay_period_anchor" not in data and instance and instance.pay_period_anchor:
                data["pay_period_anchor"] = instance.pay_period_anchor.isoformat()
            args = (data, *args[1:])
        super().__init__(*args, **kwargs)

class TimePolicyOverrideForm(WorkflowModelForm):
    """One contract's or property's deviation, stated as a delta rather than a copy.

    Every field carries an explicit "inherit" so an override can change a single thing. A form
    that prefilled a full copy of the company rule would look convenient and then quietly
    freeze it: the site would stop following the baseline the moment the baseline moved, with
    nobody left to explain why.
    """
    field_sections = (
        ("Applies to", ("client", "site")),
        ("Clock evidence and rounding", ("require_geofence", "allow_kiosk", "flag_spoof_risk", "require_selfie", "rounding_mode", "rounding_minutes")),
        ("Live attendance", ("arrival_alert_enabled", "arrival_grace_minutes", "departure_alert_enabled", "departure_grace_minutes")),
    )
    INHERIT = ("", "Inherit from client/company")
    require_geofence = forms.ChoiceField(required=False, label="Geofence requirement", choices=(
        INHERIT, ("yes", "Require location inside the site fence"), ("no", "Do not require a fence")))
    # CLK-2. Same three-state shape, for the same reason: "no kiosk here" and "not decided" are
    # different answers and a checkbox can only hold one of them.
    allow_kiosk = forms.ChoiceField(required=False, label="Shared clock station", choices=(
        INHERIT, ("yes", "Allow a shared kiosk at this level"), ("no", "Forbid a shared kiosk; each officer clocks from their own device")))
    rounding_mode = forms.ChoiceField(required=False, label="Rounding",
        choices=(INHERIT, *TimePolicy.RoundingMode.choices))
    rounding_minutes = forms.TypedChoiceField(required=False, label="Rounding interval", coerce=int, empty_value=None,
        choices=(INHERIT, *((str(value), f"{value} minutes") for value in (1, 5, 6, 10, 15, 30))))
    # CLK-4. The third field on this row that must be able to say "not decided here". A contract whose
    # client reviews spoof signals itself is the case: inheriting would either spam this company's
    # queue or silently stop looking, and a checkbox cannot tell those two answers apart.
    flag_spoof_risk = forms.ChoiceField(required=False, label="Spoof-risk review", choices=(
        INHERIT, ("yes", "Send impossible location readings to time review"),
        ("no", "Record the reading, do not raise it for review")))
    # CLK-1, the fourth tri-state. A contract that has agreed a PIN-verified station should be able to
    # say "no photo here" without waiting for the company baseline, and a site that wants one should be
    # able to say yes while the contract says nothing.
    require_selfie = forms.ChoiceField(required=False, label="Clock photo", choices=(
        INHERIT, ("yes", "Require a photo at clock-in and clock-out"),
        ("no", "Do not require a photo at this level")))
    arrival_alert_enabled = forms.ChoiceField(required=False, label="Late-arrival alerts", choices=(
        ("", "Inherit from client/company"), ("yes", "Enable"), ("no", "Disable")))
    departure_alert_enabled = forms.ChoiceField(required=False, label="Overdue-departure alerts", choices=(
        ("", "Inherit from client/company"), ("yes", "Enable"), ("no", "Disable")))

    class Meta:
        model = TimePolicyOverride
        fields = ["client", "site", "require_geofence", "allow_kiosk", "flag_spoof_risk", "require_selfie", "rounding_mode", "rounding_minutes",
                  "arrival_alert_enabled", "arrival_grace_minutes", "departure_alert_enabled", "departure_grace_minutes"]
        help_texts = {
            "client": "Every post under this contract, unless a single site says otherwise.",
            "site": "This post only. A site rule beats the contract's and the company's.",
        }

    def clean_require_geofence(self):
        # "" means inherit and None is what the model stores for "not decided here" — a plain
        # BooleanField would have made "false" and "inherit" indistinguishable.
        return self._tri_state("require_geofence")

    def clean_allow_kiosk(self):
        return self._tri_state("allow_kiosk")

    def clean_flag_spoof_risk(self):
        return self._tri_state("flag_spoof_risk")

    def clean_require_selfie(self):
        return self._tri_state("require_selfie")

    def clean_arrival_alert_enabled(self):
        return self._tri_state("arrival_alert_enabled")

    def clean_departure_alert_enabled(self):
        return self._tri_state("departure_alert_enabled")

    def _tri_state(self, name):
        value = self.cleaned_data.get(name)
        if value in (None, ""):
            return None
        return value == "yes"

    def clean(self):
        data = super().clean()
        if (data.get("rounding_mode") or "") and not data.get("rounding_minutes"):
            self.add_error("rounding_minutes", "Choose an interval, or set no rounding at all.")
        if data.get("rounding_minutes") and not data.get("rounding_mode"):
            self.add_error("rounding_mode", "Choose how to round, or leave both on inherit.")
        return data

class ChannelRuleForm(WorkflowModelForm):
    """One audience's outbound channels for one notice family (NTF-1).

    The organisation is not on the form: the view sets it from the signed-in tenant, so a crafted POST
    cannot write another company's rules. What *is* on the form is the choice the model cannot make —
    in-app is deliberately absent from `channels`, because that copy arrives whatever is chosen here.
    """
    OUTBOUND = [(value, label) for value, label in Notification.Channel.choices
                if value != Notification.Channel.IN_APP]
    channels = forms.MultipleChoiceField(required=False, choices=OUTBOUND,
        widget=forms.CheckboxSelectMultiple(), label="Send these notices on",
        help_text="Choose neither to keep this audience in the app only. In-app always arrives.")
    event_type = forms.CharField(required=False, max_length=100, label="One specific notice",
        help_text="Optional. For example <code>credential.escalated</code> on its own, instead of the whole family. Must start with the family above.")
    # A BooleanField derived from the model would be `required=True`, and an unchecked box posts
    # nothing — so a rule added by somebody who never touched the field would be born paused. Declared
    # with an initial and required=False, the box renders ticked and only a deliberate uncheck pauses.
    active = forms.BooleanField(required=False, initial=True, label="Turned on",
        help_text="Uncheck to keep the rule and stop it delivering. The row stays, so a paused rule is readable.")

    class Meta:
        model = ChannelRule
        fields = ["audience", "family", "event_type", "channels", "active"]

    def clean_event_type(self):
        return str(self.cleaned_data.get("event_type") or "").strip()

    def clean(self):
        data = super().clean()
        organization_id = self.instance.organization_id
        if organization_id and data.get("audience") and data.get("family"):
            clash = ChannelRule.objects.filter(organization_id=organization_id,
                audience=data["audience"], family=data["family"],
                event_type=data.get("event_type") or "").exclude(pk=self.instance.pk).first()
            if clash:
                # The database will refuse this too, and a 500 page is not how an operator should learn
                # they already wrote the rule — MySQL enforces the unconditional unique, sqlite enforces
                # the constraint, and neither tells them which screen to go back to.
                self.add_error("family", f"A rule for {data['audience']} and {data.get('event_type') or data['family'] + '.*'} already exists. Edit that one instead of adding a second.")
        return data

class OrganizationSecurityForm(WorkflowModelForm):
    mfa_required_roles = forms.MultipleChoiceField(choices=Membership.Role.choices,required=False,widget=forms.CheckboxSelectMultiple())
    # AUTH-3, as one text box rather than a widget that invents a schema: the thing an owner types is a
    # list of domains, and normalizing it here (lowercase, no leading @, any separator) is what keeps
    # "GuardCo.com" from silently failing to match a real address later.
    role_domains = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows":2,"cols":40}),
        label="Approved identity domains",
        help_text="Domains allowed to hold any role above Officer — one per line or comma separated. "
                  "Leave empty to allow any identity. Officers can always accept on a personal address.")

    def clean_role_domains(self):
        raw = self.cleaned_data.get("role_domains") or ""
        parts = [item.strip().lstrip("@").lower() for item in re.split(r"[\s,;]+", raw) if item.strip()]
        for item in parts:
            if item.count("@") or "." not in item:
                raise forms.ValidationError(f"“{item}” is not an email domain.")
        # Sorted and de-duplicated so saving the same list twice does not read as a policy change.
        return sorted(set(parts))

    def save(self, commit=True):
        organization = super().save(commit=False)
        organization.approved_role_domains = self.cleaned_data["role_domains"]
        if commit:
            organization.save()
        return organization

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance is not None:
            self.initial = {**(self.initial or {}),
                            "role_domains": "\n".join(self.instance.approved_role_domains or [])}

    class Meta:
        model=Organization
        fields=["mfa_required_roles"]

class DocumentUploadForm(PersonBoundForm, WorkflowForm):
    person = forms.ModelChoiceField(queryset=Person.objects.none(), required=False, help_text="Leave empty to file a company record, such as a handbook or licence, that is not part of a personnel file.")
    document_type = forms.ModelChoiceField(queryset=DocumentType.objects.none())
    file = forms.FileField()
    expires_on = forms.DateField(required=False, widget=forms.DateInput(attrs={"type":"date"}))
    revises = forms.ModelChoiceField(queryset=PersonDocument.objects.none(), required=False, label="Replaces which version",
        help_text="Pick the current version when this re-issues it, so the signatures already given stay attached to the version they were given for. Leave empty to file a separate record.")
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bound_person = None
    def clean(self):
        data=super().clean()
        person=getattr(self,"bound_person",None) or data.get("person")
        document_type=data.get("document_type")
        if document_type:
            if person is None and document_type.audience==DocumentType.Audience.PERSON:
                self.add_error("person","This record type belongs in a personnel file. Choose a person, or make it a company-record type in settings.")
            elif person is not None and document_type.audience!=DocumentType.Audience.PERSON:
                self.add_error("person","Company records are not filed against a person.")
        revises=data.get("revises")
        if revises is not None:
            if document_type is not None and revises.document_type_id != document_type.pk:
                self.add_error("revises","A revision replaces a record of the same type.")
            if (person is None) != (revises.person_id is None) or (person is not None and revises.person_id != person.pk):
                self.add_error("revises","A revision replaces the record filed against the same person, or the same company-wide record.")
        return data

class DocumentTypeForm(WorkflowModelForm):
    class Meta:
        model = DocumentType
        fields = ["name","code","audience","sensitivity","retention_days","acknowledgment_required","signature_required","active"]

class TrainingRecordForm(PersonBoundForm, WorkflowModelForm):
    class Meta:
        model = TrainingRecord
        fields = ["person","course_name","provider","completed_on","expires_on","certificate_number","hours"]
        widgets = {"completed_on":forms.DateInput(attrs={"type":"date"}),"expires_on":forms.DateInput(attrs={"type":"date"})}
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bound_person = None
    def save(self, commit=True):
        instance = self._apply_bound_person(super().save(commit=False))
        if commit:
            instance.save()
        return instance

class CsvImportForm(WorkflowForm):
    entity = forms.ChoiceField(choices=ImportBatch.Entity.choices)
    file = forms.FileField(help_text="UTF-8 CSV, maximum 50 MiB and 10,000 rows.")

class PunchAdjustmentForm(WorkflowForm):
    proposed_at = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type":"datetime-local"}))
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows":3}), min_length=5)

class PayrollPeriodForm(WorkflowForm):
    period_start = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type":"datetime-local"}))
    period_end = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type":"datetime-local"}))
    def clean(self):
        data=super().clean()
        if data.get("period_start") and data.get("period_end") and data["period_end"]<=data["period_start"]: raise forms.ValidationError("Period end must be after start.")
        return data

class DispositionRequestForm(WorkflowForm):
    action = forms.ChoiceField(choices=DispositionRequest.Action.choices)
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows":3}),min_length=10)

class DomainForm(WorkflowForm):
    hostname = forms.CharField(max_length=253,help_text="Example: portal.company.com")
    def clean_hostname(self):
        import re
        value=self.cleaned_data["hostname"].strip().rstrip(".").lower()
        if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}",value): raise forms.ValidationError("Enter a valid fully-qualified hostname.")
        return value

class AuditRedactionForm(WorkflowForm):
    fields = forms.CharField(help_text="Comma-separated metadata keys to redact from exports.")
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows":3}),min_length=10)
    legal_basis = forms.CharField(max_length=255,min_length=5)
    def clean_fields(self):
        fields=[item.strip() for item in self.cleaned_data["fields"].split(",") if item.strip()]
        if not fields:raise forms.ValidationError("Enter at least one metadata key.")
        return fields


class OnboardingItemForm(WorkflowModelForm):
    """A new-hire step. The evidence it names is chosen, not typed, so the step can be checked.

    A step of kind ``document`` or ``credential`` without the matching type named would be a box to
    tick with nothing behind it, and ``services.onboarding_evidence`` would have to report it as
    unmeasured — so the form makes the pair required rather than letting the row be half-defined.
    """
    # Declared, not inferred: `applies_to` is a JSONField, and left to the model form it renders a
    # JSON textarea where the categories are a set of boxes. Same reason CredentialTypeForm declares
    # it — the two columns mean the same thing and must be edited the same way.
    applies_to = forms.MultipleChoiceField(choices=PERSONNEL_CATEGORIES, required=False, widget=forms.CheckboxSelectMultiple(), help_text="Only people in the selected categories owe this step. Leave none ticked for every new hire.")

    class Meta:
        model = OnboardingItem
        fields = ["name", "code", "kind", "owner", "instructions", "due_within_days", "applies_to", "document_type", "signing_template_id", "credential_type", "order", "active"]
        widgets = {"instructions": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        # The pickers are filtered to one tenant on purpose. Left unfiltered, an operator could name
        # another company's record type on a step, and the cross-tenant trigger installed by this
        # migration would reject the save at the database — a 500 where a form error belongs.
        scope = organization or getattr(self.instance, "organization", None)
        if scope is not None:
            self.fields["document_type"].queryset = DocumentType.objects.filter(organization=scope)
            self.fields["credential_type"].queryset = CredentialType.objects.filter(organization=scope, active=True)
        from .document_signing import DocuSealClient
        from .models import SigningSettings
        self.signing_error = ""
        self.signing_templates = {}
        choices = [("", "Choose a DocuSeal template")]
        config = SigningSettings.objects.filter(organization=scope, enabled=True).first() if scope else None
        if config:
            try:
                data = DocuSealClient(config).api("GET", "/templates", params={"limit": 100})
                rows = data.get("data") if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    raise forms.ValidationError("DocuSeal returned an invalid template list.")
                for row in rows:
                    if isinstance(row, dict) and type(row.get("id")) is int and row["id"] > 0:
                        self.signing_templates[row["id"]] = str(row.get("name") or row["id"])[:255]
                choices += list(self.signing_templates.items())
            except forms.ValidationError as exc:
                self.signing_error = " ".join(exc.messages)
        self.fields["signing_template_id"] = forms.TypedChoiceField(
            choices=choices, coerce=int, empty_value=None, required=False, label="DocuSeal template",
            help_text="For a signing step. Build a single-signer template in DocuSeal first. Up to 100 templates are listed.",
        )
        if self.instance.signing_template_id and self.instance.signing_template_id not in self.signing_templates:
            self.fields["signing_template_id"].choices = choices + [
                (self.instance.signing_template_id, self.instance.signing_template_name or "Previously selected template"),
            ]
        self.organization = scope

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("kind")
        previous = OnboardingItem.objects.get(pk=self.instance.pk) if self.instance.pk else None
        if kind == OnboardingItem.Kind.DOCUMENT and not cleaned.get("document_type"):
            self.add_error("document_type", "A record step has to name which record type proves it, or it is a box to tick with nothing behind it.")
        if kind == OnboardingItem.Kind.CREDENTIAL and not cleaned.get("credential_type"):
            self.add_error("credential_type", "A credential step has to name which requirement it is waiting on.")
        if kind == OnboardingItem.Kind.SIGNATURE:
            from .document_signing import DocuSealClient, signing_config, validate_template
            document_type = cleaned.get("document_type")
            template_id = cleaned.get("signing_template_id")
            if not document_type or (cleaned.get("active") and (
                    not document_type.active or document_type.audience != DocumentType.Audience.PERSON
                    or document_type.sensitivity == DocumentType.Sensitivity.SEALED)):
                self.add_error("document_type", "Choose an active personnel record type that its signer may read.")
            if not template_id:
                self.add_error("signing_template_id", "Choose the template this step asks the employee to sign.")
            elif (previous and previous.kind == OnboardingItem.Kind.SIGNATURE
                    and previous.signing_template_id == template_id):
                self.instance.signing_template_name = self.signing_templates.get(template_id, previous.signing_template_name)
            else:
                try:
                    snapshot = validate_template(DocuSealClient(signing_config(self.organization)).template(template_id))
                    self.instance.signing_template_name = snapshot["name"]
                except forms.ValidationError as exc:
                    self.add_error("signing_template_id", exc)
            if cleaned.get("owner") != OnboardingItem.Owner.PERSON:
                self.add_error("owner", "A signing step belongs to the employee who signs it.")
        else:
            cleaned["signing_template_id"] = None
            self.instance.signing_template_name = ""
        if previous:
            if previous.tasks.filter(signing_requests__isnull=False).exists() and any(
                cleaned.get(name) != getattr(previous, name) for name in ("kind", "signing_template_id")
            ):
                self.add_error("kind", "This step has issued signing requests. Create a new step for a different template or kind.")
            if previous.tasks.filter(signing_requests__isnull=False).exists() and (
                getattr(cleaned.get("document_type"), "pk", None) != previous.document_type_id
            ):
                self.add_error("document_type", "This step has issued signing requests. Create a new step for a different record type.")
        return cleaned
