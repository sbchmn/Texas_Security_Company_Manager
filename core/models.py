import uuid
import json
import hashlib
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.utils import timezone

hex_color = RegexValidator(r"^#[0-9A-Fa-f]{6}$", "Use a six-digit hex color, such as #16324F.")
slug_validator = RegexValidator(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", "Use lowercase letters, numbers, and single hyphens.")

# Personnel categories are the applicability axis for a credential requirement: a
# commissioned officer must hold different records than an unarmed one, and "Missing" is
# only knowable once a requirement declares whom it applies to. Stored as keys so a new
# category is a configuration change, not a migration.
PERSONNEL_CATEGORIES = (
    ("unarmed", "Unarmed officer"),
    ("commissioned", "Commissioned / armed officer"),
    ("ppo", "Personal protection officer"),
    ("investigator", "Private investigator"),
    ("shareholder", "Shareholder or owner"),
)
PERSONNEL_CATEGORY_CODES = {key: flag for (key, flag) in zip(
    [code for code, _ in PERSONNEL_CATEGORIES],
    ("is_unarmed_officer", "is_commissioned_officer", "is_ppo", "is_private_investigator", "is_shareholder"),
)}

# Availability is a weekly pattern, and Monday is 0 because TimePolicy.workweek_start already
# counts that way. Two calendars in one product is how a dispatcher misreads a roster.
WEEKDAY_LABELS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
WEEKDAY_CHOICES = tuple((index, label) for index, label in enumerate(WEEKDAY_LABELS))

# How far one generation run may reach, in days. Eight weeks of a daily post is 56 rows, which is
# a roster a dispatcher can actually proof-read; a year in one click is a data import, and every
# blocked date inside it is a hole somebody finds on the night.
SERIES_MAX_DAYS = 84
# What "generate the next stretch" means for a series with no end date of its own.
SERIES_HORIZON_DAYS = 28

# What the industry actually calls these is not settled — "Pitman" and "Panama" are used for the
# same 14-day grid by different vendors and contradicted by others, and "2s and 3s" is a UK adverts
# term for the same rhythm. So these are presets over one cycle, not a controlled vocabulary: the
# offsets are the fact, the name is a `ShiftTemplate.alias`.
ROTATING_PRESETS = (
    ("2-2-3 · Panama · Pitman", 14, (0, 1, 4, 5, 6, 9, 10), "Seven twelve-hour tours a fortnight and every other weekend off — the usual 24/7 guard rotation."),
    ("4 on · 4 off", 8, (0, 1, 2, 3), "An eight-day cycle, so the days drift one weekday forward every time round."),
    ("4 on · 3 off (twelves)", 7, (0, 1, 2, 3), "Forty-eight hours a week: structurally over the forty-hour threshold, not occasionally."),
    ("Week on · week off", 14, tuple(range(7)), "Fourteen-day cycle; one week worked, one week off."),
    ("2 on · 2 off", 4, (0, 1), "The short version of 4-on/4-off."),
)

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
    default_post_orders = models.TextField(blank=True, help_text="Company-wide orders used when the client, site and post have no override.")
    email_provider = models.CharField(max_length=20, choices=EmailProvider.choices, default=EmailProvider.MAILJET)
    email_from = models.EmailField(blank=True)
    sms_provider = models.CharField(max_length=20, choices=SmsProvider.choices, default=SmsProvider.SNS)
    sms_from = models.CharField(max_length=30, blank=True)
    # NTF-4. The path segment a provider callback has to name to reach this company's message log. It is
    # generated on first use, not in a migration, because an empty column on every existing row would
    # collide under a plain unique index — NULL does not, on either backend.
    #
    # It is a secret and is treated like one: it is never logged, it is rotatable from the settings page,
    # and it is only half the guard. Twilio's own signature is checked when a token is configured; where
    # a provider documents nothing to verify (SNS publishes to an HTTPS URL it was given, Mailjet posts
    # plain events), the address plus TLS is the whole authentication and the retained event says so with
    # verified=False, so a later reader is not told a callback was proven when it was only addressed.
    webhook_token = models.CharField(max_length=64, blank=True, null=True, unique=True,
        help_text="Address half of this company's provider callbacks. Rotate it if it appears in a log.")
    mfa_required_roles = models.JSONField(default=list, blank=True)
    # AUTH-3. DD lets a company "restrict organizational roles to an approved Entra tenant while
    # allowing officers to link approved personal Google identities". The deployment-level
    # MICROSOFT_OIDC_TENANT cannot express that: one install serving three companies has one tenant
    # for all of them. So the rule is a tenant field, checked where a role is actually granted.
    #
    # It lists email domains, not tenant ids, and that is a known limit rather than an accident:
    # allauth's Microsoft provider returns Graph's /me payload, which carries no `tid` claim, so a
    # tenant check needs id-token handling this build cannot verify against a live directory. A
    # verified primary domain is the strongest claim available today, and it is what the DD sentence
    # is operationally about. Empty means no restriction, which keeps every existing install exactly
    # as it behaves now.
    approved_role_domains = models.JSONField(default=list, blank=True,
        help_text="Email domains allowed to hold a role other than Officer, e.g. ['guardco.com']. "
                  "Empty allows any identity; Officers may always link a personal address.")
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

    def __str__(self):
        name = self.user.get_full_name() or self.user.email or self.user.username
        email = f" · {self.user.email}" if self.user.email and self.user.email != name else ""
        return f"{name}{email} · {self.get_role_display()}"

class AuthorityScope(models.Model):
    """One bounded grant of manager authority: a branch, a contract, or a single post.

    A role on its own is organization-wide, so a site supervisor's compliance queue was the
    whole company's queue and their approval of an out-of-geofence punch reached every site.
    Authority is narrowed by granting scopes; a membership with no rows here keeps
    company-wide authority, which is what every installation had before this table existed.

    Company-level roles (owner, administrator, HR / Compliance, payroll approver, read-only
    auditor) are deliberately never narrowed: an administrator handed one branch must not
    lose the rest of the company by accident, and payroll and audit are firm-wide functions.
    """
    membership = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="authority_scopes")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="authority_scopes")
    branch = models.ForeignKey("core.Branch", on_delete=models.PROTECT, null=True, blank=True, related_name="authority_scopes")
    client = models.ForeignKey("core.Client", on_delete=models.PROTECT, null=True, blank=True, related_name="authority_scopes")
    site = models.ForeignKey("core.Site", on_delete=models.PROTECT, null=True, blank=True, related_name="authority_scopes")
    # AUTH-1. A grant may also follow the person the membership belongs to — "supervise my own
    # branch" instead of "supervise branch X". The explicit list is a snapshot that goes stale the
    # day a supervisor is transferred: the authority screen has to notice, and somebody has to edit
    # it, and until they do the supervisor is running the branch they left. The inherited reading
    # moves with the personnel file, which is what a firm that reassigns guards actually means when
    # it says "the site supervisor". Deliberately not the default: an unattended change of reach
    # when somebody moves branch is a worse failure than one extra click at grant time.
    follows_own_branch = models.BooleanField(default=False,
        help_text="Scope follows the branch recorded on this person's own file, including after a reassignment.")
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["membership", "branch"], name="one_branch_authority_per_membership"),
            models.UniqueConstraint(fields=["membership", "client"], name="one_client_authority_per_membership"),
            models.UniqueConstraint(fields=["membership", "site"], name="one_site_authority_per_membership"),
        ]
    def clean(self):
        chosen = [name for name, value in (("branch", self.branch_id), ("contract", self.client_id),
                                          ("site", self.site_id), ("own branch", self.follows_own_branch)) if value]
        if len(chosen) != 1:
            raise ValidationError("Choose exactly one scope: a branch, a contract client, a single site, "
                                  "or the branch on this person's own file.")
        if self.follows_own_branch:
            # A second inherited grant would be a duplicate rather than a wider scope, and MySQL
            # cannot make that impossible for us: the uniqueness this needs would hang on a column
            # that is NULL in every one of these rows, and NULLs are distinct inside its unique
            # indexes (roadmap §11). So it is refused here, at the door, like the dedup the rest of
            # the product does application-side.
            if AuthorityScope.objects.filter(membership_id=self.membership_id,
                                             follows_own_branch=True).exclude(pk=self.pk).exists():
                raise ValidationError("This person already has a grant that follows their own branch.")
        if self.membership_id and self.membership.organization_id != self.organization_id:
            raise ValidationError("Membership must belong to the same organization.")
        for name, obj in (("Branch", self.branch), ("Client", self.client), ("Site", self.site)):
            if obj and obj.organization_id != self.organization_id:
                raise ValidationError(f"{name} must belong to the same organization.")
    @property
    def scope_kind(self):
        if self.follows_own_branch:
            return "own_branch"
        return "branch" if self.branch_id else ("contract" if self.client_id else "site")

    @property
    def person_branch_id(self):
        """The branch on this member's own personnel file, read now rather than remembered.

        Two lookups because a membership is a user in an organization and the personnel file is the
        thing that carries a branch — and because a person with no file, or no branch on it, is a
        state the screen has to show instead of a state that silently means "everywhere".
        """
        if not self.membership_id:
            return None
        return Person.objects.filter(user_id=self.membership.user_id,
                                     organization_id=self.organization_id).values_list("branch_id", flat=True).first()

    @property
    def label(self):
        if self.follows_own_branch:
            branch = Branch.objects.filter(pk=self.person_branch_id).first()
            if branch is None:
                return "their own branch — no branch on the file yet, so this covers nothing"
            return f"{branch.name} branch (their own file, follows reassignment)"
        if self.branch_id:
            return f"{self.branch.name} branch"
        if self.client_id:
            return f"{self.client.name} (every post)"
        return f"{self.site.name} at {self.site.client.name}"
    def __str__(self):
        return self.label

class MembershipInvitation(models.Model):
    """Single-use invitation for an organization owner or administrator.

    A person-bound invitation also provisions sign-in for that personnel record: the
    officer clock, their document queue, and correction requests all key off
    ``Person.user``, and nothing in the product could set it before this link existed.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="membership_invitations")
    person = models.ForeignKey("core.Person", on_delete=models.CASCADE, null=True, blank=True, related_name="invitations")
    email = models.EmailField()
    role = models.CharField(max_length=30, choices=Membership.Role.choices)
    token_hash = models.CharField(max_length=64, unique=True)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sent_membership_invitations")
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["organization", "email"], condition=models.Q(accepted_at__isnull=True), name="one_pending_invitation_per_org_email")]

    def clean(self):
        if self.person_id and self.person.organization_id != self.organization_id:
            raise ValidationError("Person must belong to the same organization.")

    @staticmethod
    def digest_token(token):
        return hashlib.sha256(token.encode()).hexdigest()

    @classmethod
    def issue(cls, *, organization, email, role, invited_by, expires_at, person=None):
        token = secrets.token_urlsafe(32)
        invitation = cls.objects.create(organization=organization, person=person, email=email.casefold(), role=role, token_hash=cls.digest_token(token), invited_by=invited_by, expires_at=expires_at)
        return invitation, token

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
    # CLK-2. A shared clock station has to know which officer is standing in front of it, and a
    # name on a list is not identity. The PIN is stored as a password hash for exactly the reason
    # passwords are: the database is a copyable artefact, and a four-to-eight digit code is not a
    # secret that survives being read. `make_password` stretches it, so a stolen table does not
    # yield every officer's PIN at once.
    clock_pin = models.CharField(max_length=255, blank=True)
    # A stretched hash cannot be looked up, and a kiosk has to go from "the digits someone typed" to
    # "whose are these" without trying every officer's hash in turn — a five-hundred-officer roster
    # would make each keystroke on a shared tablet cost five hundred PBKDF2 runs. So there is a
    # second, fast digest used only as an index: HMAC over the deployment secret, the organization
    # and the PIN. It is not the PIN and it is not invertible without the secret, so a database dump
    # yields nothing usable; the answer is still confirmed against the stretched hash before it is
    # believed. It doubles as the uniqueness key, because two officers sharing one PIN would make a
    # punch at a shared station unattributable — which is the entire reason the PIN exists.
    clock_pin_index = models.CharField(max_length=64, blank=True, db_index=True)
    pin_set_at = models.DateTimeField(null=True, blank=True)
    # Failure state travels with the credential, not the station: an officer locked out by a
    # colleague hammering the pad at the guard shack is locked out everywhere, which is the safe
    # direction, and it means a second kiosk cannot be used to walk around the lockout.
    pin_failed_attempts = models.PositiveSmallIntegerField(default=0)
    pin_locked_until = models.DateTimeField(null=True, blank=True)
    is_unarmed_officer = models.BooleanField(default=False)
    is_commissioned_officer = models.BooleanField(default=False)
    is_ppo = models.BooleanField(default=False)
    is_private_investigator = models.BooleanField(default=False)
    is_shareholder = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["last_name", "first_name"]
        constraints=[models.UniqueConstraint(fields=["organization","employee_id"],condition=~models.Q(employee_id=""),name="unique_employee_id_in_org"),
                     # Two officers on one PIN makes a kiosk punch unattributable, so the PIN is unique
                     # inside the company — the same reason employee_id is. MySQL skips a conditional
                     # unique (models.W036), so `set_clock_pin` refuses a collision in application code
                     # as well; this constraint is what holds on the hermetic leg and any Postgres move.
                     models.UniqueConstraint(fields=["organization","clock_pin_index"],condition=~models.Q(clock_pin_index=""),name="unique_clock_pin_in_org")]
    @property
    def full_name(self): return f"{self.first_name} {self.last_name}"

    def __str__(self):
        return f"{self.full_name} ({self.employee_id})" if self.employee_id else self.full_name

    @property
    def personnel_categories(self):
        """Category keys this person holds; a person may hold several at once."""
        return [code for code, flag in PERSONNEL_CATEGORY_CODES.items() if getattr(self, flag)]

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

class PayCode(models.Model):
    """A job or cost-centre code a post is paid under — the column the customer's payroll wants.

    ``docs/development-roadmap.md`` PAY-1: the export already breaks out client, site, post, rates
    and the policy that produced the paid figure, but nothing downstream can load it without a
    manual mapping step, because every payroll system keys labour on a job code. It is required
    configuration rather than a free-text field on the shift so the list a dispatcher chooses from
    is the list the client's accountant recognises.

    Like the credential requirements and the rates, a code may be set at the contract, at the site,
    or on the individual post, and resolves in that order — a client that bills one code for a
    whole campus should not have it retyped on every shift placed there.
    """
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="pay_codes")
    name = models.CharField(max_length=140)
    code = models.SlugField(max_length=60, help_text="The literal string the customer's payroll system expects.")
    cost_centre = models.CharField(max_length=60, blank=True, help_text="Optional account or department the hours are charged to.")
    description = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)
    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["organization", "code"], name="unique_pay_code_in_org")]
    def __str__(self):
        return f"{self.name} ({self.code})" + (f" · {self.cost_centre}" if self.cost_centre else "")
    def clean(self):
        if self.organization_id and self.code:
            clash = PayCode.objects.filter(organization_id=self.organization_id, code=self.code).exclude(pk=self.pk)
            if clash.exists():
                raise ValidationError("This organization already uses that code.")

class PostOrdersMixin:
    post_orders_field: str
    post_orders_level: str

    @property
    def post_orders_parent(self) -> "Organization | Client | Site":
        raise NotImplementedError

    @property
    def post_orders_resolution(self) -> tuple[str, str]:
        own = getattr(self, self.post_orders_field)
        if own.strip():
            return own, self.post_orders_level
        parent = self.post_orders_parent
        if isinstance(parent, Organization):
            text = parent.default_post_orders
            return (text, "company") if text.strip() else ("", "")
        return parent.post_orders_resolution

    @property
    def effective_post_orders(self) -> str:
        return self.post_orders_resolution[0]

    @property
    def post_orders_source(self) -> str:
        return self.post_orders_resolution[1]


class Client(PostOrdersMixin, models.Model):
    post_orders_field = "default_post_orders"
    post_orders_level = "client"

    @property
    def post_orders_parent(self):
        return self.organization

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="clients")
    name = models.CharField(max_length=160)
    contact_name = models.CharField(max_length=160, blank=True)
    contact_email = models.EmailField(blank=True)
    default_post_orders = models.TextField(blank=True, help_text="Leave unchanged or empty to use company orders. Edited text overrides them for this client.")
    required_credentials = models.ManyToManyField("core.CredentialType", blank=True, related_name="required_by_clients")
    default_pay_rate = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    default_bill_rate = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    default_pay_code = models.ForeignKey(PayCode, on_delete=models.SET_NULL, null=True, blank=True, related_name="used_by_clients")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="unique_client_name_in_org")]
    def __str__(self): return self.name

class Site(PostOrdersMixin, models.Model):
    post_orders_field = "default_post_orders"
    post_orders_level = "site"

    @property
    def post_orders_parent(self):
        return self.client

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="sites")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="sites")
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="sites")
    name = models.CharField(max_length=160)
    address = models.CharField(max_length=255)
    default_post_orders = models.TextField(blank=True, help_text="Leave unchanged or empty to inherit client or company orders. Edited text overrides them for this site.")
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    geofence_radius_meters = models.PositiveIntegerField(default=200)
    # Requirements and rates live on the post, not only on the person or the individual
    # assignment: a contract says "every officer standing this site holds X" and pays Y, and the
    # dispatcher should not have to remember that on each shift.
    required_credentials = models.ManyToManyField("core.CredentialType", blank=True, related_name="required_by_sites")
    default_pay_rate = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    default_bill_rate = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    default_pay_code = models.ForeignKey(PayCode, on_delete=models.SET_NULL, null=True, blank=True, related_name="used_by_sites")
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

class AppliesToCategories:
    """The half of a rule that says who it binds — shared by approved rules and by checklists.

    Split out of ``RuleVocabulary`` because an onboarding step needs "armed officers only" without
    needing an authority URL, an interpretation, and a named approver. The field name is the
    contract: ``applies_to`` holds personnel-category codes, and an empty list binds nobody by
    category (for a rule that means unenforceable; for a checklist it means every new hire).
    """

    def applies_to_person(self, person):
        codes = set(self.applies_to or [])
        return bool(codes & set(person.personnel_categories))

    @property
    def applies_to_labels(self):
        lookup = dict(PERSONNEL_CATEGORIES)
        return [lookup.get(code, code) for code in (self.applies_to or [])]


class RuleVocabulary(AppliesToCategories):
    """The shared half of an approved rule: what makes it enforceable, whom it binds, how it renews.

    Two models use it — `CredentialType` and `ComplianceRule` — because the control matrix is one
    table and a rule whose approval or reminder ladder meant something different in the other row
    would not be a matrix. Field names are the contract; neither model stores the values itself.
    """

    @property
    def is_approved(self):
        """A rule is enforceable only once its source, reading, and dates are on the row."""
        return bool(self.approved_at and self.approved_by_id and self.authority_url and self.authority_reference and self.interpretation)

    @property
    def reminder_levels(self):
        """Descending, de-duplicated reminder lead times for this requirement.

        A single ``warning_days`` lead means the first notice and the last notice are the
        same notice. Peers escalate (30/7/1 and 90/60/30 in the products surveyed), so the
        ladder is configuration rather than a second hard-coded window.
        """
        levels = []
        for value in [self.warning_days, *(self.reminder_days_before or [])]:
            try:
                days = int(value)
            except (TypeError, ValueError):
                continue
            if days > 0 and days not in levels:
                levels.append(days)
        return sorted(levels, reverse=True)

class CredentialType(RuleVocabulary, models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="credential_types")
    name = models.CharField(max_length=140)
    code = models.SlugField(max_length=60)
    blocks_scheduling = models.BooleanField(default=True)
    blocks_clock_in = models.BooleanField(default=True)
    enforcement_grandfathered = models.BooleanField(
        default=True,
        help_text="Legacy escape for rows that were already refusing assignments and clock-ins before "
                  "approval became a precondition for enforcement. The settings screen clears it on "
                  "every row it creates or edits, so a requirement made from now on enforces only once "
                  "a named person has approved it; an existing row keeps behaving as its installation "
                  "chose until somebody approves it or turns the flags off. See the matrix's "
                  "\"Grandfathered\" label.")

    @property
    def may_enforce(self):
        """Whether this requirement's `blocks_*` flags are allowed to stop anything right now.

        The owner ruled on 2026-10-05 that approval gates enforcement: a rule with no source, no
        reference and no named approver must not be the reason a guard cannot clock in, because the
        firm cannot even say where the refusal came from. The grandfather flag exists because flipping
        that gate on for rows that predate it would silently stop enforcing obligations companies are
        relying on today — a change of behaviour dressed as a bug fix. So: new rows ask for approval,
        old rows keep their history, and every row that is enforcing *without* approval is labelled.
        """
        return bool(self.is_approved or self.enforcement_grandfathered)
    warning_days = models.PositiveIntegerField(default=60)
    reminder_days_before = models.JSONField(default=list, blank=True, help_text="Escalating reminder lead times in days before expiry. Empty uses the warning window once.")
    # NTF-2. The escalation lives on the requirement row beside the ladder it extends, which is the
    # shape DD asks for ("multiple reminder levels, lead times, repeat intervals, delivery channels,
    # and escalations") and the shape that keeps a rule enforceable: a duty whose warning and whose
    # escalation are configured in two places drifts, and the drifted pair reads as a company that
    # warned nobody. Two fields, not a settings object — see roadmap §5.
    escalate_after_days = models.PositiveIntegerField(null=True, blank=True, help_text="Once the countdown reaches this many days before expiry, the notice goes to the escalation roles as well. Set it to one of the lead times above, or nothing changes is missed.")
    escalate_to = models.JSONField(default=list, blank=True, help_text="Roles to pull in when a lower rung went unanswered. Empty means no escalation.")
    evidence_required = models.BooleanField(default=True)
    # CMP-3: how often somebody has to look the number up in the state registry. Blank means no
    # periodic verification is owed for this requirement, which is a different fact from "verified
    # today" — so it is a nullable column rather than a default of zero days.
    registry_check_within_days = models.PositiveIntegerField(null=True, blank=True, help_text="How often to re-check this credential against the DPS registry. Leave empty when no periodic check is owed.")
    jurisdiction = models.CharField(max_length=80, default="Texas")
    authority_url = models.URLField(blank=True)
    authority_reference = models.CharField(max_length=180, blank=True)
    interpretation = models.TextField(blank=True)
    effective_from = models.DateField(null=True, blank=True)
    effective_until = models.DateField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="approved_credential_rules")
    applies_to = models.JSONField(default=list, blank=True, help_text="Personnel categories that must hold this credential. Empty means no category is required to hold it.")
    # Bumped when a value that changes an evaluation actually changes, for the same reason the
    # clock policy carries one. The compliance matrix can say which rule applies to whom and who
    # approved it, but a reminder sent last month against a 60-day lead time cannot be reproduced
    # once someone edits it to 30 — the evaluation was computed from text that no longer exists.
    revision = models.PositiveIntegerField(default=1)
    active = models.BooleanField(default=True)
    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["organization", "code"], name="unique_credential_code_in_org")]
    def __str__(self): return self.name

    @property
    def escalation_roles(self):
        """The roles this requirement pulls in once a rung goes unanswered, validated on read.

        Stored as a JSON list of role values typed by an operator, so an unknown or stale value is
        dropped here rather than reaching a queryset that would raise on it — and rather than being
        silently quoted back on the settings page as if it were a role.
        """
        known = {value for value, _ in Membership.Role.choices}
        return [value for value in (self.escalate_to or []) if value in known]

    def escalates_at(self, days):
        """Whether a credential `days` from expiry has entered its escalation window.

        Deliberately *not* "was the notice opened". `Notification.Status.READ` exists only for
        in-app rows, so a read-based rule would escalate an officer who never saw a text and stay
        quiet about one who marked the app row read and did nothing. Whether the record was renewed
        is a fact the database already holds, cannot be satisfied by clicking, and is what the firm
        actually cares about — so the missed rung is measured by the state of the credential, and the
        ladder firing at all is the proof that the earlier rungs did not change it.
        """
        if days is None or not self.escalation_roles or self.escalate_after_days is None:
            return False
        return days <= self.escalate_after_days

    def missed_rungs(self, days):
        """The lead times that already fired above this one, largest first — for the notice body.

        An escalated notice that does not say it is an escalation is a second copy of a reminder
        somebody already read. Naming the rungs that passed is what tells the owner this is new
        information about somebody else's inaction rather than the countdown repeating.
        """
        return [level for level in self.reminder_levels if level > (days or 0)]

    def clean(self):
        roles = self.escalate_to or []
        if self.escalate_after_days is not None and not roles:
            raise ValidationError({"escalate_to": "Choose who hears the notice once a rung is missed, or clear the escalation lead time."})
        if roles and self.escalate_after_days is None:
            raise ValidationError({"escalate_after_days": "An escalation needs the lead time it triggers at — the rung after which the notice is considered unanswered."})
        known = {value for value, _ in Membership.Role.choices}
        unknown = [role for role in roles if role not in known]
        if unknown:
            raise ValidationError({"escalate_to": "Unknown role: " + ", ".join(unknown)})
        if roles and self.escalate_after_days is not None:
            levels = self.reminder_levels
            # Escalating before the first warning has been sent is not an escalation, it is a wider
            # first notice — and the difference matters to the person who receives a lapsed licence
            # named as "unanswered" when nobody had asked anybody yet.
            if levels and self.escalate_after_days > levels[0]:
                raise ValidationError({"escalate_after_days":
                    f"Pick {self.escalate_after_days} days or fewer: the first notice for this requirement goes out at "
                    f"{levels[0]} days, so nothing can have been missed yet."})

class ComplianceRule(RuleVocabulary, models.Model):
    """A duty that is not "a person holds a numbered document with an expiry date".

    The control matrix had one subject, `CredentialType`, so the Texas obligations the research pass
    recorded had nowhere to go: the CGL limits and certificate holding in §1702.124, the workplace
    posting duty in §1702.128 / 37 TAC §35.8, and the owner/shareholder training **DD §HCRM** names
    by title. None is a credential an individual renews. An operator could file the *evidence* as a
    `DocumentType` with retention and get nothing else — no applicability, no authority, no effective
    dates, no version, and no answer to "who is missing this".

    The columns deliberately match the credential requirement's, because the matrix is one table and
    a rule the operator cannot compare against its neighbours is not a control matrix. What differs
    is where the proof lives (`evidence`) and *what has to hold it* (`applies_to_subject`): an
    insurance certificate is held by the company, a commission by a person, a posting duty at a
    site.
    """
    class Evidence(models.TextChoices):
        DOCUMENT="document","A filed record of the named type"
        POSTING="posting","A notice posted where the work is performed"
        TRAINING_HOURS="training_hours","Training hours on file"
        CREDENTIAL="credential","A numbered credential (entered as a requirement)"
    class Subject(models.TextChoices):
        ORGANIZATION="organization","The company"
        PEOPLE="people","Officers in the listed categories"
        SITES="sites","Each site the company operates"
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="compliance_rules")
    name=models.CharField(max_length=140)
    code=models.SlugField(max_length=60)
    evidence=models.CharField(max_length=20,choices=Evidence.choices,default=Evidence.DOCUMENT)
    applies_to_subject=models.CharField(max_length=20,choices=Subject.choices,default=Subject.PEOPLE)
    applies_to=models.JSONField(default=list,blank=True,help_text="Personnel categories that must satisfy this rule. Only consulted when the subject is officers.")
    document_type=models.ForeignKey("core.DocumentType",on_delete=models.SET_NULL,null=True,blank=True,related_name="rules_requiring_it",help_text="For a filed-record rule: which record type proves it.")
    required_hours=models.PositiveIntegerField(null=True,blank=True,help_text="For a training-hours rule: how many are required.")
    warning_days=models.PositiveIntegerField(default=60,help_text="Renewal lead time for evidence that itself expires.")
    reminder_days_before=models.JSONField(default=list,blank=True,help_text="Escalating reminder lead times in days. Empty uses the warning window once.")
    jurisdiction=models.CharField(max_length=80,default="Texas")
    authority_url=models.URLField(blank=True)
    authority_reference=models.CharField(max_length=180,blank=True)
    interpretation=models.TextField(blank=True)
    effective_from=models.DateField(null=True,blank=True)
    effective_until=models.DateField(null=True,blank=True)
    approved_at=models.DateTimeField(null=True,blank=True)
    approved_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name="approved_compliance_rules")
    revision=models.PositiveIntegerField(default=1)
    active=models.BooleanField(default=True)
    class Meta:
        ordering=["name"]
        constraints=[models.UniqueConstraint(fields=["organization","code"],name="unique_compliance_rule_code_in_org")]
    def __str__(self): return self.name

    @property
    def unevaluated_reason(self):
        """Why the queue cannot yet answer "who is missing this", or None when it can.

        Reporting a duty as satisfied because nothing was found to check it against is the failure
        this product exists to prevent, so a rule with no evidence store says so out loud instead of
        dropping off the list: a certificate rule with no record type named, training hours matched
        against nothing, and a posting at each site (the personnel file has no site column to file
        against) are all entered, versioned, and honestly not evaluated.
        """
        if not self.is_approved:
            return "not approved, so not enforced yet"
        if self.evidence==self.Evidence.CREDENTIAL:
            return "credential evidence is tracked as a requirement"
        if self.evidence==self.Evidence.DOCUMENT and not self.document_type_id:
            return "no record type named as the evidence"
        if self.evidence==self.Evidence.TRAINING_HOURS:
            return "no course is matched to these hours yet"
        if self.applies_to_subject==self.Subject.SITES:
            return "records cannot be filed against a site yet"
        if self.evidence==self.Evidence.POSTING:
            return "posting evidence is not tracked yet"
        if self.applies_to_subject==self.Subject.PEOPLE and not (self.applies_to or []):
            return "no personnel category is bound to it"
        return None

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

class CredentialRegistryCheck(models.Model):
    """One human lookup of a licence in the state registry, kept as a history rather than a flag.

    CMP-3 exists because DPS publishes a search page and no public API, so the honest capability is
    "somebody checked it, on a date, and recorded what they saw" — not an automated call. A single
    ``verified`` boolean would drop the two things an inspection asks for (*when*, and *who*), and an
    undated check proves nothing about the present: a registration confirmed in March says nothing
    about April, which is exactly the gap §1702.302(a) / 37 TAC §35.22(b) turns on.

    Rows are appended, never overwritten, so the queue can show the last check while the file keeps
    the trail. What this record does **not** do is change the credential's ``status`` or the
    compliance rate: a licence the registry confirmed as valid but nobody has looked up since June is
    a bookkeeping lapse, and treating it as a failed credential would cancel posts over paperwork
    instead of over an expired registration.
    """
    class Result(models.TextChoices):
        VALID = "valid", "Registry shows it valid"
        EXPIRED = "expired", "Registry shows it expired"
        NOT_FOUND = "not_found", "No registry record found"
        ATTENTION = "attention", "Registry disagrees with the record on file"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="credential_registry_checks")
    credential = models.ForeignKey(Credential, on_delete=models.CASCADE, related_name="registry_checks")
    checked_on = models.DateField()
    result = models.CharField(max_length=20, choices=Result.choices, default=Result.VALID)
    registry_reference = models.CharField(max_length=120, blank=True, help_text="What the registry itself displayed, so the next checker can compare.")
    note = models.TextField(blank=True)
    checked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="credential_registry_checks")
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-checked_on", "-created_at"]
        indexes = [models.Index(fields=["credential", "checked_on"], name="core_registrycheck_credential")]
    def clean(self):
        if self.credential_id and self.credential.organization_id != self.organization_id:
            raise ValidationError("Credential must belong to the same organization.")

    def __str__(self):
        return f"{self.get_result_display()} — {self.checked_on}"


class OnboardingItem(AppliesToCategories, models.Model):
    """A step a new hire in this company owes, defined once and issued per person.

    ``Person.status`` was one column and a dashboard count, so "automate onboarding" had nothing to
    automate against — no sequence, no owner, no deadline, no completion state (§13 ONB-1), which is
    also why NTF-3 deferred the onboarding notification family. A step is *configuration on the
    company*, the same shape ``DocumentType.retention_days`` is: the officer does not compose their
    own checklist, and editing the checklist does not rewrite the tasks already issued from it.

    ``kind`` names only the two kinds whose evidence this product can actually look up. Training is a
    step someone marks, not evidence we can verify — the course matcher CMP-0 recorded as missing is
    still missing, and a kind that compared ``TrainingRecord.course_name`` by string would report a
    completion nobody checked. That is the same discipline ``ComplianceRule.unevaluated_reason``
    applies: say what is not measured instead of measuring nothing and calling it satisfied.
    """
    class Kind(models.TextChoices):
        TASK = "task", "A step someone marks done"
        DOCUMENT = "document", "A record filed in the personnel file"
        SIGNATURE = "signature", "A document signed in DocuSeal"
        CREDENTIAL = "credential", "A numbered registration or commission on file"
    class Owner(models.TextChoices):
        PERSON = "person", "The new hire"
        STAFF = "staff", "Somebody in the office"
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="onboarding_items")
    name = models.CharField(max_length=140)
    code = models.SlugField(max_length=60)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.TASK)
    owner = models.CharField(max_length=10, choices=Owner.choices, default=Owner.PERSON)
    instructions = models.TextField(blank=True)
    due_within_days = models.PositiveIntegerField(default=7, help_text="Calendar days after the hire date this step is owed by.")
    applies_to = models.JSONField(default=list, blank=True, help_text="Personnel categories that must do this step. Empty means every new hire.")
    document_type = models.ForeignKey("core.DocumentType", on_delete=models.SET_NULL, null=True, blank=True, related_name="onboarding_items", help_text="For a record step: which type must be on file.")
    signing_template_id = models.PositiveBigIntegerField(null=True, blank=True)
    signing_template_name = models.CharField(max_length=255, blank=True)
    credential_type = models.ForeignKey(CredentialType, on_delete=models.PROTECT, null=True, blank=True, related_name="onboarding_items", help_text="For a credential step: which requirement must be on file.")
    order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["order", "name"]
        constraints = [models.UniqueConstraint(fields=["organization", "code"], name="unique_onboarding_item_code_in_org")]
    def __str__(self):
        return self.name

    @property
    def binds_every_new_hire(self):
        """An empty category list here means "everyone", not "nobody".

        ``RuleVocabulary`` reads the same column the other way and deliberately so: a compliance duty
        that names no category is unenforceable, while a checklist step that names no category is one
        every new hire takes. The two readings are spelled out instead of sharing one boolean,
        because confusing them silently empties a new hire's checklist.
        """
        return not (self.applies_to or [])

    def applies_to_person_or_all(self, person):
        """Whether this step belongs on this person's checklist."""
        return self.binds_every_new_hire or self.applies_to_person(person)


class OnboardingTask(models.Model):
    """One item owed by one person, stamped with the deadline it was issued under.

    ``due_on`` is stored at issue time rather than recomputed on every read, for the same reason the
    reminder ladder dedups on a rung: a notice already sent named a date, and if somebody edits
    ``due_within_days`` from 7 to 30, the officer chased on day eight should not be told the deadline
    was always day thirty. The trade-off is stated plainly — an open task keeps the date it was
    issued with, and re-planning after a rule change is an explicit action, not a side effect of a
    save.
    """
    class Status(models.TextChoices):
        OPEN = "open", "Outstanding"
        DONE = "done", "Complete"
        WAIVED = "waived", "Not required for this person"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="onboarding_tasks")
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="onboarding_tasks")
    item = models.ForeignKey(OnboardingItem, on_delete=models.PROTECT, related_name="tasks")
    due_on = models.DateField(null=True, blank=True, help_text="Hire date plus the step's lead time when this was issued. Empty means no hire date was recorded yet.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="onboarding_decisions")
    decided_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True, help_text="Why this was waived, or what was accepted as proof.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering = ["item__order", "item__name"]
        constraints = [models.UniqueConstraint(fields=["person", "item"], name="one_onboarding_task_per_person_item")]
        indexes = [models.Index(fields=["organization", "person", "status"], name="core_onboardingtask_state")]
    def clean(self):
        if self.person_id and self.person.organization_id != self.organization_id:
            raise ValidationError("Person must belong to the same organization.")

    def __str__(self):
        return f"{self.item.name} — {self.person.full_name}"


class SigningSettings(models.Model):
    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="signing_settings")
    base_url = models.URLField(max_length=255)
    encrypted_api_key = models.TextField()
    enabled = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)


class SigningRequest(models.Model):
    class Status(models.TextChoices):
        PREPARING = "preparing", "Checking submission creation"
        SENT = "sent", "Awaiting signature"
        COMPLETED = "completed", "Signed and filed"
        DECLINED = "declined", "Declined or expired"
        REJECTED = "rejected", "Not created; correct settings and resend"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="signing_requests")
    task = models.ForeignKey(OnboardingTask, on_delete=models.PROTECT, related_name="signing_requests")
    attempt = models.PositiveIntegerField()
    document_type = models.ForeignKey("core.DocumentType", on_delete=models.PROTECT)
    template_id = models.PositiveBigIntegerField()
    template_snapshot = models.JSONField(default=dict)
    signer_email = models.EmailField()
    signer_name = models.CharField(max_length=255)
    base_url = models.URLField(max_length=255)
    submission_id = models.PositiveBigIntegerField(null=True, blank=True)
    submitter_id = models.PositiveBigIntegerField(null=True, blank=True)
    signing_slug = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PREPARING)
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    next_check_at = models.DateTimeField(default=timezone.now)
    failures = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-attempt"]
        constraints = [
            models.UniqueConstraint(fields=["task", "attempt"], name="one_signing_attempt_per_task"),
            models.UniqueConstraint(fields=["organization", "submission_id"], name="one_signing_submission_per_org"),
        ]
        indexes = [models.Index(fields=["status", "next_check_at"], name="core_signing_due")]

    def clean(self):
        if self.task_id and self.task.organization_id != self.organization_id:
            raise ValidationError("Signing task must belong to this organization.")
        if self.document_type_id and self.document_type.organization_id != self.organization_id:
            raise ValidationError("Signing record type must belong to this organization.")


class SignedArtifact(models.Model):
    request = models.ForeignKey(SigningRequest, on_delete=models.PROTECT, related_name="artifacts")
    document = models.OneToOneField("core.PersonDocument", on_delete=models.PROTECT, related_name="signed_artifact")
    key = models.CharField(max_length=80)
    name = models.CharField(max_length=255)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["request", "key"], name="one_artifact_per_signing_request")]

    def clean(self):
        if self.request_id and self.document_id:
            if (self.document.organization_id != self.request.organization_id
                    or self.document.person_id != self.request.task.person_id
                    or self.document.document_type_id != self.request.document_type_id):
                raise ValidationError("Signed artifact must be filed against the signing request's person and record type.")


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
    # Organization-level records (handbook, company licence) have no person, so the key
    # must not interpolate a None into the private prefix.
    scope=str(instance.person_id) if instance.person_id else "organization"
    return f"private/{instance.organization_id}/{scope}/{uuid.uuid4().hex}{extension}"

class DocumentType(models.Model):
    class Audience(models.TextChoices):
        PERSON="person","Attached to one person's file"
        WORKFORCE="workforce","Company record issued to every worker"
        MANAGEMENT="management","Company record for administrators only"
    class Sensitivity(models.TextChoices):
        STANDARD="standard","Personnel file"
        RESTRICTED="restricted","Restricted"
        SEALED="sealed","Sealed"
        BIOMETRIC="biometric","Biometric"
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="document_types")
    name = models.CharField(max_length=160)
    code = models.SlugField(max_length=60)
    audience = models.CharField(max_length=20, choices=Audience.choices, default=Audience.PERSON)
    sensitivity = models.CharField(max_length=20, choices=Sensitivity.choices, default=Sensitivity.STANDARD, help_text="Who may open a record of this type, in addition to its audience.")
    retention_days = models.PositiveIntegerField(null=True, blank=True, help_text="Blank means permanent retention.")
    acknowledgment_required = models.BooleanField(default=False)
    signature_required = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    class Meta:
        ordering=["name"]
        constraints=[models.UniqueConstraint(fields=["organization","code"],name="unique_document_type_code_in_org")]
    def __str__(self): return self.name

    @property
    def reader_summary(self):
        """Who may open a record of this type, spelled out for the settings screen.

        The rung is a decision an administrator makes once and never reads again; showing the role
        list beside it is what tells them they just hid a claim file from the auditor.
        """
        names = [Membership.Role(value).label for value in DOCUMENT_TYPE_READERS.get(self.sensitivity, ())]
        if self.sensitivity == self.Sensitivity.SEALED.value:
            names.append("not the officer the record is about")
        return ", ".join(names)

# `audience` says who a record is *issued to*. It cannot say who may *open* one, and every
# family-4 record in RB §HCRM record catalog — workers' compensation, accommodation and leave,
# drug and background screening, investigation and discipline — is attached to one person exactly
# like a payroll receipt is, so the three-value choice never separates them. The research asks for
# those to be "isolated by stricter permissions rather than exposed in the general personnel
# file", so secrecy is its own ladder, and the two axes it narrows are different ones.
#
# `restricted` narrows the **staff** list: a claim file or a screening result stops being readable
# by a read-only auditor, who is often an outside accountant, and stays with the roles that own the
# employment relationship. `sealed` narrows the **subject** instead: an investigation record is not
# shown to the person it investigates, which is why its staff list is the same as `restricted` —
# the rung is about the worker, not about the office.
DOCUMENT_TYPE_READERS = {
    DocumentType.Sensitivity.STANDARD.value: (Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR, Membership.Role.AUDITOR),
    DocumentType.Sensitivity.RESTRICTED.value: (Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR),
    DocumentType.Sensitivity.SEALED.value: (Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR),
    # CLK-1, and the reason a fourth rung exists rather than a widened third. The owner ruled on
    # 2026-10-04 that a clock selfie is openable by the subject, owner, administrator, HR *and the
    # dispatcher* — the role that has to decide at 3 a.m. whether a no-show was real. `restricted` is
    # owner/admin/HR, and adding dispatch to it would equally disclose every workers'-compensation
    # claim, accommodation file, leave record and screening result to dispatch, which is a different
    # disclosure decided by different people for different reasons. So the staff list is exactly the
    # four named roles, and it is the *auditor* — often an outside accountant — that stays out.
    # Being subject-readable costs nothing here: `SUBJECT_READABLE_SENSITIVITIES` below excludes only
    # `sealed`, and a face photograph about you is not sealed from you.
    DocumentType.Sensitivity.BIOMETRIC.value: (Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR, Membership.Role.SCHEDULER),
}
# A worker filed the claim and was sent the screening result, so those are theirs to read.
SUBJECT_READABLE_SENSITIVITIES = tuple(value for value in DocumentType.Sensitivity.values if value != DocumentType.Sensitivity.SEALED.value)
SEALED_FROM_SUBJECT = (DocumentType.Sensitivity.SEALED.value,)


def staff_sensitivities(role):
    """The rungs a role opens. An unknown role opens none, so a new role fails closed."""
    return tuple(value for value, roles in DOCUMENT_TYPE_READERS.items() if role in roles)


def record_visibility_filter(role, subject_person_id=None):
    """Which `PersonDocument` rows one reader may open, as a filter.

    Every listing path takes this — the person's documents tab, the `/documents/` register, the
    compliance queue and the rate computed from it — rather than writing its own `if`, because the
    list and the download route must give the same answer. A register that shows a row the route
    refuses 404s the user on their own file; a list that hides a row the route serves is the same
    defect quietly. `record_readable` is the row-level twin, and `RecordSegregationTest` asserts
    the two agree on every rung so neither can drift alone.

    Being the subject of a sealed record *denies* even a role that reads sealed files: the staff
    list exists to let HR open other people's records, not to route around "this one is about you
    and it is not yours yet".
    """
    grant = models.Q(document_type__audience=DocumentType.Audience.WORKFORCE, person__isnull=True)
    grant |= models.Q(document_type__sensitivity__in=staff_sensitivities(role))
    if subject_person_id:
        grant |= models.Q(person_id=subject_person_id, document_type__sensitivity__in=SUBJECT_READABLE_SENSITIVITIES)
        grant &= ~models.Q(person_id=subject_person_id, document_type__sensitivity__in=SEALED_FROM_SUBJECT)
    return grant


def record_open_for(document_type, person_id, role, subject_person_id=None):
    """The ladder for one (record type, holder) pair, before any row exists to check.

    Split out from `record_readable` because the compliance queue has to ask this about a duty it
    is *about to report*: a row that reads "no claim filed" for someone whose sealed-from-you claim
    is sitting in the register is a false statement built out of a permission decision.
    """
    is_subject = bool(subject_person_id) and person_id == subject_person_id
    if is_subject and document_type.sensitivity in SEALED_FROM_SUBJECT:
        return False
    if person_id is None and document_type.audience == DocumentType.Audience.WORKFORCE:
        return True
    if document_type.sensitivity in staff_sensitivities(role):
        return True
    return is_subject and document_type.sensitivity in SUBJECT_READABLE_SENSITIVITIES


def record_readable(document, role, subject_person_id=None):
    """`record_visibility_filter` for one row, for the routes that must answer 404."""
    return record_open_for(document.document_type, document.person_id, role, subject_person_id)

class PersonDocument(models.Model):
    class ScanStatus(models.TextChoices):
        PENDING="pending","Pending scan"
        CLEAN="clean","Clean"
        REJECTED="rejected","Rejected"
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="person_documents")
    person=models.ForeignKey(Person,on_delete=models.CASCADE,related_name="documents",null=True,blank=True,help_text="Blank for a company-level record such as a handbook or licence.")
    document_type=models.ForeignKey(DocumentType,on_delete=models.PROTECT,related_name="documents")
    file=models.FileField(upload_to=personnel_document_path)
    original_name=models.CharField(max_length=255)
    content_type=models.CharField(max_length=100)
    # What the upload's bytes were actually proved to be, from the extension's magic signature during
    # `validate_document_upload`. `content_type` above is what the uploading browser *declared*, which
    # is attacker-controlled and therefore useless for a decision about rendering: a file named
    # `report.png` that is really `<script>` markup would otherwise be served inline as an image and
    # sniffed into a document. This column is the one the preview reads, and it is empty on rows
    # recorded before it existed — which makes those rows downloadable, not previewable, rather than
    # guessing at what they contain.
    verified_type=models.CharField(max_length=100,blank=True,default="",
        help_text="MIME proved from the file's own bytes at upload. Blank means never verified.")
    size=models.PositiveBigIntegerField()
    sha256=models.CharField(max_length=64)
    scan_status=models.CharField(max_length=20,choices=ScanStatus.choices,default=ScanStatus.PENDING)
    expires_on=models.DateField(null=True,blank=True)
    retain_until=models.DateField(null=True,blank=True)
    legal_hold=models.BooleanField(default=False)
    archived_at=models.DateTimeField(null=True,blank=True)
    acknowledged_at=models.DateTimeField(null=True,blank=True)
    deleted_at=models.DateTimeField(null=True,blank=True)
    # A revised handbook is a revision of the previous one, not an unrelated row: signed
    # signatures stay attached to the version they were given for, and the chain is what tells an
    # auditor that the worker who signed in 2024 never read the 2026 text.
    supersedes=models.ForeignKey("self",on_delete=models.SET_NULL,null=True,blank=True,related_name="revisions")
    uploaded_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,related_name="uploaded_person_documents")
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=["-created_at"]
        indexes=[models.Index(fields=["organization","person","deleted_at"])]

    def __str__(self):
        person = self.person
        subject = person.full_name if person is not None else "Company record"
        return f"{self.document_type.name} - {subject} - {self.original_name}"

    @property
    def is_current(self):
        """Nothing supersedes this row, so it is the version the roster is being asked to sign."""
        return not self.revisions.exists()

    @property
    def is_archived(self):
        """Filed away under a retention decision, but not destroyed.

        The state has to mean something on a read path or "archive" is only a timestamp: an
        archived record is out of the active personnel file, out of the compliance queue, and out
        of the reminders — while staying readable and downloadable from retention review, because
        retention is not destruction. Recovery is the other half of that, which is why
        ``DispositionRequest`` records the reversal against itself and not against the document.
        """
        return bool(self.archived_at) and not self.deleted_at
    @property
    def preview_kind(self):
        """How this record may be shown inside the page — `pdf`, `image`, `text`, or `None`.

        Keyed on `verified_type`, never on `content_type`, and the allowlist lives in `services`
        beside the signatures that produced the verdict, so the two cannot drift. `None` is the
        answer for anything whose bytes were never verified, for Office documents (a zip whose
        contents cannot be reasoned about from its head), and for anything scriptable at all — which
        is why SVG is absent from the storable set to begin with.

        Local import rather than a module-level one because `services` imports `models`; and this is
        advisory for rendering only. The route re-decides, so a template that offered a preview
        because this said `pdf` still gets refused if the stored bytes have not held up.
        """
        from .services import PREVIEW_INLINE_TYPES
        return PREVIEW_INLINE_TYPES.get(self.verified_type)
    @property
    def revision_number(self):
        number=1
        parent=self.supersedes
        while parent is not None:
            number+=1; parent=parent.supersedes
        return number
    def clean(self):
        if self.person_id and self.person.organization_id != self.organization_id: raise ValidationError("Person must belong to the same organization.")
        if self.document_type_id and self.document_type.organization_id != self.organization_id: raise ValidationError("Document type must belong to the same organization.")
        if self.supersedes_id:
            if self.supersedes_id == self.pk: raise ValidationError("A revision cannot supersede itself.")
            if self.supersedes.organization_id != self.organization_id: raise ValidationError("Superseded record must belong to the same organization.")
            if self.supersedes.document_type_id != self.document_type_id: raise ValidationError("A revision must be of the same record type as the version it replaces.")
            if bool(self.supersedes.person_id) != bool(self.person_id) or (self.person_id and self.supersedes.person_id != self.person_id):
                raise ValidationError("A revision replaces the record filed against the same person, or the same company-wide record.")

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
        # Reversal is its own state rather than a return to CANCELLED: the disposition did execute,
        # and the record book has to say both things — it ran, and then an archived record was
        # brought back by a named actor who gave a reason.
        RESTORED="restored","Reversed — record restored"
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
    restored_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name="restored_dispositions")
    restored_at=models.DateTimeField(null=True,blank=True)
    restore_reason=models.TextField(blank=True)
    class Meta: ordering=["-created_at"]

class MessageConsent(models.Model):
    """One decision by one human, about one destination, kept after it is reversed.

    NTF-4. DD §Email, text messaging, and templates makes "consent and messaging-policy requirements"
    mandatory, and the owner's ruling on 2026-10-03 was that opt-in is captured *at the account's own
    start* — invitation acceptance, first sign-in, or the officer's own text-alerts page.

    A flag on ``Person`` cannot hold this, for two reasons. Consent attaches to the **number**, not the
    person: a phone reassigned by a carrier to somebody else carries the old holder's opt-in with it, and
    texting the new owner is exactly the violation the record exists to prevent. And an opt-out that
    *overwrites* its opt-in destroys the pair of facts a complaint defence needs — that this number was
    asked, agreed, and later withdrew. So every decision is a row, and the current answer is the newest.

    ``wording`` holds the disclosure as it was shown. Consent to a sentence nobody can reproduce is
    consent to nothing, and the sentence is what changes when marketing language is added later.
    """
    class Channel(models.TextChoices):
        SMS="sms","Text message"
        EMAIL="email","Email"
    class State(models.TextChoices):
        GRANTED="granted","Opted in"
        REVOKED="revoked","Opted out"
    class Source(models.TextChoices):
        SIGNUP="signup","Accepted with the account"
        PROFILE="profile","The officer's own settings"
        FIRST_LOGIN="first_login","Answered on first sign-in"
        PROVIDER_KEYWORD="provider_keyword","A reply to the number itself"
        IMPORT="import","A roster import column"
        ADMIN="admin","A manager, on the officer's behalf"
        OFFICER_RECORD="officer_record","Stated on the paper application"
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="message_consents")
    # Nullable with SET_NULL on purpose: the evidence outlives the account. Deleting a person from the
    # roster must not delete the proof that this number was consented, because the complaint arrives
    # after the guard has left.
    person=models.ForeignKey(Person,on_delete=models.SET_NULL,null=True,blank=True,related_name="message_consents")
    channel=models.CharField(max_length=20,choices=Channel.choices,default=Channel.SMS)
    destination=models.CharField(max_length=320,help_text="The number or address this decision is about. Consent travels with it, not with the person.")
    state=models.CharField(max_length=10,choices=State.choices)
    source=models.CharField(max_length=24,choices=Source.choices,default=Source.PROFILE)
    wording=models.TextField(blank=True,help_text="The disclosure shown when the answer was given, recorded as shown.")
    evidence=models.JSONField(default=dict,blank=True,help_text="How the answer arrived: surface, acting user, provider message id, source IP.")
    recorded_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="message_consents_recorded")
    decided_at=models.DateTimeField(default=timezone.now)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=["-decided_at","-created_at"]
        indexes=[models.Index(fields=["organization","channel","destination"])]
    def __str__(self):
        return f"{self.get_channel_display()} {self.destination} — {self.get_state_display()}"

class Suppression(models.Model):
    """A destination the sender is not allowed to reach right now, and why it got that way.

    DD requires suppression to be *processed*, which means it has to be consulted by the send path and
    not merely recorded somewhere. One row per (company, channel, destination): the reason and the
    timestamp stay on the row, and the full callback history is in ``DeliveryEvent``.

    The kinds are deliberately not one "blocked" bucket, because they unblock differently. A wrong-number
    or carrier bounce is a fact about the address; an unsubscribe is a fact about a decision; a spam
    complaint is a fact about reputation and is the one a provider will act on. ``cleared_at`` is set only
    by the paths that legitimately remove a block — an explicit re-consent removes an unsubscribe, nothing
    removes a complaint except the provider's own delisting — and what cleared it is recorded beside it.
    """
    class Kind(models.TextChoices):
        HARD_BOUNCE="hard_bounce","Mailbox rejected the message"
        UNKNOWN_NUMBER="unknown_number","Number is not in service"
        UNSUBSCRIBE="unsubscribe","Asked not to receive these"
        COMPLAINT="complaint","Marked as spam"
        MANUAL="manual","Blocked by an administrator"
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="suppressions")
    channel=models.CharField(max_length=20,choices=MessageConsent.Channel.choices,default=MessageConsent.Channel.SMS)
    destination=models.CharField(max_length=320)
    kind=models.CharField(max_length=20,choices=Kind.choices)
    reason=models.CharField(max_length=255,blank=True)
    provider=models.CharField(max_length=30,blank=True)
    source_reference=models.CharField(max_length=120,blank=True,help_text="The provider id or message that produced this block.")
    since=models.DateTimeField(auto_now_add=True)
    cleared_at=models.DateTimeField(null=True,blank=True)
    cleared_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="suppressions_cleared")
    class Meta:
        ordering=["-since"]
        constraints=[models.UniqueConstraint(fields=["organization","channel","destination"],name="one_suppression_per_destination")]
    def __str__(self):
        return f"{self.destination} — {self.get_kind_display()}"

class DeliveryEvent(models.Model):
    """One provider callback, kept whether or not it changed anything.

    DD §Email, text messaging, and templates: "Delivery, bounce, complaint, unsubscribe, and suppression
    events are processed and retained." Processing without retention is a log line; retention without
    processing is an inbox nobody reads. Both halves are here — the row is the record, and
    ``applied`` says what the sender did about it.

    ``verified`` is stored rather than assumed. Twilio signs its callbacks and the signature is checked
    when a token is configured; SNS and Mailjet document nothing equivalent on this path, so a callback
    from them is accepted on the strength of the organization's own webhook token and marked unverified.
    A reader six months later has to be able to tell those two apart.
    """
    class Kind(models.TextChoices):
        ACCEPTED="accepted","Accepted by the provider"
        DELIVERED="delivered","Delivered"
        BOUNCE="bounce","Bounced"
        COMPLAINT="complaint","Complaint or spam report"
        UNSUBSCRIBE="unsubscribe","Unsubscribe request"
        INBOUND="inbound","A message sent to us"
        FAILED="failed","Could not be delivered"
        STATUS="status","Some other status change"
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="delivery_events")
    provider=models.CharField(max_length=30)
    channel=models.CharField(max_length=20,choices=MessageConsent.Channel.choices,default=MessageConsent.Channel.SMS)
    destination=models.CharField(max_length=320)
    kind=models.CharField(max_length=20,choices=Kind.choices)
    notification=models.ForeignKey("Notification",on_delete=models.SET_NULL,null=True,blank=True,related_name="delivery_events")
    message_reference=models.CharField(max_length=160,blank=True,help_text="The provider's own id for the message, so a support ticket can find it.")
    detail=models.CharField(max_length=255,blank=True)
    occurred_at=models.DateTimeField(null=True,blank=True)
    applied=models.BooleanField(default=False,help_text="Whether this callback changed a send or a consent.")
    verified=models.BooleanField(default=False,help_text="Signed by the provider, as opposed to delivered to the right token.")
    raw=models.JSONField(default=dict,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=["-created_at"]
        indexes=[models.Index(fields=["organization","channel","destination"])]
    def __str__(self):
        return f"{self.provider} {self.get_kind_display()} {self.destination}"

# Who a recipient is *in relation to* a notice. Not the same thing as their role: the site
# supervisor whose own licence lapsed is being told as a person first, and a rule that texted
# "supervisors" about licence lapses would text them in the voice of a manager chasing somebody
# else. `services.notification_audience` resolves one recipient to exactly one of these.
class ChannelAudience(models.TextChoices):
    SUBJECT = "subject", "The person the notice is about"
    COMPLIANCE = "compliance", "Owner, administrator, HR / compliance"
    DISPATCH = "dispatch", "Scheduler, dispatcher, site supervisor"
    PAYROLL = "payroll", "Payroll approver"
    AUDIT = "audit", "Read-only auditor"
    WORKER = "worker", "Any other recipient"

# The event families in use, taken from the `event_type` prefixes every call site already writes.
# A rule is scoped to one of these rather than to free text because the settings page has to list
# what can be configured — an empty text box invites a typo, and a typo in a channel rule means a
# notice nobody ever receives, discovered only when somebody says they were never told.
NOTIFICATION_FAMILIES = (
    ("credential", "Credentials and compliance"),
    ("training", "Training"),
    ("punch", "Clock punches and exceptions"),
    ("shift", "Posts, swaps and trades"),
    ("timeoff", "Time off"),
    ("payroll", "Payroll periods"),
    ("document", "Documents and acknowledgments"),
    ("retention", "Retention, holds and disposal"),
    ("membership", "Accounts and invitations"),
    ("onboarding", "Onboarding tasks"),
    ("import", "Data imports"),
    ("audit", "Audit history and sealing"),
    ("message", "Text-message consent"),
    ("organization", "Company settings"),
)

# Which audience a membership role falls into when a notice reaches it. The officer the notice is
# *about* is checked first and wins — see `services.notification_audience` for why that ordering is
# the whole design rather than a detail.
AUDIENCE_ROLES = {
    ChannelAudience.COMPLIANCE.value: (Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR),
    ChannelAudience.DISPATCH.value: (Membership.Role.SCHEDULER, Membership.Role.SUPERVISOR),
    ChannelAudience.PAYROLL.value: (Membership.Role.PAYROLL,),
    ChannelAudience.AUDIT.value: (Membership.Role.AUDITOR,),
}


class ChannelRule(models.Model):
    """Which channels one audience hears one family of notices on.

    DD §Email, text messaging, and templates asks for delivery channels to be configurable
    "per-audience"; today every notice goes in-app plus email to every recipient whatever they are,
    and the SMS adapter — written, tested, and consent-gated by NTF-4 — has never been asked for by a
    call site. That gap is the difference between a licence warning that reaches a guard's pocket and
    a licence warning that reaches the office inbox of somebody who cannot renew it.

    The rule *selects*, it does not *permit*. Whether a selected channel may actually be used is
    still decided per message by `send_block_reason` (NTF-4): a rule that puts SMS on a family is a
    statement about who should be texted, not a licence to text anybody. That split is deliberate and
    load-bearing — consent belongs to the person and their number, channel preference belongs to the
    company, and neither should be able to overrule the other.

    `channels` may be empty, which means "in-app only, nothing outbound". In-app is always delivered
    whatever the rule says (`services.rule_channels` adds it back), because the notification centre is
    the durable copy: a text a carrier dropped leaves no evidence the company told anybody.
    """
    # The audience vocabulary is module-level because `services.notification_audience` answers the
    # same question for a recipient who has no rule in sight, and an enum defined inside this model
    # would make the resolver depend on a settings table it has no business reading.
    Audience = ChannelAudience

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="channel_rules")
    audience = models.CharField(max_length=20, choices=Audience.choices)
    family = models.CharField(max_length=20, choices=NOTIFICATION_FAMILIES)
    # An exact event type narrows a family rule: "text officers about `credential.reminder`, but
    # nothing else in the credential family". Stored as text, matched exactly, and validated against
    # the family so `credential.reminder` cannot be filed under `payroll` and become unfindable.
    event_type = models.CharField(max_length=100, blank=True, help_text="Leave empty to cover the whole family.")
    channels = models.JSONField(default=list, blank=True, help_text="Email, SMS, or neither (in-app only). In-app always arrives and cannot be switched off.")
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="channel_rules")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["audience", "family", "event_type"]
        constraints = [models.UniqueConstraint(fields=["organization", "audience", "family", "event_type"],
                                               name="one_channel_rule_per_audience_and_event")]

    def __str__(self):
        return f"{self.get_audience_display()} · {self.family}{('.' + self.event_type) if self.event_type else ''}"

    @property
    def scope_label(self):
        return self.event_type if self.event_type else f"{self.family}.*"

    def matches(self, event_type):
        """Whether this rule speaks for one event type — the narrowest voice wins.

        Matching lives on the row rather than in a queryset filter so the same rule of precedence
        decides both "which rule applies" and "which rule is shown as the reason" on the settings
        page. An exact event type beats a family rule; that is the only precedence rule, so two rules
        can never both claim to answer one notice.
        """
        if not self.active:
            return False
        text = str(event_type or "")
        if self.event_type:
            return text == self.event_type
        return text.startswith(f"{self.family}.")

    def clean(self):
        if self.event_type:
            if not self.event_type.startswith(f"{self.family}."):
                raise ValidationError({"event_type":
                    f"“{self.event_type}” is not part of the {self.family} family. Pick the family it belongs to, "
                    "or clear this field to cover the whole family."})
        allowed = {value for value, _ in Notification.Channel.choices}
        unknown = [channel for channel in (self.channels or []) if channel not in allowed]
        if unknown:
            raise ValidationError({"channels": "Unknown channel: " + ", ".join(unknown)})
        if "in_app" in (self.channels or []):
            raise ValidationError({"channels":
                "In-app is delivered on every notice and cannot be chosen here — this rule decides what goes *out*."})


class SmsTemplate(models.Model):
    """One company's own wording for one kind of text message.

    The built-in wording lives in `core.sms.SMS_NOTICES`; a row here replaces it for this company
    only, and deleting the row is "reset to default". Keyed by the notice key rather than the event
    type because one event can reach two people who need different sentences — the officer whose
    swap was approved and the colleague now covering it.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="sms_templates")
    notice_key = models.CharField(max_length=100)
    body = models.TextField()
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="sms_templates")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["notice_key"]
        constraints = [models.UniqueConstraint(fields=["organization", "notice_key"],
                                               name="one_sms_template_per_notice")]

    def __str__(self):
        return f"{self.organization} · {self.notice_key}"


class EmailTemplate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="email_templates")
    notice_key = models.CharField(max_length=100)
    subject = models.CharField(max_length=200)
    body = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                  null=True, blank=True, related_name="email_templates")

    class Meta:
        ordering = ["notice_key"]
        constraints = [models.UniqueConstraint(fields=["organization", "notice_key"],
                                               name="one_email_template_per_notice")]


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
        BLOCKED="blocked","Not sent — consent or suppression"
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="notifications")
    recipient=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE,null=True,blank=True,related_name="workforce_notifications")
    destination=models.CharField(max_length=320,blank=True,help_text="Explicit email address or phone number for recipients without an account.")
    channel=models.CharField(max_length=20,choices=Channel.choices,default=Channel.IN_APP)
    event_type=models.CharField(max_length=100)
    subject=models.CharField(max_length=200)
    body=models.TextField()
    status=models.CharField(max_length=20,choices=Status.choices,default=Status.QUEUED)
    # NTF-4's categorisation, and the reason it is a column rather than a guess from `event_type`: DD
    # requires mandatory operational messages to be categorised separately from optional ones, and
    # RB §Email and SMS providers warns not to assume email-unsubscribe rules and mandatory
    # operational-SMS rules are the same. They are not, and the difference has to be decided per
    # message at the moment it is queued — by the code that knows whether an officer's licence lapsed
    # is a notice or a courtesy.
    mandatory=models.BooleanField(default=False,help_text="Required operational or security notice. Optional notices obey unsubscribes and bounces; this one still goes by email, but never by text to an opted-out number.")
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

class Shift(PostOrdersMixin, models.Model):
    post_orders_field = "post_orders"
    post_orders_level = "post"

    @property
    def post_orders_parent(self):
        return self.site

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
    # A rate on the assignment is the exception that proves the rule: holiday cover, a
    # differential, or a client who agreed to a one-off premium. Both stay nullable so the
    # resolution order (shift, site, client, officer) is visible rather than pre-filled.
    pay_rate = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True, help_text="Overrides the site, client, and officer rate for this post only.")
    bill_rate = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True, help_text="Overrides the site and client rate charged for this post.")
    pay_code = models.ForeignKey(PayCode, on_delete=models.SET_NULL, null=True, blank=True, related_name="shifts",
        help_text="Overrides the site and contract code for this post only.")
    required_credentials = models.ManyToManyField(CredentialType, blank=True, related_name="required_for_shifts")
    # Retiring a recurring series must not erase the roster it built: an individual post is often
    # edited, filled, or already worked by the time the pattern behind it goes away, and the
    # schedule is the firm's coverage record, not the template's property.
    template = models.ForeignKey("ShiftTemplate", on_delete=models.SET_NULL, null=True, blank=True, related_name="shifts")
    # SCH-3's split tour. Two officers standing one post in a day is ordinary in this trade — a
    # 12-hour gate covered 18:00–00:00 and 00:00–06:00, or one officer taking over mid-tour when the
    # relief is late — and it has to be two posts, because each officer needs a post to clock onto
    # and the client is billed per officer. But two overlapping posts at one site are also just two
    # posts, so the pairing would be an inference nobody makes. Naming the half that takes over says
    # the tour was handed on, which is what a pass-down and a dispute both ask about.
    relief_for = models.ForeignKey("core.Shift", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="relief_halves",
        help_text="Set when this post takes over another officer's tour partway through, or is the second half of it.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering = ["starts_at"]
        indexes = [models.Index(fields=["organization", "starts_at"])]
        constraints = [models.UniqueConstraint(fields=["template", "starts_at"], name="one_generated_post_per_occurrence")]

    def __str__(self):
        start = timezone.localtime(self.starts_at) if timezone.is_aware(self.starts_at) else self.starts_at
        end = timezone.localtime(self.ends_at) if timezone.is_aware(self.ends_at) else self.ends_at
        assigned = self.officer
        officer = assigned.full_name if assigned is not None else "Open post"
        post = f" / {self.post_name}" if self.post_name else ""
        return f"{self.site}{post} - {start:%b %d, %Y %H:%M} to {end:%b %d, %Y %H:%M} - {officer}"

    def clean(self):
        if self.ends_at and self.starts_at and self.ends_at <= self.starts_at:
            raise ValidationError("Shift end must be after shift start.")
        if self.site_id and self.site.organization_id != self.organization_id:
            raise ValidationError("Site must belong to the same organization.")
        if self.officer_id and self.officer.organization_id != self.organization_id:
            raise ValidationError("Officer must belong to the same organization.")
        if self.relief_for_id:
            parent = self.relief_for
            if parent.pk == self.pk:
                raise ValidationError("A post cannot relieve itself.")
            if parent.organization_id != self.organization_id:
                raise ValidationError("The tour being relieved must belong to the same organization.")
            if parent.site_id != self.site_id:
                raise ValidationError("A split tour is one post at one site — the half that takes over "
                                      "stands at the same site as the half it relieves.")
            # Adjacent halves (a 00:00 start taking over an 18:00–00:00 first half) and a mid-tour
            # takeover both count. A post starting before the one it relieves, or after it has
            # finished, is a separate tour, and naming that a relief would misdescribe the roster.
            if self.starts_at and not (parent.starts_at <= self.starts_at <= parent.ends_at):
                raise ValidationError(
                    f"This post starts outside the tour it relieves ({parent.starts_at:%H:%M}–{parent.ends_at:%H:%M}).")

class ShiftClaim(models.Model):
    """An officer's request to take a published post that nobody is assigned to.

    An open post is the normal state of a guard schedule — the coverage gap is what the
    dispatcher is trying to fill — so publishing must not require an officer. The credential
    gate then moves to the moment of assignment, which is why approval re-checks eligibility
    instead of trusting the state at request time.
    """
    class Status(models.TextChoices):
        REQUESTED="requested","Requested"
        APPROVED="approved","Approved"
        REJECTED="rejected","Rejected"
        WITHDRAWN="withdrawn","Withdrawn"
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="shift_claims")
    shift=models.ForeignKey(Shift,on_delete=models.CASCADE,related_name="claims")
    officer=models.ForeignKey(Person,on_delete=models.CASCADE,related_name="shift_claims")
    status=models.CharField(max_length=20,choices=Status.choices,default=Status.REQUESTED)
    note=models.CharField(max_length=255,blank=True)
    decided_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="decided_shift_claims")
    decided_at=models.DateTimeField(null=True,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=["-created_at"]
        constraints=[models.UniqueConstraint(fields=["shift","officer"],condition=models.Q(status="requested"),name="one_open_claim_per_shift_officer")]
    def clean(self):
        if self.shift_id and self.shift.organization_id != self.organization_id:
            raise ValidationError("Shift must belong to the same organization.")
        if self.officer_id and self.officer.organization_id != self.organization_id:
            raise ValidationError("Officer must belong to the same organization.")

class ShiftTemplate(PostOrdersMixin, models.Model):
    post_orders_field = "post_orders"
    post_orders_level = "series"

    @property
    def post_orders_parent(self):
        return self.site

    """A repeating tour that produces dated posts, so a contract is staffed once, not every week.

    ``docs/discovery-decisions.md`` puts recurring templates in the first release, and every peer
    surveyed ships one (Deputy's "Shift templates", Homebase's and When I Work's repeat
    schedules). The guard version of the job is mechanical: the same tour at the same post, seven
    days a week, for the length of a contract.

    A template stores the *shape* of a post and never its money. Each generated row keeps its own
    ``pay_rate``/``bill_rate`` empty, so the rate resolves through site → contract → officer every
    time the schedule is read; a series frozen at June's rate would silently bill December's posts
    at last year's number after a renegotiation.
    """
    class Pattern(models.TextChoices):
        WEEKLY="weekly","Every week"
        FORTNIGHTLY="fortnightly","Every other week"
        CYCLE="cycle","A rotating cycle of days"
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="shift_templates")
    name=models.CharField(max_length=140,help_text="Names the pattern on the schedule, such as “Gate 1 night tour”.")
    site=models.ForeignKey(Site,on_delete=models.PROTECT,related_name="shift_templates")
    officer=models.ForeignKey(Person,on_delete=models.SET_NULL,null=True,blank=True,related_name="recurring_posts",help_text="Optional. Leave empty to generate each post open, so a qualified officer can ask to take it.")
    start_time=models.TimeField()
    end_time=models.TimeField(help_text="A time earlier than the start means the tour finishes the next morning, which is how a 18:00–06:00 post is stated.")
    weekdays=models.JSONField(default=list,blank=True,help_text="The days of the week this pattern produces.")
    pattern=models.CharField(max_length=16,choices=Pattern.choices,default=Pattern.WEEKLY)
    # A rotating pattern is not "which weekdays" — it is "which days of a cycle", and the cycle's
    # length does not have to be seven. 4-on/4-off is an eight-day cycle, so its days drift one
    # weekday forward every time round; 2-2-3 is fourteen days; DuPont is twenty-eight. A
    # weekday-anchored rule cannot express any of them, which is why guardhouses today make two
    # templates per fortnight and keep the rotation in their heads.
    alias=models.CharField(max_length=80,blank=True,help_text="What the firm calls it — “2s and 3s”, “Panama”, “the nights block”. Names are not standardised, so this is text, not a choice.")
    cycle_days=models.PositiveSmallIntegerField(null=True,blank=True,help_text="How many days the rotation takes to repeat. Only used by a rotating cycle.")
    cycle_work_days=models.JSONField(default=list,blank=True,help_text="Which days of the cycle are worked, counted from the first day of the series. Empty days are off.")
    series_start=models.DateField(help_text="The week the pattern begins. An every-other-week series counts its phase from this date, so generating a later range does not move the alternating weekends.")
    series_end=models.DateField(null=True,blank=True,help_text="Leave empty for an open-ended series. When set, the pattern produces no post after this date and generation can be run up to it in one action.")
    post_name=models.CharField(max_length=140,blank=True)
    post_orders=models.TextField(blank=True)
    required_credentials=models.ManyToManyField(CredentialType,blank=True,related_name="required_for_templates")
    active=models.BooleanField(default=True,help_text="Paused series generate no further posts. Posts already produced stay on the schedule either way.")
    # Off unless a dispatcher sets it. TrackTik's guard scheduling is rolling ("repeatedly apply
    # until the end of the service period") while every generic tool surveyed is view- or
    # range-based, so this is the vertical's behaviour rather than an invention — but it writes
    # assignment rows with nobody watching, which is why the default is that nobody does it.
    auto_generate_days=models.PositiveSmallIntegerField(null=True,blank=True,
        help_text="Set to keep this series filled unattended that many days ahead, never past the end date. Leave empty to generate only from the screen.")
    auto_publish=models.BooleanField(default=False,
        help_text="Only an unattended run reads this: drafts hold the coverage, published announces it.")
    created_at=models.DateTimeField(auto_now_add=True)
    updated_at=models.DateTimeField(auto_now=True)

    class Meta:
        ordering=["name"]

    def __str__(self):
        return self.name

    def clean(self):
        if self.start_time and self.end_time and self.start_time == self.end_time:
            raise ValidationError("A tour cannot start and end at the same time.")
        days = self.weekdays or []
        if self.pattern == self.Pattern.CYCLE:
            if not self.cycle_days or not 2 <= self.cycle_days <= 60:
                raise ValidationError({"cycle_days": "A rotating cycle needs a length between 2 and 60 days."})
            work = self.cycle_work_days or []
            if not work:
                raise ValidationError({"cycle_work_days": "Choose which days of the cycle are worked."})
            if any(not isinstance(value, int) or not 0 <= value < self.cycle_days for value in work):
                raise ValidationError({"cycle_work_days": "Every worked day must be counted from the first day of the cycle."})
        elif not days:
            raise ValidationError({"weekdays": "Choose at least one day of the week."})
        if days and any(not isinstance(value, int) or not 0 <= value <= 6 for value in days):
            raise ValidationError({"weekdays": "Days must be Monday through Sunday."})
        if self.series_end and self.series_start and self.series_end < self.series_start:
            raise ValidationError({"series_end": "The series cannot end before the week it starts."})
        if self.site_id and self.site.organization_id != self.organization_id:
            raise ValidationError("Site must belong to the same organization.")
        if self.officer_id and self.officer.organization_id != self.organization_id:
            raise ValidationError("Officer must belong to the same organization.")

    @property
    def cycle_work_indexes(self):
        return sorted({value for value in (self.cycle_work_days or [])
                       if isinstance(value, int) and 0 <= value < (self.cycle_days or 0)})

    @property
    def pattern_label(self):
        if self.pattern == self.Pattern.CYCLE:
            return f"Day {', '.join(str(value + 1) for value in self.cycle_work_indexes)} of {self.cycle_days}"
        return self.get_pattern_display()

    @property
    def cycle_note(self):
        """Whether the cycle's days move through the week, which is the point of a rotation."""
        if self.pattern != self.Pattern.CYCLE or not self.cycle_days:
            return ""
        drift = self.cycle_days % 7
        if not drift:
            return "Every round lands on the same weekdays."
        return (f"The days move {drift} weekday{'' if drift == 1 else 's'} forward each round, so this "
                "pattern is not tied to particular days of the week.")

    @property
    def overnight(self):
        return bool(self.start_time and self.end_time and self.end_time <= self.start_time)

    @property
    def day_labels(self):
        if self.pattern == self.Pattern.CYCLE:
            return [f"day {value + 1} of {self.cycle_days}" for value in self.cycle_work_indexes]
        return [WEEKDAY_LABELS[value] for value in self.day_indexes]

    @property
    def day_indexes(self):
        return sorted({value for value in (self.weekdays or []) if isinstance(value, int) and 0 <= value <= 6})

    @property
    def window_label(self):
        return f"{self.start_time:%H:%M}–{self.end_time:%H:%M}" + (" next day" if self.overnight else "")

    def window_for(self, day):
        """The concrete start and end instants the pattern produces for one calendar day.

        Wall times, not durations: a 12-hour tour across a DST boundary is eleven or thirteen
        clock hours, and the payroll side measures the punches rather than this arithmetic, so
        the schedule stays honest about when the officer was expected at the post.
        """
        from django.utils import timezone
        start = timezone.make_aware(datetime.combine(day, self.start_time))
        end_day = day + timedelta(days=1) if self.overnight else day
        return start, timezone.make_aware(datetime.combine(end_day, self.end_time))

    def occurrences(self, range_start, range_end):
        """Every dated window the pattern produces inside an inclusive range of calendar days.

        The series' own end date is applied here rather than in each caller, so a preview, an
        apply, and the unattended run cannot disagree about when the pattern stops.
        """
        if range_end < range_start:
            return []
        if self.series_end and range_end > self.series_end:
            range_end = self.series_end
        if range_end < range_start:
            return []
        if self.pattern == self.Pattern.CYCLE:
            if not self.cycle_work_indexes:
                return []
            # Day 0 of the cycle is the first day of the series, so a second crew on the same post
            # is the same cycle with a different start date — which is how a four-crew 24/7 post is
            # stood without writing the pattern four times.
            worked = set(self.cycle_work_indexes)
            produced = []
            offset = (range_start - self.series_start).days
            if offset < 0:
                offset = ((offset % self.cycle_days) + self.cycle_days) % self.cycle_days
            for index in range((range_end - range_start).days + 1):
                day = range_start + timedelta(days=index)
                if (offset + index) % self.cycle_days in worked:
                    produced.append(self.window_for(day))
            return produced
        anchor = self.series_start - timedelta(days=self.series_start.weekday())
        week = range_start - timedelta(days=range_start.weekday())
        produced = []
        while week <= range_end:
            alternate = ((week - anchor).days // 7) % 2 == 1
            if not (self.pattern == self.Pattern.FORTNIGHTLY and alternate):
                for index in self.day_indexes:
                    day = week + timedelta(days=index)
                    if range_start <= day <= range_end:
                        produced.append(self.window_for(day))
            week += timedelta(days=7)
        return produced

    @property
    def last_generated_on(self):
        """The latest start date this series has already produced, or None."""
        from django.utils import timezone
        latest = self.shifts.order_by("-starts_at").values_list("starts_at", flat=True).first()
        return timezone.localtime(latest).date() if latest else None

    def next_window(self, today, horizon=SERIES_HORIZON_DAYS, maximum=SERIES_MAX_DAYS):
        """The next range worth generating, so filling the same roster again is one click.

        Starts the day after the last post this series produced (or today, if that is later — a
        paused series can be picked up mid-contract without back-filling the past). The far end is
        measured from **today**, not from that start: "keep this post filled four weeks ahead" means
        ``today + 28``, so a run that finds the window already covered returns ``None`` and the
        unattended pass stops instead of chasing an ever-receding horizon.

        ``maximum`` keeps the automatic path inside the same cap a human must preview under, so an
        unattended run can never write a wider range than the screen would allow.
        """
        if self.series_end and self.series_end < today:
            return None
        last = self.last_generated_on
        start = today if last is None else max(today, last + timedelta(days=1))
        ends = [today + timedelta(days=horizon), start + timedelta(days=maximum)]
        if self.series_end:
            ends.append(self.series_end)
        end = min(ends)
        if end < start:
            return None
        return start, end

    @property
    def term_label(self):
        if self.series_end:
            return f"{self.series_start:%d %b %Y} – {self.series_end:%d %b %Y}"
        return f"from {self.series_start:%d %b %Y}, open-ended"

class ShiftSwap(models.Model):
    """A filled post its officer hands to one colleague — an *offer*, not a two-way swap.

    Connecteam's own distinction names the two things the industry keeps separate: "**Offer Shift:
    a one-way handoff.** The employee gives the shift to a qualified teammate and is no longer
    scheduled for it" versus "**Swap Shift: a two-way trade.** … both stay scheduled and both keep
    their planned hours for the week." This row is the first; :class:`ShiftExchange` is the second.
    Keeping them apart is what stops a one-leg handoff being approved as if a colleague had also
    handed something back.

    Eligibility is not tested when the offer is made, only at approval, for the same reason
    ``shift_claim_decide`` re-checks: a replacement who is short a document today may renew it
    before the tour, and a credential that lapsed overnight is exactly what a Texas post cannot
    absorb. The offer screen still *filters* the candidate list, because Deputy and When I Work
    both do, and an officer who offers a post to someone who could never take it has wasted the
    colleague's attention and the manager's queue.
    """
    class Status(models.TextChoices):
        OFFERED="offered","Waiting for the colleague"
        AGREED="agreed","Waiting for a manager"
        APPROVED="approved","Approved"
        DECLINED="declined","Declined by the colleague"
        REFUSED="refused","Refused by a manager"
        WITHDRAWN="withdrawn","Withdrawn"
        EXPIRED="expired","Expired before an answer"
    class RaisedBy(models.TextChoices):
        OFFICER="officer","The officer standing the post"
        MANAGER="manager","A manager, on the officer's behalf"
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="shift_swaps")
    shift=models.ForeignKey(Shift,on_delete=models.CASCADE,related_name="swaps")
    requester=models.ForeignKey(Person,on_delete=models.CASCADE,related_name="swaps_offered")
    replacement=models.ForeignKey(Person,on_delete=models.CASCADE,related_name="swaps_received")
    status=models.CharField(max_length=20,choices=Status.choices,default=Status.OFFERED)
    # Who asked for the move is recorded because it is a fact about the consent chain, not about
    # who used the keyboard: a manager raising it on an officer's behalf changes neither colleague's
    # obligation to agree. Deputy keeps these separate ("Manager Initiated Swaps" versus a manager
    # simply editing the assignee), and the difference is whether a second consent exists at all.
    raised_by=models.CharField(max_length=12,choices=RaisedBy.choices,default=RaisedBy.OFFICER)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="raised_shift_swaps")
    note=models.CharField(max_length=255,blank=True,help_text="Why the post needs moving. The colleague and the manager both read this.")
    review_note=models.CharField(max_length=255,blank=True)
    agreed_at=models.DateTimeField(null=True,blank=True)
    decided_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="decided_shift_swaps")
    decided_at=models.DateTimeField(null=True,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering=["-created_at"]
        # MySQL skips a conditional constraint (models.W036), so the routes refuse a duplicate
        # open offer outright rather than relying on this as the only defence.
        constraints=[models.UniqueConstraint(fields=["shift","replacement"],condition=models.Q(status__in=("offered","agreed")),name="one_open_swap_per_post_and_colleague")]

    def __str__(self):
        return f"{self.requester} handing {self.shift} to {self.replacement}"

    def clean(self):
        if self.shift_id and self.shift.organization_id != self.organization_id:
            raise ValidationError("Shift must belong to the same organization.")
        for person, label in ((self.requester, "requester"), (self.replacement, "replacement")):
            if getattr(self, f"{label}_id") and person.organization_id != self.organization_id:
                raise ValidationError(f"The {label} belongs to another organization.")
        if self.requester_id and self.requester_id == self.replacement_id:
            raise ValidationError("An officer cannot offer a post to themselves.")

    @property
    def is_open(self):
        return self.status in (self.Status.OFFERED, self.Status.AGREED)

    @property
    def parties(self):
        return {self.requester_id, self.replacement_id}

    def involved_user_ids(self):
        return {item.user_id for item in (self.requester, self.replacement) if item.user_id}


class ShiftExchange(models.Model):
    """Two posts traded between two officers, raised and decided as one transaction.

    Every peer that ships a real exchange makes the pair a single decision, and the reason is
    coverage rather than fairness: approve one leg on its own and one officer is released from a
    post while nobody has taken the other. Connecteam states the commit rule as the spec — "**Only
    then is the original shift removed from the employee and given to the other**" — and Deputy
    forbids trading one shift for several of a colleague's. This object therefore carries both legs
    and has exactly one approval, so a half-approved exchange cannot be expressed at all.

    The officer who is asked to trade chooses **which of their own posts** to put in
    (``partner_shift``), the way Deputy and When I Work have the responder collapse several
    candidates into one pair. The proposer does not get to pick a colleague's post for them.
    """
    class Status(models.TextChoices):
        PROPOSED="proposed","Waiting for the colleague to choose a post"
        AGREED="agreed","Waiting for a manager"
        APPROVED="approved","Approved"
        DECLINED="declined","Declined by the colleague"
        REFUSED="refused","Refused by a manager"
        WITHDRAWN="withdrawn","Withdrawn"
        EXPIRED="expired","Expired before an answer"
    class RaisedBy(models.TextChoices):
        OFFICER="officer","One of the two officers"
        MANAGER="manager","A manager, on their behalf"
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="shift_exchanges")
    initiator=models.ForeignKey(Person,on_delete=models.CASCADE,related_name="exchanges_proposed")
    initiator_shift=models.ForeignKey(Shift,on_delete=models.CASCADE,related_name="exchanges_initiated")
    partner=models.ForeignKey(Person,on_delete=models.CASCADE,related_name="exchanges_invited")
    partner_shift=models.ForeignKey(Shift,on_delete=models.CASCADE,null=True,blank=True,related_name="exchanges_partnered",
        help_text="Chosen by the colleague being asked to trade, not by the officer who proposed it.")
    status=models.CharField(max_length=20,choices=Status.choices,default=Status.PROPOSED)
    raised_by=models.CharField(max_length=12,choices=RaisedBy.choices,default=RaisedBy.OFFICER)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="raised_shift_exchanges")
    note=models.CharField(max_length=255,blank=True)
    review_note=models.CharField(max_length=255,blank=True)
    agreed_at=models.DateTimeField(null=True,blank=True)
    decided_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="decided_shift_exchanges")
    decided_at=models.DateTimeField(null=True,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering=["-created_at"]
        constraints=[
            models.UniqueConstraint(fields=["initiator","initiator_shift"],condition=models.Q(status__in=("proposed","agreed")),name="one_open_exchange_per_post"),
        ]

    def __str__(self):
        return f"{self.initiator} ⇄ {self.partner}"

    def clean(self):
        if self.initiator_id == self.partner_id:
            raise ValidationError("An officer cannot exchange a post with themselves.")
        for person, label in ((self.initiator, "initiator"), (self.partner, "partner")):
            if getattr(self, f"{label}_id") and person.organization_id != self.organization_id:
                raise ValidationError(f"The {label} belongs to another organization.")
        if self.initiator_shift_id:
            if self.initiator_shift.organization_id != self.organization_id:
                raise ValidationError("The post offered belongs to another organization.")
            if self.initiator_shift.officer_id != self.initiator_id:
                raise ValidationError("An officer can only propose a post they are standing.")
        if self.partner_shift_id:
            if self.partner_shift.organization_id != self.organization_id:
                raise ValidationError("The traded post belongs to another organization.")
            if self.partner_shift.officer_id != self.partner_id:
                raise ValidationError("The colleague can only put in a post they are standing.")
            if self.partner_shift_id == self.initiator_shift_id:
                raise ValidationError("An exchange needs two different posts.")

    @property
    def is_open(self):
        return self.status in (self.Status.PROPOSED, self.Status.AGREED)

    @property
    def parties(self):
        return {self.initiator_id, self.partner_id}

    def involved_user_ids(self):
        return {item.user_id for item in (self.initiator, self.partner) if item.user_id}

    @property
    def legs(self):
        """(post, officer giving it up, officer taking it) — empty until the pair is complete."""
        if not self.partner_shift_id:
            return []
        return [(self.initiator_shift, self.initiator, self.partner),
                (self.partner_shift, self.partner, self.initiator)]

class AvailabilityRule(models.Model):
    """A weekly window in which an officer can be stood.

    ``docs/discovery-decisions.md`` puts availability in the first release and requires
    assignment validation to combine it with credentials, training, and site requirements. It is
    configuration, not a legal gate: a post outside every window is flagged to the dispatcher
    rather than forbidden, because a coverage gap is not solved by hiding the officer who could
    fill it. A window with no rows at all means the person has stated nothing.
    """
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="availability_rules")
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="availability_rules")
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAY_CHOICES)
    starts_at = models.TimeField()
    ends_at = models.TimeField(help_text="Earlier than the start means the window runs past midnight into the next day.")

    class Meta:
        ordering = ["weekday", "starts_at"]
        constraints = [models.UniqueConstraint(fields=["person", "weekday", "starts_at"], name="one_availability_window_per_start")]

    def clean(self):
        if self.person_id and self.person.organization_id != self.organization_id:
            raise ValidationError("Person must belong to the same organization.")
        if self.starts_at == self.ends_at:
            raise ValidationError("An availability window cannot start and end at the same time.")

    def windows(self, date):
        """The concrete start and end instants this weekly pattern produces for a given week.

        An overnight window is anchored to the day it *starts*, so it is tested from both the
        night it opens and the morning it closes; a guard stood 18:00–06:00 states one window,
        not two, which is how they think about the job.
        """
        from django.utils import timezone
        week_start = date - timedelta(days=date.weekday())
        zone = timezone.get_default_timezone()
        produced = []
        for offset in range(7):
            day = week_start + timedelta(days=offset)
            if day.weekday() != self.weekday:
                continue
            start = datetime.combine(day, self.starts_at, tzinfo=zone)
            if self.ends_at > self.starts_at:
                end = datetime.combine(day, self.ends_at, tzinfo=zone)
            else:
                end = datetime.combine(day + timedelta(days=1), self.ends_at, tzinfo=zone)
            produced.append((start, end))
        return produced

    def covers(self, moment):
        for day in (moment.date(), moment.date() - timedelta(days=1)):
            if any(start <= moment <= end for start, end in self.windows(day)):
                return True
        return False

    def __str__(self):
        return f"{WEEKDAY_LABELS[self.weekday]} {self.starts_at:%H:%M}–{self.ends_at:%H:%M}"


class TimeOffRequest(models.Model):
    """A request to be absent, decided by a manager.

    Approved leave blocks *assignment* — a guard who was granted the week off cannot be put on a
    post in it — while recorded punches stay evidence of time actually worked, so the clock is
    never refused by this rule.
    """
    class Status(models.TextChoices):
        REQUESTED="requested","Requested"
        APPROVED="approved","Approved"
        DECLINED="declined","Declined"
        CANCELLED="cancelled","Cancelled"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="time_off_requests")
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="time_off_requests")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REQUESTED)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="requested_time_off")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="decided_time_off")
    decided_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["starts_at"]
        indexes = [models.Index(fields=["organization", "person", "starts_at"])]

    def clean(self):
        if self.person_id and self.person.organization_id != self.organization_id:
            raise ValidationError("Person must belong to the same organization.")
        if self.ends_at and self.starts_at and self.ends_at <= self.starts_at:
            raise ValidationError("Leave must end after it starts.")

    def collides_with(self, shift):
        return shift.starts_at < self.ends_at and shift.ends_at > self.starts_at

    def __str__(self):
        return f"{self.person} off {self.starts_at:%Y-%m-%d}–{self.ends_at:%Y-%m-%d}"


class PayCategory(models.Model):
    """What a kind of hour *is* for pay: paid or not, at what multiple, and whether it counts
    toward the overtime threshold. PAY-2.

    The kinds are fixed because the payroll arithmetic has to be able to find them; the
    three properties are editable because the answers genuinely differ between firms and even
    between contracts. A mandatory pre-assignment briefing is hours worked and paid (29 CFR
    785.27 excludes training only when it is outside normal hours, voluntary, not job-related,
    *and* produces no work — a dispatcher debrief fails three of the four), while a bona fide
    meal period is unpaid and is not time worked. Neither belongs hard-coded: DD says do not
    encode a jurisdiction's numbers in code, so the defaults are a starting reading that carries
    the same version stamp the clock policy got — a payroll line can be traced back to the
    multiplier that priced it rather than to whoever saved the row last.

    The three fields are not independent, and the combinations are the whole design:

    * ``paid=False, counts_toward_overtime=False`` — a genuine meal break. Time comes *out* of the
      tour, so it also lowers the week's total and can un-charge overtime for later posts.
    * ``paid=True, counts_toward_overtime=False`` — hours that pay at their own rate but are not
      worked time for the threshold (a firm that treats travel that way, say).
    * ``paid=True, counts_toward_overtime=True`` — the premium case: the hours stay in the worked
      total, and this row prices only what sits *above* straight time, so a category line and the
      worked line together equal the pay without counting the hour twice.
    """
    class Kind(models.TextChoices):
        BREAK = "break", "Break"
        HOLIDAY = "holiday", "Holiday"
        TRAINING = "training", "Training"
        TRAVEL = "travel", "Travel"
        DOUBLE_TIME = "double_time", "Double time"
        # The last kind the payroll baseline in RB §Payroll export baseline asks for. A night or
        # weekend differential is a premium, not extra hours, so it prices the excess over straight
        # time like holiday does — which is why a flat "$1.50 an hour" is entered here as the
        # equivalent multiple and the arithmetic behind it goes in `interpretation`.
        DIFFERENTIAL = "differential", "Differential"
        # Not a designation: nobody marks "leave hours" on a post, because approved leave arrives
        # from a decided TimeOffRequest rather than from somebody's assertion. It lives here anyway
        # so the *paidness* of leave is the firm's rule with a version, like every other kind —
        # DD §Time calculation requires that configuration be configurable, and PAY-2's ruling prices
        # leave on the hours it displaced, which needs a multiple to price them at.
        LEAVE = "leave", "Approved leave"
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="pay_categories")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    name = models.CharField(max_length=140)
    paid = models.BooleanField(default=True, help_text="Unpaid hours are deducted from the tour, which also lowers the week's total.")
    multiplier = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal("1.00"), help_text="Rate as a multiple of the officer's pay rate.")
    counts_toward_overtime = models.BooleanField(default=True, help_text="Whether these hours count toward the weekly threshold that turns time into overtime.")
    interpretation = models.TextField(blank=True, help_text="Why this firm treats the kind this way, in the words somebody can point at later.")
    revision = models.PositiveIntegerField(default=1)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["kind", "name"]
        constraints = [models.UniqueConstraint(fields=["organization", "kind"], name="one_pay_category_per_kind_in_org")]
    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        """Bump the version when a value that changes a figure moved, whoever is writing.

        This lives on the model rather than in the settings view because the view is not the only
        door: a shell, a data migration, or the bulk-import path could all change a multiplier, and
        a payroll line stamped ``rule v1`` that was actually priced at 1.5× is worse than no stamp
        at all — it is a false statement about the past. One extra read per save of a row that is
        edited a few times a year is a cheap price for the stamp being true.
        """
        if self.pk and (self.revision or 1) >= 1:
            previous = type(self).objects.filter(pk=self.pk).values("paid", "multiplier",
                                                                   "counts_toward_overtime", "active").first()
            if previous and any(str(previous[name]) != str(getattr(self, name)) for name in previous):
                self.revision = (self.revision or 1) + 1
        return super().save(*args, **kwargs)

    @property
    def deducts_time(self):
        """An unpaid category takes its hours out of the tour; a paid one never does."""
        return not self.paid


class ShiftHourDesignation(models.Model):
    """Hours on one post the dispatcher says are a particular kind, with the reason. PAY-2.

    These categories have no automatic source: a 20-minute paid rest, an officer's drive to a
    second property, and a tour held past its end for want of relief are all facts a person knows
    and the punches do not show. So the designation is made by the role that knows, on the post
    it applies to, and it cannot be saved without saying why — an hours bucket with no reason is
    exactly the unattributable adjustment a dispute is decided against.

    It is deliberately *not* a punch edit. The clock keeps the raw in/out pair as evidence; this
    sits beside it and changes how those minutes are paid and classified, which is the difference
    between correcting a record and rewriting one.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="hour_designations")
    shift = models.ForeignKey("core.Shift", on_delete=models.CASCADE, related_name="hour_designations")
    category = models.ForeignKey(PayCategory, on_delete=models.PROTECT, related_name="designations")
    hours = models.DecimalField(max_digits=6, decimal_places=2)
    reason = models.CharField(max_length=255)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="hour_designations")
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["shift__starts_at", "category__kind"]
        constraints = [models.UniqueConstraint(fields=["shift", "category"], name="one_designation_per_shift_category")]
    def clean(self):
        if self.shift_id and self.shift.organization_id != self.organization_id:
            raise ValidationError("Shift must belong to the same organization.")
        if self.category_id and self.category.organization_id != self.organization_id:
            raise ValidationError("Pay category must belong to the same organization.")
        if self.hours is not None and self.hours <= 0:
            raise ValidationError({"hours": "Designate a positive number of hours, or remove the line."})

    def __str__(self):
        return f"{self.hours}h {self.category.kind} on {self.shift}"


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
    # PAY-5. The threshold was already configuration and the 1.5× multiple was not, which is
    # backwards: a firm whose contracts pay double time after the eighth hour, or a municipal
    # pattern paying 1.5× over 12 in a day, had to have it coded. Private security contractors do
    # not get §7(k), so 1.5× is the right *default* for a weekly-threshold employer — and a
    # default is a starting value, not a fact to bury in the arithmetic. Watched for revision,
    # because a pay stub stamped version 4 priced its premium under whatever multiple was in force.
    overtime_premium = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal("1.50"), help_text="Rate applied to overtime hours, as a multiple of the pay rate. A contract that pays double time over its own threshold sets 2.00.")
    rounding_mode = models.CharField(max_length=12, choices=RoundingMode.choices, default=RoundingMode.EXACT)
    rounding_minutes = models.PositiveSmallIntegerField(default=1)
    require_geofence = models.BooleanField(default=True)
    # CLK-2. Whether a shared clock station is allowed at all. It defaults to allowed because the
    # switch is a *restriction* an authorised level imposes: the contract that requires each officer
    # to clock from a device bound to them sets this to false at that level, and DD's "policies may
    # strengthen or weaken the global baseline" is exactly this field. Opening a kiosk is still a
    # deliberate act by a manager — this only decides whether that act is permitted at a post.
    allow_kiosk = models.BooleanField(default=True, help_text="Allow a shared clock station at this company. Turn off to require each officer to clock from their own device.")
    # CLK-4. DD lists "mock-location/spoofing-risk signals" as one of the supported evidence options,
    # and the switch to use it belongs beside the geofence it judges. Default on: the signal *names*
    # a suspicious punch for a human, it never blocks the clock, so a company that turns nothing on is
    # still not silently accepting forged locations — an implausible reading simply goes unexamined.
    # A firm that has decided it will not review these can turn the flag off and say so in policy.
    flag_spoof_risk = models.BooleanField(default=True, help_text="Send punches with an implausible location reading to time review. Never blocks the clock.")
    # CLK-1, per the owner's ruling of 2026-10-04: a frame at clock-in and clock-out, not at breaks,
    # and only where no other identity method already covers the punch. One boolean rather than a
    # per-event-kind matrix because the ruled scope has exactly two states; breaks stay out because a
    # meal break is an annotation on a tour somebody is already paid for, not an arrival anyone needs
    # to witness. Default **off**, unlike the CLK-4 switch above, because a selfie is a biometric:
    # requiring one is a decision about a person's face, not a fraud check a firm should be defaulted
    # into. The exemption is resolved in `services.selfie_owed`, not here, so the rule exists once.
    require_selfie = models.BooleanField(default=False, help_text="Ask for a photo at clock-in and clock-out when no station PIN or checkpoint scan covers the punch. A punch without one is still recorded; it goes to time review.")
    # Reopening is off by default and is a configuration choice, not a per-user one: a company
    # that has told its clients a period is final should have to decide that in policy, in the
    # presence of a reason, rather than in a moment of convenience.
    allow_reopen = models.BooleanField(default=False, help_text="Allow an owner or administrator to unlock an approved period so corrections can flow again.")
    # Bumped only when a value really changes, so a timecard stamped "…:3" can be traced to the
    # third edition of the rule that produced it rather than to whoever saved the row last.
    revision = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)
    def clean(self):
        if self.rounding_minutes not in (1, 5, 6, 10, 15, 30):
            raise ValidationError({"rounding_minutes": "Choose 1, 5, 6, 10, 15, or 30 minutes."})

class TimePolicyOverride(models.Model):
    """A contract's or a property's clock-evidence and rounding rule, beating the company row.

    ``docs/discovery-decisions.md``: "Clock evidence is configurable at global, client, and site
    levels. Authorized client and site policies may strengthen or weaken the global baseline. The
    resolved effective policy, source scope, version, and authorizing actor must be visible and
    auditable." Only two things are overridable — the geofence requirement and the rounding rule.
    The workweek start and the overtime threshold stay company facts: a pay period cannot begin
    on two different days inside one company, and the FLSA workweek is not a client preference.

    A null field means *inherit*, so an override states only what it changes. Copying the whole
    company policy into every override would look friendlier and silently drift the moment the
    baseline moved — which is exactly the failure the resolved-version stamp exists to reveal.
    """
    class Scope(models.TextChoices):
        CONTRACT="contract","Contract client"
        SITE="site","Single site"
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="time_policy_overrides")
    client = models.ForeignKey("core.Client", on_delete=models.PROTECT, null=True, blank=True, related_name="time_policy_overrides")
    site = models.ForeignKey("core.Site", on_delete=models.PROTECT, null=True, blank=True, related_name="time_policy_overrides")
    require_geofence = models.BooleanField(null=True, blank=True, help_text="Leave unset to inherit the company rule.")
    # CLK-2, and the second field on this row that has to be a *nullable* boolean. The defect the
    # roadmap warns about is exactly here: a plain BooleanField in a form turns "inherit" into False,
    # so a site that never decided anything would silently forbid its own kiosk. The tri-state test
    # pins that an explicit False survives a round trip and still renders as "No", not "Inherit".
    allow_kiosk = models.BooleanField(null=True, blank=True, help_text="Leave unset to inherit the company rule.")
    # CLK-4, and the third nullable boolean on this row. A property whose dispatcher has agreed to
    # check spoof signals by hand should not have the signal switched off company-wide, and a
    # contract that forbids shared stations should not inherit an unrelated location rule — which is
    # why this is one field per decision rather than one "evidence level" enum.
    flag_spoof_risk = models.BooleanField(null=True, blank=True, help_text="Leave unset to inherit the company rule.")
    # CLK-1, and the fourth nullable boolean on this row. A contract whose client supplies a
    # PIN-verified station should not have to accept a company-wide selfie rule, and a property that
    # demands a face at the gate should not have to wait for the baseline to change. Same shape, same
    # reason: "not decided here" and "decided, no" are different answers.
    require_selfie = models.BooleanField(null=True, blank=True, help_text="Leave unset to inherit the company rule.")
    rounding_mode = models.CharField(max_length=12, choices=TimePolicy.RoundingMode.choices, null=True, blank=True)
    rounding_minutes = models.PositiveSmallIntegerField(null=True, blank=True, choices=[(value, f"{value} minutes") for value in (1, 5, 6, 10, 15, 30)])
    # Bumped whenever a value on the row actually changes, so a timecard stamped "3f2a…:4"
    # names the text that produced it even after the override has been edited twice more.
    revision = models.PositiveIntegerField(default=1)
    authorized_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="authorized_time_policies")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["client__name", "site__name", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "client"], condition=models.Q(client__isnull=False), name="one_time_policy_override_per_client"),
            models.UniqueConstraint(fields=["organization", "site"], condition=models.Q(site__isnull=False), name="one_time_policy_override_per_site"),
        ]

    @property
    def scope(self):
        return "site" if self.site_id else "contract"

    @property
    def version(self):
        return f"{self.pk}:{self.revision}"

    @property
    def label(self):
        return str(self.site) if self.site_id else (f"{self.client.name} (every post)" if self.client_id else "")

    def overridden_fields(self):
        return [name for name in ("require_geofence", "allow_kiosk", "flag_spoof_risk", "require_selfie", "rounding_mode", "rounding_minutes") if getattr(self, name) not in (None, "")]

    def clean(self):
        if bool(self.client_id) == bool(self.site_id):
            raise ValidationError("Choose the level this rule applies at: one contract client or one site.")
        for obj, text in ((self.client, "Client"), (self.site, "Site")):
            if obj and obj.organization_id != self.organization_id:
                raise ValidationError(f"{text} must belong to the same organization.")
        if self.rounding_minutes not in (None, "", 1, 5, 6, 10, 15, 30):
            raise ValidationError({"rounding_minutes": "Choose 1, 5, 6, 10, 15, or 30 minutes."})
        if (self.rounding_mode in (None, "")) != (self.rounding_minutes in (None, "")):
            raise ValidationError("Rounding mode and interval are set together, or not at all.")

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
    # CLK-1. A *pointer*, not a store: the frame itself is an ordinary `PersonDocument` under the
    # biometric rung, so it inherits the upload validation, the malware scan, the SHA-256, the
    # retention clock and the disclosure ladder that already govern every personal record. This is the
    # one column the roadmap allowed, because "was there a photo, and may I open it" has to be
    # answerable while deciding the row, and a reviewer is not going to query the audit chain at 3 a.m.
    # `SET_NULL` because the retention pass does delete documents: the punch keeps its exception text
    # and the `punch.recorded` event keeps naming the document by id and hash, so a deleted frame
    # leaves the fact that one existed — and its absence is then visible, not silently rewritten.
    selfie = models.ForeignKey("PersonDocument",on_delete=models.SET_NULL,null=True,blank=True,related_name="selfie_punches",
        help_text="The clock photo captured for this punch. Blank means none was attached.")
    # CLK-4. The *verdict* lives here because it has to be visible on the row a reviewer decides on;
    # the measurements behind it live in the `punch.recorded` audit event, beside the rule that judged
    # them. That split is the roadmap's own instruction — no artefact column on the raw punch — with
    # one deliberate exception: a code list is not an artefact store, and a reviewer who has to open
    # the chain to learn why a row is in front of them will not do it at 3 a.m.
    # Values are short codes (`implausible_accuracy`, `stale_fix`, `impossible_travel`), never
    # sentences, so the set can be counted. An empty list means "no signal", which is a different
    # fact from "the reading was never taken" — a punch with no coordinates at all is already
    # `location missing`, not a clean location.
    risk_flags = models.JSONField(default=list, blank=True, help_text="Location-plausibility signals this punch tripped. Empty means none, not unchecked.")
    class Meta:
        ordering = ["occurred_at"]
        indexes = [models.Index(fields=["organization", "person", "occurred_at"])]

    # Display wording for the codes above, kept on the row that carries them so the review screen
    # renders a list rather than inventing one. A plain class attribute, not a field.
    RISK_LABELS = {
        "implausible_accuracy": "location reported as good to 2 m or less",
        "stale_fix": "location was captured minutes before the clock event",
        "impossible_travel": "distance from the previous punch needs an impossible speed",
    }

    @property
    def risk_labels(self):
        """Each stored signal code as the sentence a reviewer reads on the row.

        The codes are the durable thing — `services.location_risk_signals` writes them and a future
        query counts them — so the wording lives beside the row that carries them and the review
        screen renders a list instead of inventing one. An unknown code falls back to itself rather
        than vanishing: a signal nobody can name is a signal nobody will investigate.
        """
        return [self.RISK_LABELS.get(code, code) for code in (self.risk_flags or [])]
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

class ClockKiosk(models.Model):
    """One shared clock station — a tablet or terminal at a post that many officers use.

    ``docs/discovery-decisions.md`` lists "shared kiosk PIN" among the supported evidence options,
    and DD §Sites adds that registered-device binding is *not* required. That combination is the
    design: the station is not a person's device and is not trusted on its own, so nothing here
    identifies a human. The station says *where* the punch was made; the officer's PIN says *who*.

    ``id`` is a random UUID and doubles as the station's unguessable address, which is why there is
    no secret column: the URL names a kiosk, it does not authenticate anyone, and every punch still
    has to carry a verified PIN. Revocation is ``active`` — checked on the database on each request,
    not baked into a token — so a station that walks out with a tablet can be retired in one click
    and stops working mid-keystroke.

    ``device_id`` on ``Punch`` holds this row's primary key. It is a plain UUID column with no
    foreign key (the same choice the offline-device path already made), so a retired kiosk never
    strands the punches it recorded, and the punch table needed no trigger rewrite for a new
    reference. The name is copied into the punch's audit metadata instead, so the review screen can
    say "Guard shack 2" after the row is deleted.
    """
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="clock_kiosks")
    site=models.ForeignKey(Site,on_delete=models.PROTECT,null=True,blank=True,related_name="clock_kiosks",
        help_text="Where the station physically stands. A punch taken here is placed at this post.")
    name=models.CharField(max_length=120)
    active=models.BooleanField(default=True)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="clock_kiosks_created")
    created_at=models.DateTimeField(auto_now_add=True)
    last_seen_at=models.DateTimeField(null=True,blank=True)
    class Meta:
        ordering=["name"]
        constraints=[models.UniqueConstraint(fields=["organization","name"],name="unique_kiosk_name_in_org")]
    def __str__(self):
        return self.name
    def clean(self):
        if self.site_id and self.site.organization_id != self.organization_id:
            raise ValidationError("Kiosk site must belong to the same organization.")

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
    # The last unlocking of this period. The reason stays on the row because "who unlocked
    # payroll, and why" is the first question an auditor asks a locked number; the chain of every
    # reopening is the audit trail's job (payroll.reopened), and the count here makes a habit
    # visible on the page instead of buried in event metadata.
    reopened_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name="reopened_payroll_runs")
    reopened_at=models.DateTimeField(null=True,blank=True)
    reopen_reason=models.TextField(blank=True)
    reopen_count=models.PositiveSmallIntegerField(default=0)
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

def audit_event_payload(*, id, organization, actor, action, target_type, target_id, metadata, previous_hash):
    """The exact bytes an audit event's hash is taken over.

    One definition rather than two, because the writer and the verifier existed separately and had to
    agree by luck. When they drift, every row already written reads as tampering — the worst possible
    failure for a tamper-evident log, and the kind that is discovered by a lawyer rather than a test.
    Both `AuditEvent.save` and `services.verify_audit_chain` now come through here, and so does the
    archive reader that re-verifies a sealed period after its rows have left the table.
    """
    return json.dumps({"id": str(id), "organization": str(organization), "actor": actor, "action": action,
        "target_type": target_type, "target_id": target_id, "metadata": metadata, "previous_hash": previous_hash},
        sort_keys=True, separators=(",", ":"), default=str)

def audit_event_hash(**fields):
    return hashlib.sha256(audit_event_payload(**fields).encode()).hexdigest()

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
    occurred_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)
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
            # The verifier walks (occurred_at, id). A timestamp tie with the head would be ordered by
            # random uuid and could put this row before its own predecessor, so step strictly past it.
            self.occurred_at=timezone.now()
            if previous and self.occurred_at<=previous.occurred_at:
                self.occurred_at=previous.occurred_at+timedelta(microseconds=1)
            self.event_hash=audit_event_hash(id=self.pk,organization=self.organization_id,actor=self.actor_id,
                action=self.action,target_type=self.target_type,target_id=self.target_id,
                metadata=self.metadata,previous_hash=self.previous_hash)
            return super().save(*args, **kwargs)
        return super().save(*args, **kwargs)
    def delete(self, *args, **kwargs):
        raise ValueError("Audit events cannot be deleted")

class RuleRevision(models.Model):
    """One saved version of a rule, kept after the rule itself is gone.

    ``docs/development-roadmap.md`` calls this the residual gap in the whole inheritance model,
    and it is the one place where evidence the product has *already produced* becomes
    unreproducible: a punch and a payroll row both carry a stamp like ``4:2`` — "the fourth
    override row, its second version" — and when that row is deleted the stamp points at nothing.
    The values survive only in the audit chain, which is a log, not a lookup.

    This table is that lookup. It is append-only, keyed by ``(kind, rule_id, revision)``, and the
    constraint is deliberately **unconditional** so MySQL creates it rather than skipping it with
    ``models.W036``. It is written from one place (`services.record_rule_revision`) for the clock
    policy, contract/site rules, and credential requirements alike, because an expiring licence and
    a rounding waiver fail to reconstruct in exactly the same way and the fix should not be
    invented twice.

    ``values`` holds the whole watched set at that revision, not a diff, so resolving a stamp needs
    no other row; ``changed`` holds the diff for the human reading the history.
    """
    class Kind(models.TextChoices):
        CLOCK_POLICY="clock_policy","Company clock policy"
        CLOCK_RULE="clock_rule","Contract or site clock rule"
        CREDENTIAL_RULE="credential_rule","Credential requirement"
        COMPLIANCE_RULE="compliance_rule","Compliance duty"
        # A pay category prices hours, so its history is read for the same reason as the clock
        # policy's: a payroll line says which version priced it, and that has to be answerable after
        # somebody edits the multiple.
        PAY_CATEGORY="pay_category","Pay category"
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="rule_revisions")
    kind = models.CharField(max_length=24, choices=Kind.choices)
    # A string, because the rule's own primary key may be an int or a UUID — and because the row it
    # came from may no longer exist to look one up.
    rule_id = models.CharField(max_length=40)
    revision = models.PositiveIntegerField()
    values = models.JSONField(default=dict, blank=True)
    changed = models.JSONField(default=dict, blank=True)
    label = models.CharField(max_length=200, blank=True, help_text="What the rule was called at the time, so a removed rule still reads as itself.")
    saved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="saved_rule_revisions")
    saved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "rule_id", "revision"]
        constraints = [models.UniqueConstraint(fields=["kind", "rule_id", "revision"], name="one_row_per_rule_version")]
        indexes = [models.Index(fields=["organization", "kind", "rule_id"])]

    def __str__(self):
        return f"{self.get_kind_display()} {self.label or self.rule_id} · v{self.revision}"


class AuditRedaction(models.Model):
    """Two-person request to hide metadata keys from exports; the event row is never changed."""
    class Status(models.TextChoices):
        PENDING="pending","Pending approval"
        APPLIED="applied","Applied to exports"
        REJECTED="rejected","Rejected"
    organization=models.ForeignKey(Organization,on_delete=models.PROTECT,related_name="audit_redactions")
    event=models.ForeignKey(AuditEvent,on_delete=models.PROTECT,related_name="redactions")
    fields=models.JSONField(default=list)
    reason=models.TextField()
    legal_basis=models.CharField(max_length=255)
    requested_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name="requested_audit_redactions")
    approved_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name="approved_audit_redactions")
    status=models.CharField(max_length=20,choices=Status.choices,default=Status.PENDING)
    created_at=models.DateTimeField(auto_now_add=True)
    decided_at=models.DateTimeField(null=True,blank=True)
    class Meta:
        ordering=["-created_at"]
        constraints=[models.UniqueConstraint(fields=["event"],name="one_redaction_per_audit_event")]

def audit_seal_path(instance, filename):
    """One archive per sealed period, filed where only the tenant's own retention job looks."""
    return f"audit-seals/{instance.organization_id}/{instance.period_end:%Y%m}.ndjson"

class AuditSeal(models.Model):
    """A closed period of the audit chain, archived whole and then trimmed from the live table.

    REC-4. ``Organization.audit_retention_days`` was displayed and enforced by nothing, and it cannot
    be enforced the way a document expiry is: the audit log is a hash chain in which every event names
    the hash of the one before it, so a bulk ``DELETE`` of old rows leaves the first surviving event
    pointing at a row that no longer exists. Every later reader — the daily verification, an insurer, a
    court — would see a break and be right to. The chain cannot be *shortened*; it can only be
    *continued from a head that is on record*.

    So the period is sealed first: every row of it is written to an archive whose own SHA-256 is stored
    here, together with the first row's predecessor hash (``first_previous``) and the last row's hash
    (``last_hash``). Only then may the live rows go, and the verification walk continues through the seal
    instead of stopping at the gap. Two seals are themselves chained — a new period must start exactly
    where the last one ended — so the whole history is one continuous line even when only a week of
    events is left in the table.

    This is retention enforcement, not destruction, and it is honest about the difference: the bytes of
    a sealed period still exist, in storage, addressable by the tenant. What stops growing is the live
    queryable log. Destroying the rows outright would break the chain for the periods after them, which
    is the trade the owner took when the ruling was "seal the period, then purge" rather than "delete".
    """
    class Status(models.TextChoices):
        SEALED="sealed","Archived; rows still live"
        PURGED="purged","Archived; rows removed from the live table"
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.PROTECT,related_name="audit_seals")
    period_start=models.DateTimeField()
    period_end=models.DateTimeField()
    # The hash the period's first row claimed as its predecessor. "" for a company's very first event,
    # and otherwise the previous seal's ``last_hash`` — the seam that makes two seals one chain.
    first_previous=models.CharField(max_length=64,blank=True)
    first_hash=models.CharField(max_length=64)
    last_hash=models.CharField(max_length=64)
    event_count=models.PositiveIntegerField()
    # Redactions protect their event row with on_delete=PROTECT, so a period holding one cannot be
    # purged at all. Counting them at seal time is what lets the retention page say *why* a period is
    # stuck, rather than an IntegrityError at midnight in front of nobody.
    redaction_count=models.PositiveIntegerField(default=0)
    manifest=models.JSONField(default=list,help_text="Per-action counts for the period, so a seal reads as something without opening the archive.")
    archive=models.FileField(upload_to=audit_seal_path,blank=True)
    archive_sha256=models.CharField(max_length=64)
    archive_bytes=models.BigIntegerField(default=0)
    # The retention setting that produced this cutoff, recorded because it is the one number a later
    # reader needs to judge whether the period was due at all — and it may have been changed since.
    retention_days=models.PositiveIntegerField()
    status=models.CharField(max_length=10,choices=Status.choices,default=Status.SEALED)
    sealed_at=models.DateTimeField(auto_now_add=True)
    sealed_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="audit_seals_created")
    purged_at=models.DateTimeField(null=True,blank=True)
    purged_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name="audit_seals_purged")
    class Meta:
        ordering=["period_start"]
        constraints=[models.UniqueConstraint(fields=["organization","period_end"],name="one_seal_per_period")]
    def __str__(self):
        return f"{timezone.localtime(self.period_start):%Y-%m-%d} → {timezone.localtime(self.period_end):%Y-%m-%d}"
    def clean(self):
        if self.period_end <= self.period_start:
            raise ValidationError("A sealed period has to end after it starts.")
        if not self.first_hash or not self.last_hash:
            raise ValidationError("A seal records the hashes it continues from and to.")

class ReportSnapshot(models.Model):
    """One saved figure, at the moment it was measured.

    RPT-1/RPT-2. A compliance rate is a fact about a moment, not about a table: file the lapsed
    registration tomorrow and "were we compliant on 12 June" becomes unanswerable, because the live
    panel recomputes it from evidence that no longer looks the way it did. So the figure is stored
    with what it counted and what it left out — denominator, the kinds included, the posts excluded
    as drafts, and the items that needed action — which is enough to defend the number later without
    keeping every satisfied obligation.

    `subject_key` exists instead of a nullable branch/client pair because MySQL treats NULLs as
    distinct inside a unique index: an `(organization, date, NULL, NULL)` constraint would let a
    second company-level row for the same day through, and a duplicate day is exactly what breaks a
    trend. Same reasoning as `RuleRevision`'s deliberately unconditional constraint, and the reason
    the capture is `get_or_create` rather than `create`.
    """
    class Metric(models.TextChoices):
        COMPLIANCE="compliance","Compliance rate"
        COVERAGE="coverage","Published posts staffed and eligible"
        TOUR="tour","Tours with complete time evidence"
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    organization=models.ForeignKey(Organization,on_delete=models.CASCADE,related_name="report_snapshots")
    period_date=models.DateField()
    metric=models.CharField(max_length=20,choices=Metric.choices)
    subject_key=models.CharField(max_length=72,help_text="'company', 'branch:<id>', or 'client:<id>'.")
    branch=models.ForeignKey(Branch,on_delete=models.SET_NULL,null=True,blank=True,related_name="report_snapshots")
    client=models.ForeignKey(Client,on_delete=models.SET_NULL,null=True,blank=True,related_name="report_snapshots")
    total=models.PositiveIntegerField(default=0)
    satisfied=models.PositiveIntegerField(default=0)
    attention=models.PositiveIntegerField(default=0)
    # Null means nothing was measured, which is a different statement from 0% — an empty window is
    # reported as none, never as a perfect score.
    rate=models.DecimalField(max_digits=4,decimal_places=1,null=True,blank=True)
    basis=models.JSONField(default=dict,help_text="Window, kinds counted, exclusions, and the reader basis the figure was taken on.")
    exceptions=models.JSONField(default=list,help_text="The items that made up 'needs action', so the number can be read back.")
    captured_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=["-period_date","metric","subject_key"]
        constraints=[models.UniqueConstraint(fields=["organization","subject_key","metric","period_date"],
                                             name="one_report_snapshot_per_subject_metric_day")]
        indexes=[models.Index(fields=["organization","metric","subject_key","period_date"])]
    def __str__(self):
        return f"{self.get_metric_display()} · {self.subject_key} · {self.period_date}"

    @property
    def subject_label(self):
        """What the row is about, named the way the operator names it on the screen."""
        if self.branch_id:
            return f"{self.branch.name} (branch)"
        if self.client_id:
            return f"{self.client.name} (contract)"
        return "Whole company"


class HoldOver(models.Model):
    """A tour that ran past its scheduled end, and the reason a person had to give for it. SCH-3.

    The vertical's own word, from its glossaries: a guard whose relief does not arrive is held over,
    and the industry treats that as a *coverage* event, not a timekeeping accident. Until now the
    product could only show the consequence — a late clock-out, indistinguishable from a guard who
    simply left two hours after the site closed — and it threw away the two facts a dispute turns on:
    how long the post was *supposed* to run, and who was meant to relieve.

    So the scheduled end is copied here rather than read back off the post. A dispatcher who extends
    ``Shift.ends_at`` to make the pay match rewrites the schedule, and then nothing records that the
    tour ran long at all; this row keeps the original sentence and the reason together. The clock-out
    remains the evidence of when the officer actually stopped — a hold-over explains the span, it does
    not replace it, and it is deliberately not a punch edit.

    ``relief`` names the person who was supposed to stand the next tour, which is the fact that makes
    this a control instead of a note: the same non-arrival that holds one officer over is the
    no-show that will empty the following post, and "who was it meant to be" is the first question
    anybody with an inspection or a claim asks. It is nullable because the commonest real case is
    that nobody was ever assigned — which is what ``reason=unfilled`` is for.
    """
    class Reason(models.TextChoices):
        RELIEF_NO_SHOW = "relief_no_show", "Relief did not arrive"
        RELIEF_LATE = "relief_late", "Relief arrived late"
        UNFILLED = "unfilled", "No reliever was ever assigned"
        INCIDENT_OPEN = "incident_open", "Incident still open at handover"
        SITE_EMERGENCY = "emergency", "Site emergency or lockdown"
        OTHER = "other", "Other (state it)"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="hold_overs")
    shift = models.ForeignKey(Shift, on_delete=models.CASCADE, related_name="hold_overs")
    scheduled_ends_at = models.DateTimeField(
        help_text="The post's end as it stood when the tour ran past it. The overrun is measured "
                  "against this, not against the post's current end, because that is what a dispatcher edits.")
    held_until = models.DateTimeField(null=True, blank=True,
        help_text="When the officer actually came off the post. Blank means they are still on it.")
    relief = models.ForeignKey(Person, on_delete=models.PROTECT, null=True, blank=True, related_name="relief_failures",
        help_text="Who was supposed to stand the next tour, if anybody was.")
    reason = models.CharField(max_length=24, choices=Reason.choices)
    note = models.TextField(blank=True, help_text="Required for 'Other', and the place for the incident number.")
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="hold_overs_recorded")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["shift__starts_at", "created_at"]

    def __str__(self):
        return f"Held over on {self.shift} — {self.get_reason_display().lower()}"

    def clean(self):
        if self.shift_id and self.shift.organization_id != self.organization_id:
            raise ValidationError("Shift must belong to the same organization.")
        if self.relief_id:
            if self.relief.organization_id != self.organization_id:
                raise ValidationError("Relief must belong to the same organization.")
            if self.shift.officer_id and self.relief_id == self.shift.officer_id:
                raise ValidationError(
                    "The officer standing the post cannot also be the relief who failed to arrive.")
        if self.held_until and self.held_until <= self.scheduled_ends_at:
            raise ValidationError(
                "That is not a hold-over — the officer came off before the post was due to end. "
                "Correct the post's end time instead of recording an overrun.")
        if self.reason == self.Reason.OTHER and len((self.note or "").strip()) < 10:
            raise ValidationError({"note": "'Other' has to say what happened, in at least 10 characters."})

    @property
    def overrun_hours(self):
        """How far past the scheduled end this row reaches. ``None`` while the tour is still running."""
        if not self.held_until:
            return None
        return round((self.held_until - self.scheduled_ends_at).total_seconds() / 3600, 2)


class PayrollLockSegment(models.Model):
    """One branch's or one contract's lock state inside a payroll run. PAY-4.

    A run is one period for the whole company, and that is right for the *period* — the workweek
    boundary and the pay dates are firm-wide facts. It is wrong for the lock, because the real
    sequence in a firm with four branches is that two are paid on Thursday, one is waiting on a
    missing punch, and the fourth has a rate dispute. One status for all of them means the only way to
    correct one employee's hours is to re-open every employee's, and a period that is universally
    editable is not a locked period at all.

    So a segment states an **exception** to the run's own status and nothing else. No segment exists
    ⇒ the run's status governs everything, which is exactly the behaviour every installation has today.
    A `locked` segment inside a draft run is "branch A is agreed, keep working branch B"; an `open`
    segment inside an approved run is the correction that used to require re-locking the world.

    Branch and contract are alternatives, and naming neither is the third option: a segment that
    covers everything the named ones do not, which is what lets an operator say "all of it except
    Ridgeline" without a row per branch.
    """
    class Status(models.TextChoices):
        LOCKED = "locked", "Locked"
        OPEN = "open", "Open for correction"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="payroll_lock_segments")
    run = models.ForeignKey("core.PayrollRun", on_delete=models.CASCADE, related_name="lock_segments")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True,
        related_name="payroll_lock_segments")
    client = models.ForeignKey(Client, on_delete=models.PROTECT, null=True, blank=True,
        related_name="payroll_lock_segments")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.LOCKED)
    reason = models.CharField(max_length=255,
        help_text="Why this slice differs from the run. Read months later by somebody who was not in the room.")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payroll_lock_decisions")
    decided_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["run__period_start", "branch__name", "client__name"]
        constraints = [
            # One exception per slice per run. Two rows for one branch could disagree, and the answer
            # to "is Thursday's gate locked" has to be one answer — the same rule RPT-4 applies to
            # every figure in the product.
            models.UniqueConstraint(fields=["run", "branch"], name="one_lock_segment_per_run_branch"),
            models.UniqueConstraint(fields=["run", "client"], name="one_lock_segment_per_run_client"),
        ]

    def __str__(self):
        return f"{self.run.period_start:%b %d} · {self.subject_label} · {self.get_status_display()}"

    def clean(self):
        chosen = [name for name, value in (("branch", self.branch_id), ("contract", self.client_id)) if value]
        if len(chosen) > 1:
            raise ValidationError("A lock segment covers a branch or a contract, not both — two readings "
                                  "of one row is the ambiguity this table exists to remove.")
        if self.branch_id and self.branch.organization_id != self.organization_id:
            raise ValidationError("Branch must belong to the same organization.")
        if self.client_id and self.client.organization_id != self.organization_id:
            raise ValidationError("Contract client must belong to the same organization.")
        if self.run_id and self.run.organization_id != self.organization_id:
            raise ValidationError("Payroll run must belong to the same organization.")
        if len((self.reason or "").strip()) < 10:
            raise ValidationError({"reason": "Say why this slice differs from the run, in at least 10 "
                                             "characters. A lock that differs from its period needs a "
                                             "reason a stranger can read."})

    @property
    def is_company_wide(self):
        return not self.branch_id and not self.client_id

    @property
    def subject_label(self):
        if self.branch_id:
            return f"{self.branch.name} branch"
        if self.client_id:
            return f"{self.client.name} (contract)"
        return "everywhere else"
