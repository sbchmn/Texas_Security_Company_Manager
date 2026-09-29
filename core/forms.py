from django import forms
from .models import Client, Credential, CredentialType, DispositionRequest, DocumentType, Branch, ImportBatch, Membership, Organization, Person, Shift, Site, TimePolicy, TrainingRecord

class BrandForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = ["display_name", "primary_color", "accent_color", "logo", "support_email", "support_phone", "timezone"]
        widgets = {"primary_color": forms.TextInput(attrs={"type": "color"}), "accent_color": forms.TextInput(attrs={"type": "color"})}

class BranchForm(forms.ModelForm):
    class Meta:
        model = Branch
        fields = ["name", "city"]

class PersonForm(forms.ModelForm):
    class Meta:
        model = Person
        fields = ["branch", "first_name", "last_name", "email", "status", "is_unarmed_officer", "is_commissioned_officer", "is_ppo", "is_private_investigator", "is_shareholder"]

class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = ["name", "contact_name", "contact_email", "active"]

class SiteForm(forms.ModelForm):
    class Meta:
        model = Site
        fields = ["client", "branch", "name", "address", "latitude", "longitude", "geofence_radius_meters", "active"]

class CredentialTypeForm(forms.ModelForm):
    class Meta:
        model = CredentialType
        fields = ["name", "code", "blocks_scheduling", "blocks_clock_in", "warning_days", "evidence_required", "active"]

class CredentialForm(forms.ModelForm):
    class Meta:
        model = Credential
        fields = ["person", "credential_type", "number", "status", "issued_on", "expires_on", "notes"]
        widgets = {"issued_on": forms.DateInput(attrs={"type": "date"}), "expires_on": forms.DateInput(attrs={"type": "date"})}

class ShiftForm(forms.ModelForm):
    class Meta:
        model = Shift
        fields = ["site", "officer", "starts_at", "ends_at", "status", "post_name", "post_orders", "required_credentials"]
        widgets = {"starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"}), "ends_at": forms.DateTimeInput(attrs={"type": "datetime-local"}), "required_credentials": forms.CheckboxSelectMultiple()}

class TimePolicyForm(forms.ModelForm):
    class Meta:
        model = TimePolicy
        fields = ["timezone", "workweek_start", "overtime_after_hours", "rounding_mode", "rounding_minutes", "require_geofence"]

class OrganizationSecurityForm(forms.ModelForm):
    mfa_required_roles = forms.MultipleChoiceField(choices=Membership.Role.choices,required=False,widget=forms.CheckboxSelectMultiple())
    class Meta:
        model=Organization
        fields=["mfa_required_roles"]

class DocumentUploadForm(forms.Form):
    person = forms.ModelChoiceField(queryset=Person.objects.none())
    document_type = forms.ModelChoiceField(queryset=DocumentType.objects.none())
    file = forms.FileField()
    expires_on = forms.DateField(required=False, widget=forms.DateInput(attrs={"type":"date"}))

class DocumentTypeForm(forms.ModelForm):
    class Meta:
        model = DocumentType
        fields = ["name","code","retention_days","acknowledgment_required","signature_required","active"]

class TrainingRecordForm(forms.ModelForm):
    class Meta:
        model = TrainingRecord
        fields = ["person","course_name","provider","completed_on","expires_on","certificate_number","hours"]
        widgets = {"completed_on":forms.DateInput(attrs={"type":"date"}),"expires_on":forms.DateInput(attrs={"type":"date"})}

class CsvImportForm(forms.Form):
    entity = forms.ChoiceField(choices=ImportBatch.Entity.choices)
    file = forms.FileField(help_text="UTF-8 CSV, maximum 50 MiB and 10,000 rows.")

class PunchAdjustmentForm(forms.Form):
    proposed_at = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type":"datetime-local"}))
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows":3}), min_length=5)

class PayrollPeriodForm(forms.Form):
    period_start = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type":"datetime-local"}))
    period_end = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type":"datetime-local"}))
    def clean(self):
        data=super().clean()
        if data.get("period_start") and data.get("period_end") and data["period_end"]<=data["period_start"]: raise forms.ValidationError("Period end must be after start.")
        return data

class DispositionRequestForm(forms.Form):
    action = forms.ChoiceField(choices=DispositionRequest.Action.choices)
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows":3}),min_length=10)
