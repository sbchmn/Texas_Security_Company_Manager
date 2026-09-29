import uuid
import json
import hashlib
from pathlib import Path
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models, transaction

hex_color = RegexValidator(r"^#[0-9A-Fa-f]{6}$", "Use a six-digit hex color, such as #16324F.")
slug_validator = RegexValidator(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", "Use lowercase letters, numbers, and single hyphens.")

def validate_brand_image_size(upload):
    if upload.size > 5 * 1024 * 1024:
        raise ValidationError("Brand images must be 5 MiB or smaller.")

class Organization(models.Model):
    class EmailProvider(models.TextChoices):
        MAILJET="mailjet","Mailjet"
        SES="ses","Amazon SES"
        POSTMARK="postmark","Postmark"
    class SmsProvider(models.TextChoices):
        SNS="sns","Amazon SNS"
        TWILIO="twilio","Twilio"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    legal_name = models.CharField(max_length=200)
    display_name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=63, unique=True, validators=[slug_validator])
    primary_color = models.CharField(max_length=7, default="#16324F", validators=[hex_color])
    accent_color = models.CharField(max_length=7, default="#14B8A6", validators=[hex_color])
    dark_primary_color = models.CharField(max_length=7, default="#0B1F33", validators=[hex_color])
    dark_accent_color = models.CharField(max_length=7, default="#5EEAD4", validators=[hex_color])
    dark_mode_enabled = models.BooleanField(default=True)
    logo = models.ImageField(upload_to="brands/%Y/%m/", blank=True, validators=[validate_brand_image_size])
    support_email = models.EmailField(blank=True)
    support_phone = models.CharField(max_length=30, blank=True)
    timezone = models.CharField(max_length=64, default="America/Chicago")
    email_provider = models.CharField(max_length=20, choices=EmailProvider.choices, default=EmailProvider.MAILJET)
    email_from = models.EmailField(blank=True)
    sms_provider = models.CharField(max_length=20, choices=SmsProvider.choices, default=SmsProvider.SNS)
    sms_from = models.CharField(max_length=30, blank=True)
    mfa_required_roles = models.JSONField(default=list, blank=True)
    audit_retention_days = models.PositiveIntegerField(default=2555)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.display_name

class BrandVersion(models.Model):
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="brand_versions")
    version=models.PositiveIntegerField()
    snapshot=models.JSONField()
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name="brand_versions")
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=["-version"]
        constraints=[models.UniqueConstraint(fields=["organization","version"],name="unique_brand_version_in_org")]

class Membership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Administrator"
        HR = "hr_compliance", "HR / Compliance"
        SCHEDULER = "scheduler", "Scheduler / Dispatcher"
        PAYROLL = "payroll", "Payroll approver"
        SUPERVISOR = "supervisor", "Site supervisor"
        OFFICER = "officer", "Officer"
        AUDITOR = "auditor", "Read-only auditor"
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="organization_memberships")
    role = models.CharField(max_length=30, choices=Role.choices)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "user"], name="one_membership_per_org")]

class Branch(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="branches")
    name = models.CharField(max_length=120)
    city = models.CharField(max_length=100, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="unique_branch_name_in_org")]
    def __str__(self): return self.name

class Person(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        ONBOARDING = "onboarding", "Onboarding"
        INACTIVE = "inactive", "Inactive"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="people")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="person_profiles")
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="people")
    first_name = models.CharField(max_length=80)
    last_name = models.CharField(max_length=80)
    email = models.EmailField(blank=True)
    mobile_phone = models.CharField(max_length=30, blank=True)
    employee_id = models.CharField(max_length=60, blank=True)
    job_title = models.CharField(max_length=120, blank=True)
    hire_date = models.DateField(null=True,blank=True)
    termination_date = models.DateField(null=True,blank=True)
    date_of_birth = models.DateField(null=True,blank=True)
    address_line1 = models.CharField(max_length=180,blank=True)
    address_line2 = models.CharField(max_length=180,blank=True)
    city = models.CharField(max_length=100,blank=True)
    state = models.CharField(max_length=2,default="TX")
    postal_code = models.CharField(max_length=12,blank=True)
    emergency_contact_name = models.CharField(max_length=160,blank=True)
    emergency_contact_phone = models.CharField(max_length=30,blank=True)
    hourly_rate = models.DecimalField(max_digits=9,decimal_places=2,null=True,blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ONBOARDING)
    is_unarmed_officer = models.BooleanField(default=False)
    is_commissioned_officer = models.BooleanField(default=False)
    is_ppo = models.BooleanField(default=False)
    is_private_investigator = models.BooleanField(default=False)
    is_shareholder = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["last_name", "first_name"]
        constraints=[models.UniqueConstraint(fields=["organization","employee_id"],condition=~models.Q(employee_id=""),name="unique_employee_id_in_org")]
    @property
    def full_name(self): return f"{self.first_name} {self.last_name}"

class PersonHistory(models.Model):
    person=models.ForeignKey(Person,on_delete=models.CASCADE,related_name="history")
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="person_history")
    changed_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True)
    changes=models.JSONField(default=dict)
    snapshot=models.JSONField(default=dict)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta: ordering=["-created_at"]

class CustomFieldDefinition(models.Model):
    class Kind(models.TextChoices):
        TEXT="text","Text"
        NUMBER="number","Number"
        DATE="date","Date"
        BOOLEAN="boolean","Yes / no"
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="custom_field_definitions")
    name=models.CharField(max_length=120)
    key=models.SlugField(max_length=60)
    kind=models.CharField(max_length=20,choices=Kind.choices,default=Kind.TEXT)
    required=models.BooleanField(default=False)
    sensitive=models.BooleanField(default=False)
    active=models.BooleanField(default=True)
    class Meta:
        ordering=["name"]
        constraints=[models.UniqueConstraint(fields=["organization","key"],name="unique_custom_field_key_in_org")]
    def __str__(self):return self.name

class PersonCustomValue(models.Model):
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="person_custom_values")
    person=models.ForeignKey(Person,on_delete=models.CASCADE,related_name="custom_values")
    definition=models.ForeignKey(CustomFieldDefinition,on_delete=models.CASCADE,related_name="values")
    value=models.JSONField(null=True,blank=True)
    class Meta: constraints=[models.UniqueConstraint(fields=["person","definition"],name="unique_custom_value")]
    def clean(self):
        if self.person_id and self.person.organization_id!=self.organization_id:raise ValidationError("Person must belong to the same organization.")
        if self.definition_id and self.definition.organization_id!=self.organization_id:raise ValidationError("Field definition must belong to the same organization.")

class Client(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="clients")
    name = models.CharField(max_length=160)
    contact_name = models.CharField(max_length=160, blank=True)
    contact_email = models.EmailField(blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="unique_client_name_in_org")]
    def __str__(self): return self.name

class Site(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="sites")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="sites")
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="sites")
    name = models.CharField(max_length=160)
    address = models.CharField(max_length=255)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    geofence_radius_meters = models.PositiveIntegerField(default=200)
    active = models.BooleanField(default=True)
    class Meta:
        ordering = ["client__name", "name"]
        constraints = [models.UniqueConstraint(fields=["organization", "client", "name"], name="unique_site_name_for_client")]
    def clean(self):
        if self.client_id and self.client.organization_id != self.organization_id:
            raise ValidationError("Client must belong to the same organization.")
        if self.branch_id and self.branch.organization_id != self.organization_id:
            raise ValidationError("Branch must belong to the same organization.")
    def __str__(self): return f"{self.client} — {self.name}"

class CredentialType(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="credential_types")
    name = models.CharField(max_length=140)
    code = models.SlugField(max_length=60)
    blocks_scheduling = models.BooleanField(default=True)
    blocks_clock_in = models.BooleanField(default=True)
    warning_days = models.PositiveIntegerField(default=60)
    evidence_required = models.BooleanField(default=True)
    active = models.BooleanField(default=True)
    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["organization", "code"], name="unique_credential_code_in_org")]
    def __str__(self): return self.name

class Credential(models.Model):
    class Status(models.TextChoices):
        MISSING = "missing", "Missing"
        PENDING = "pending", "Pending"
        UNVERIFIED = "unverified", "Unverified"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        REVOKED = "revoked", "Revoked"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="credentials")
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="credentials")
    credential_type = models.ForeignKey(CredentialType, on_delete=models.PROTECT, related_name="credentials")
    number = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UNVERIFIED)
    issued_on = models.DateField(null=True, blank=True)
    expires_on = models.DateField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering = ["expires_on", "credential_type__name"]
        constraints = [models.UniqueConstraint(fields=["person", "credential_type", "number"], name="unique_person_credential")]
    def clean(self):
        if self.person_id and self.person.organization_id != self.organization_id:
            raise ValidationError("Person must belong to the same organization.")
        if self.credential_type_id and self.credential_type.organization_id != self.organization_id:
            raise ValidationError("Credential type must belong to the same organization.")
    @property
    def effective_status(self):
        from django.utils import timezone
        if self.status == self.Status.ACTIVE and self.expires_on and self.expires_on < timezone.localdate():
            return "expired"
        return self.status

class TrainingRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="training_records")
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="training_records")
    course_name = models.CharField(max_length=180)
    provider = models.CharField(max_length=180, blank=True)
    completed_on = models.DateField()
    expires_on = models.DateField(null=True, blank=True)
    certificate_number = models.CharField(max_length=120, blank=True)
    hours = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0"))
    created_at = models.DateTimeField(auto_now_add=True)
    def clean(self):
        if self.person_id and self.person.organization_id != self.organization_id:
            raise ValidationError("Person must belong to the same organization.")

def personnel_document_path(instance, filename):
    extension=Path(filename).suffix.lower()
    return f"private/{instance.organization_id}/{instance.person_id}/{uuid.uuid4().hex}{extension}"

class DocumentType(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="document_types")
    name = models.CharField(max_length=160)
    code = models.SlugField(max_length=60)
    retention_days = models.PositiveIntegerField(null=True, blank=True, help_text="Blank means permanent retention.")
    acknowledgment_required = models.BooleanField(default=False)
    signature_required = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    class Meta:
        ordering=["name"]
        constraints=[models.UniqueConstraint(fields=["organization","code"],name="unique_document_type_code_in_org")]
    def __str__(self): return self.name

class PersonDocument(models.Model):
    class ScanStatus(models.TextChoices):
        PENDING="pending","Pending scan"
        CLEAN="clean","Clean"
        REJECTED="rejected","Rejected"
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="person_documents")
    person=models.ForeignKey(Person,on_delete=models.CASCADE,related_name="documents")
    document_type=models.ForeignKey(DocumentType,on_delete=models.PROTECT,related_name="documents")
    file=models.FileField(upload_to=personnel_document_path)
    original_name=models.CharField(max_length=255)
    content_type=models.CharField(max_length=100)
    size=models.PositiveBigIntegerField()
    sha256=models.CharField(max_length=64)
    scan_status=models.CharField(max_length=20,choices=ScanStatus.choices,default=ScanStatus.PENDING)
    expires_on=models.DateField(null=True,blank=True)
    retain_until=models.DateField(null=True,blank=True)
    legal_hold=models.BooleanField(default=False)
    archived_at=models.DateTimeField(null=True,blank=True)
    acknowledged_at=models.DateTimeField(null=True,blank=True)
    deleted_at=models.DateTimeField(null=True,blank=True)
    uploaded_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,related_name="uploaded_person_documents")
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=["-created_at"]
        indexes=[models.Index(fields=["organization","person","deleted_at"])]
    def clean(self):
        if self.person_id and self.person.organization_id != self.organization_id: raise ValidationError("Person must belong to the same organization.")
        if self.document_type_id and self.document_type.organization_id != self.organization_id: raise ValidationError("Document type must belong to the same organization.")

class DocumentAcknowledgment(models.Model):
    document=models.ForeignKey(PersonDocument,on_delete=models.PROTECT,related_name="acknowledgments")
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="document_acknowledgments")
    person=models.ForeignKey(Person,on_delete=models.PROTECT,related_name="document_acknowledgments")
    user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name="document_acknowledgments")
    signature_name=models.CharField(max_length=160,blank=True)
    statement=models.CharField(max_length=255)
    document_sha256=models.CharField(max_length=64)
    ip_hash=models.CharField(max_length=64)
    acknowledged_at=models.DateTimeField(auto_now_add=True)
    class Meta: constraints=[models.UniqueConstraint(fields=["document","person"],name="unique_document_acknowledgment")]

class DispositionRequest(models.Model):
    class Action(models.TextChoices):
        ARCHIVE="archive","Archive"
        DELETE="delete","Permanently delete file"
    class Status(models.TextChoices):
        PENDING="pending","Pending approval"
        EXECUTED="executed","Executed"
        CANCELLED="cancelled","Cancelled"
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="disposition_requests")
    document=models.ForeignKey(PersonDocument,on_delete=models.PROTECT,related_name="disposition_requests")
    action=models.CharField(max_length=20,choices=Action.choices)
    reason=models.TextField()
    requested_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name="requested_dispositions")
    approved_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name="approved_dispositions")
    status=models.CharField(max_length=20,choices=Status.choices,default=Status.PENDING)
    created_at=models.DateTimeField(auto_now_add=True)
    executed_at=models.DateTimeField(null=True,blank=True)
    class Meta: ordering=["-created_at"]

class Notification(models.Model):
    class Channel(models.TextChoices):
        IN_APP="in_app","In app"
        EMAIL="email","Email"
        SMS="sms","SMS"
    class Status(models.TextChoices):
        QUEUED="queued","Queued"
        SENT="sent","Sent"
        FAILED="failed","Failed"
        READ="read","Read"
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="notifications")
    recipient=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE,related_name="workforce_notifications")
    channel=models.CharField(max_length=20,choices=Channel.choices,default=Channel.IN_APP)
    event_type=models.CharField(max_length=100)
    subject=models.CharField(max_length=200)
    body=models.TextField()
    status=models.CharField(max_length=20,choices=Status.choices,default=Status.QUEUED)
    deduplication_key=models.CharField(max_length=160,blank=True)
    sent_at=models.DateTimeField(null=True,blank=True)
    attempts=models.PositiveSmallIntegerField(default=0)
    last_error=models.CharField(max_length=500,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=["-created_at"]
        constraints=[models.UniqueConstraint(fields=["organization","recipient","channel","deduplication_key"],condition=~models.Q(deduplication_key=""),name="unique_notification_deduplication")]

class ImportBatch(models.Model):
    class Entity(models.TextChoices):
        BRANCHES="branches","Branches"
        PEOPLE="people","People"
        CLIENTS="clients","Clients"
        SITES="sites","Sites"
        CREDENTIALS="credentials","Credentials"
        TRAINING="training","Training"
        SHIFTS="shifts","Shifts"
    class Status(models.TextChoices):
        PREVIEW="preview","Preview"
        INVALID="invalid","Invalid"
        APPLIED="applied","Applied"
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="import_batches")
    entity=models.CharField(max_length=30,choices=Entity.choices)
    source_name=models.CharField(max_length=255)
    source_hash=models.CharField(max_length=64)
    rows=models.JSONField(default=list)
    errors=models.JSONField(default=list)
    status=models.CharField(max_length=20,choices=Status.choices)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name="import_batches")
    created_at=models.DateTimeField(auto_now_add=True)
    applied_at=models.DateTimeField(null=True,blank=True)
    class Meta:
        ordering=["-created_at"]
        constraints=[models.UniqueConstraint(fields=["organization","entity","source_hash"],name="unique_import_source_in_org")]

class Shift(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="shifts")
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="shifts")
    officer = models.ForeignKey(Person, on_delete=models.SET_NULL, null=True, blank=True, related_name="shifts")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    post_name = models.CharField(max_length=140, blank=True)
    post_orders = models.TextField(blank=True)
    required_credentials = models.ManyToManyField(CredentialType, blank=True, related_name="required_for_shifts")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering = ["starts_at"]
        indexes = [models.Index(fields=["organization", "starts_at"])]
    def clean(self):
        if self.ends_at and self.starts_at and self.ends_at <= self.starts_at:
            raise ValidationError("Shift end must be after shift start.")
        if self.site_id and self.site.organization_id != self.organization_id:
            raise ValidationError("Site must belong to the same organization.")
        if self.officer_id and self.officer.organization_id != self.organization_id:
            raise ValidationError("Officer must belong to the same organization.")

class TimePolicy(models.Model):
    class RoundingMode(models.TextChoices):
        EXACT = "exact", "Exact time"
        NEAREST = "nearest", "Nearest interval"
        UP = "up", "Always up"
        DOWN = "down", "Always down"
    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="time_policy")
    timezone = models.CharField(max_length=64, default="America/Chicago")
    workweek_start = models.PositiveSmallIntegerField(default=0, help_text="Monday is 0; Sunday is 6")
    overtime_after_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("40.00"))
    rounding_mode = models.CharField(max_length=12, choices=RoundingMode.choices, default=RoundingMode.EXACT)
    rounding_minutes = models.PositiveSmallIntegerField(default=1)
    require_geofence = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)
    def clean(self):
        if self.rounding_minutes not in (1, 5, 6, 10, 15, 30):
            raise ValidationError({"rounding_minutes": "Choose 1, 5, 6, 10, 15, or 30 minutes."})

class Punch(models.Model):
    class Kind(models.TextChoices):
        IN = "in", "Clock in"
        OUT = "out", "Clock out"
        CHECKPOINT = "checkpoint", "Checkpoint"
    class Review(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        PENDING = "pending", "Pending review"
        REJECTED = "rejected", "Rejected"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client_event_id = models.UUIDField(unique=True)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="punches")
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="punches")
    shift = models.ForeignKey(Shift, on_delete=models.SET_NULL, null=True, blank=True, related_name="punches")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    occurred_at = models.DateTimeField()
    received_at = models.DateTimeField(auto_now_add=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    offline = models.BooleanField(default=False)
    review_status = models.CharField(max_length=20, choices=Review.choices, default=Review.ACCEPTED)
    exception_reason = models.CharField(max_length=255, blank=True)
    source = models.CharField(max_length=30, default="web")
    device_id = models.UUIDField(null=True, blank=True)
    device_sequence = models.PositiveBigIntegerField(null=True, blank=True)
    checkpoint = models.ForeignKey("Checkpoint",on_delete=models.SET_NULL,null=True,blank=True,related_name="punches")
    class Meta:
        ordering = ["occurred_at"]
        indexes = [models.Index(fields=["organization", "person", "occurred_at"])]
    def clean(self):
        if self.person_id and self.person.organization_id != self.organization_id:
            raise ValidationError("Person must belong to the same organization.")
        if self.shift_id and self.shift.organization_id != self.organization_id:
            raise ValidationError("Shift must belong to the same organization.")

class OfflineClockDevice(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="clock_devices")
    person=models.ForeignKey(Person,on_delete=models.CASCADE,related_name="clock_devices")
    user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE,related_name="clock_devices")
    label=models.CharField(max_length=120,blank=True)
    last_sequence=models.PositiveBigIntegerField(default=0)
    active=models.BooleanField(default=True)
    enrolled_at=models.DateTimeField(auto_now_add=True)
    last_seen_at=models.DateTimeField(null=True,blank=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=["organization","user","id"],name="unique_clock_device_in_org")]

class Checkpoint(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="checkpoints")
    site=models.ForeignKey(Site,on_delete=models.CASCADE,related_name="checkpoints")
    name=models.CharField(max_length=120)
    scan_code=models.UUIDField(default=uuid.uuid4,unique=True,editable=False)
    latitude=models.DecimalField(max_digits=9,decimal_places=6,null=True,blank=True)
    longitude=models.DecimalField(max_digits=9,decimal_places=6,null=True,blank=True)
    radius_meters=models.PositiveIntegerField(default=100)
    active=models.BooleanField(default=True)
    class Meta: constraints=[models.UniqueConstraint(fields=["site","name"],name="unique_checkpoint_name_at_site")]

    def clean(self):
        if self.site_id and self.site.organization_id != self.organization_id: raise ValidationError("Checkpoint must belong to the same organization.")

class PunchAdjustment(models.Model):
    class Status(models.TextChoices):
        REQUESTED="requested","Requested"
        APPROVED="approved","Approved"
        REJECTED="rejected","Rejected"
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="punch_adjustments")
    punch=models.ForeignKey(Punch,on_delete=models.PROTECT,related_name="adjustments")
    requested_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name="requested_punch_adjustments")
    proposed_at=models.DateTimeField()
    reason=models.TextField()
    status=models.CharField(max_length=20,choices=Status.choices,default=Status.REQUESTED)
    reviewed_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name="reviewed_punch_adjustments")
    reviewed_at=models.DateTimeField(null=True,blank=True)
    review_note=models.TextField(blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta: ordering=["-created_at"]
    def clean(self):
        if self.punch_id and self.punch.organization_id != self.organization_id: raise ValidationError("Punch must belong to the same organization.")

class PayrollRun(models.Model):
    class Status(models.TextChoices):
        DRAFT="draft","Draft"
        APPROVED="approved","Approved and locked"
        EXPORTED="exported","Exported"
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="payroll_runs")
    period_start=models.DateTimeField()
    period_end=models.DateTimeField()
    status=models.CharField(max_length=20,choices=Status.choices,default=Status.DRAFT)
    snapshot=models.JSONField(default=list)
    exceptions=models.JSONField(default=list)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name="created_payroll_runs")
    approved_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name="approved_payroll_runs")
    approved_at=models.DateTimeField(null=True,blank=True)
    exported_at=models.DateTimeField(null=True,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=["-period_end"]
        constraints=[models.UniqueConstraint(fields=["organization","period_start","period_end"],name="unique_payroll_period_in_org")]

class OrganizationDomain(models.Model):
    class Status(models.TextChoices):
        PENDING="pending","Pending verification"
        VERIFIED="verified","Verified"
        FAILED="failed","Verification failed"
        SUSPENDED="suspended","Suspended"
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="domains")
    hostname = models.CharField(max_length=253, unique=True)
    verified = models.BooleanField(default=False)
    status = models.CharField(max_length=20,choices=Status.choices,default=Status.PENDING)
    verification_token = models.CharField(max_length=64,default="")
    verified_at = models.DateTimeField(null=True,blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

class AppendOnlyAuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("Audit events are append-only")

    def delete(self):
        raise ValueError("Audit events cannot be deleted")

class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="audit_events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=120)
    target_type = models.CharField(max_length=80)
    target_id = models.CharField(max_length=100)
    metadata = models.JSONField(default=dict, blank=True)
    previous_hash = models.CharField(max_length=64,blank=True)
    event_hash = models.CharField(max_length=64,blank=True)
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    objects = AppendOnlyAuditQuerySet.as_manager()
    class Meta:
        ordering = ["-occurred_at"]
        constraints=[models.UniqueConstraint(fields=["organization","event_hash"],condition=~models.Q(event_hash=""),name="unique_audit_hash_in_org")]
    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError("Audit events are append-only")
        if self.event_hash or self.previous_hash: raise ValueError("Audit hashes are generated by the append-only writer")
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=self.organization_id)
            previous=type(self).objects.filter(organization_id=self.organization_id).order_by("-occurred_at","-id").first()
            self.previous_hash=previous.event_hash if previous else ""
            payload=json.dumps({"id":str(self.pk),"organization":str(self.organization_id),"actor":self.actor_id,"action":self.action,"target_type":self.target_type,"target_id":self.target_id,"metadata":self.metadata,"previous_hash":self.previous_hash},sort_keys=True,separators=(",",":"),default=str)
            self.event_hash=hashlib.sha256(payload.encode()).hexdigest()
            return super().save(*args, **kwargs)
        return super().save(*args, **kwargs)
    def delete(self, *args, **kwargs):
        raise ValueError("Audit events cannot be deleted")

class AuditRedaction(models.Model):
    organization=models.ForeignKey(Organization,on_delete=models.PROTECT,related_name="audit_redactions")
    event=models.ForeignKey(AuditEvent,on_delete=models.PROTECT,related_name="redactions")
    fields=models.JSONField(default=list)
    reason=models.TextField()
    legal_basis=models.CharField(max_length=255)
    requested_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name="requested_audit_redactions")
    approved_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name="approved_audit_redactions")
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=["-created_at"]
        constraints=[models.UniqueConstraint(fields=["event"],name="one_redaction_per_audit_event")]
