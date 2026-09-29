from django import forms
from .models import Client, Credential, CredentialType, CustomFieldDefinition, DispositionRequest, DocumentType, Branch, ImportBatch, Membership, Organization, Person, Shift, Site, TimePolicy, TrainingRecord

class BrandForm(forms.ModelForm):
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

class BranchForm(forms.ModelForm):
    class Meta:
        model = Branch
        fields = ["name", "city"]

class PersonForm(forms.ModelForm):
    class Meta:
        model = Person
        fields = ["branch", "employee_id", "first_name", "last_name", "email", "mobile_phone", "job_title", "hire_date", "termination_date", "date_of_birth", "address_line1", "address_line2", "city", "state", "postal_code", "emergency_contact_name", "emergency_contact_phone", "hourly_rate", "status", "is_unarmed_officer", "is_commissioned_officer", "is_ppo", "is_private_investigator", "is_shareholder"]
        widgets={name:forms.DateInput(attrs={"type":"date"}) for name in ("hire_date","termination_date","date_of_birth")}

class CustomFieldDefinitionForm(forms.ModelForm):
    class Meta:
        model=CustomFieldDefinition
        fields=["name","key","kind","required","sensitive","active"]

class DocumentAcknowledgmentForm(forms.Form):
    confirm=forms.BooleanField(label="I acknowledge that I reviewed this document")
    signature_name=forms.CharField(required=False,max_length=160,help_text="Required when the document type requests a signature.")

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

class DomainForm(forms.Form):
    hostname = forms.CharField(max_length=253,help_text="Example: portal.company.com")
    def clean_hostname(self):
        import re
        value=self.cleaned_data["hostname"].strip().rstrip(".").lower()
        if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}",value): raise forms.ValidationError("Enter a valid fully-qualified hostname.")
        return value

class AuditRedactionForm(forms.Form):
    fields = forms.CharField(help_text="Comma-separated metadata keys to redact from exports.")
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows":3}),min_length=10)
    legal_basis = forms.CharField(max_length=255,min_length=5)
    def clean_fields(self):
        fields=[item.strip() for item in self.cleaned_data["fields"].split(",") if item.strip()]
        if not fields:raise forms.ValidationError("Enter at least one metadata key.")
        return fields
