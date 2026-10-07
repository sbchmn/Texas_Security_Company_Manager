import json
import uuid
from datetime import datetime, timedelta
from itertools import groupby
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core import signing
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.db.models.manager import BaseManager
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt
from .auth import membership_required
from .middleware import client_ip
from .forms import AuditRedactionForm, AuthorityScopeForm, AvailabilityRuleForm, BrandForm, BranchForm, CheckpointForm, ClientForm, ClockKioskForm, ClockPinForm, ComplianceRuleForm, CredentialForm, CredentialTypeForm, CsvImportForm, CustomFieldDefinitionForm, DispositionRequestForm, DocumentAcknowledgmentForm, DocumentTypeForm, DocumentUploadForm, DomainForm, ExchangeAcceptForm, InvitationAcceptanceForm, MembershipAccessForm, MembershipInvitationForm, OnboardingItemForm, OrganizationSecurityForm, PayCodeForm, PayrollPeriodForm, PersonAccessForm, PersonForm, PersonLinkForm, PunchAdjustmentForm, ShiftExchangeForm, ShiftForm, ShiftGenerationForm, ShiftSwapForm, ShiftTemplateForm, SiteForm, TextAlertsForm, TimeOffRequestForm, TimePolicyForm, TimePolicyOverrideForm, TrainingRecordForm
from .models import AuditEvent, AuditRedaction, AuthorityScope, AvailabilityRule, BrandVersion, Checkpoint, Client, ClockKiosk, ComplianceRule, Credential, CredentialRegistryCheck, CredentialType, CustomFieldDefinition, DispositionRequest, DOCUMENT_TYPE_READERS, DocumentAcknowledgment, DocumentType, HoldOver, ImportBatch, Membership, MembershipInvitation, MessageConsent, DeliveryEvent, Notification, OnboardingItem, OnboardingTask, OfflineClockDevice, Organization, OrganizationDomain, PayCategory, PayCode, PayrollRun, Person, PersonCustomValue, PersonDocument, PayrollLockSegment, Punch, PunchAdjustment, record_readable, record_visibility_filter, ReportSnapshot, RuleRevision, Shift, ShiftClaim, ShiftExchange, ShiftHourDesignation, ShiftSwap, ShiftTemplate, Site, TimeOffRequest, TimePolicy, TimePolicyOverride, TrainingRecord
from .scope import SCOPE_CAPABLE_ROLES, ActorScope, dispatch_recipients_for_shift, scope_for
from .sms import date_span, moment_label, shift_when
from decimal import Decimal
from .services import COMPLIANCE_KINDS, RULE_WATCHED, SNAPSHOT_WINDOW_DAYS, apply_csv_import, apply_recurring_plan, approve_payroll_run, assignment_impact, audit_retention_state, bump_policy_revision, bump_rule_revision, capture_report_snapshots, clear_kiosk_pin_failures, clock_pin_lockout, close_clock_kiosk, clock_policy_coverage, coerce_custom_value, compliance_attendance, compliance_recipients, compliance_summary, credential_registry_state, coverage_report, coverage_state, create_brand_version, create_payroll_run, decide_onboarding_task, describe_hold_over, effective_clock_policy, effective_pay_code, effective_rates, ensure_pay_categories, ensure_rule_history, execute_disposition, expire_stale_moves, find_person_by_pin, haversine_meters, KIOSK_IDENTITY_SECONDS, kiosk_identity, kiosk_identity_token, kiosk_policy_refusal, kiosk_shifts, lock_subject, onboarding_board, onboarding_progress, open_clock_kiosk, open_lock_segments, open_post_candidates, outstanding_acknowledgments, overrun_prompt, payroll_csv, payroll_lock_state, payroll_recipients, payroll_totals, payroll_snapshot_csv, payroll_snapshot_pdf, payroll_snapshot_xlsx, parse_punch_timestamp, person_snapshot, personnel_file_bundle, personnel_file_zip, post_requirements, preview_filename, PreviewUnavailable, preview_source, preview_csv_import, process_brand_image, provision_onboarding_tasks, purge_sealed_audit, queue_acknowledgment_reminders, queue_notice, queue_onboarding_assignment, record_rule_revision, record_registry_check, recurring_plan, record_hold_over, record_person_history, record_punch, registry_checks_by_credential, reopen_payroll_run, report_breakdown, resolve_rule_version, restore_disposition, revise_pay_category, role_domain_gate, role_recipients, rounding_preview, rule_snapshot, saved_report_history, schedule_week_start, seal_audit_period, set_clock_pin, set_payroll_lock_segment, set_shift_designation, shift_advisories, shift_eligibility, shift_participants, signature_lineage, snapshot_csv, store_person_document, swap_candidates, tour_completion, uncovered_advisory, uncovered_windows, verify_audit_chain, verify_clock_pin, verify_seal_archive, week_offset_for
# NTF-4, kept on its own line so the messaging surface's imports read as one group.
from .services import (active_suppression, callback_is_signed, callback_requires_signature,
                       current_consent, handle_inbound_message, ingest_provider_events, normalize_destination,
                       normalize_provider_callback, record_consent, rotate_webhook_token, sms_consent_wording,
                       twiml_reply)

PRIVILEGED = (Membership.Role.OWNER, Membership.Role.ADMIN)
MANAGERS = PRIVILEGED + (Membership.Role.HR, Membership.Role.SCHEDULER, Membership.Role.SUPERVISOR)
# Private personnel records (documents and their acknowledgments) stay narrower than the
# manager set: a scheduler must see the credential and training state that gates an
# assignment, but not the medical, screening, or discipline records in the file.
RECORD_READERS = PRIVILEGED + (Membership.Role.HR, Membership.Role.AUDITOR)
RECORD_WRITERS = PRIVILEGED + (Membership.Role.HR,)
PAYROLL = PRIVILEGED + (Membership.Role.PAYROLL,)
TIME_REVIEWERS = PAYROLL + (Membership.Role.SUPERVISOR,)
AUDIT_READERS = PRIVILEGED + (Membership.Role.AUDITOR,)
PERSON_TABS = ("profile", "onboarding", "credentials", "training", "documents", "time", "history")
PAGE_SIZE = 50
REPORT_WINDOW_DAYS = 7
REPORT_WINDOWS = (7, 14, 28)
_MISS = object()

def _record_subject(request):
    """This sign-in's own personnel record, or None. Cached: the ladder needs it on six routes."""
    subject = getattr(request, "_record_subject", _MISS)
    if subject is _MISS:
        subject = request.organization.people.filter(user=request.user).first()
        request._record_subject = subject
    return subject


def _record_visibility(request):
    """The viewer's document-secrecy filter, as a Q for a `PersonDocument` queryset.

    The listing paths take this instead of writing their own `if`, and the download route takes the
    row-level twin with the same two arguments, so a register can never show a file it then
    refuses to open.
    """
    subject = _record_subject(request)
    return record_visibility_filter(request.membership.role, subject.pk if subject else None)


def _record_open(request, document):
    """Row-level twin of `_record_visibility`, for the routes that must answer 404.

    Both take the same two arguments from the same request, and `RecordSegregationTest` asserts
    they agree on every rung — the filter is what a list shows, this is what a link serves, and the
    user should never be the one to discover which of the two is wrong.
    """
    subject = _record_subject(request)
    return record_readable(document, request.membership.role, subject.pk if subject else None)


def _record_reader(request):
    """The `(role, own person id)` pair the queue's record sections gate on."""
    subject = _record_subject(request)
    return (request.membership.role, subject.pk if subject else None)


def _page(request, queryset, size=PAGE_SIZE):
    """One page of a long list.

    Every register in this product used to render the whole tenant at once, and three capped
    themselves silently — a supervisor who read a 250-row punch list as "the 250 exceptions"
    was being told a number that was not true. `get_page` tolerates a junk page number instead
    of answering 404 to a stale bookmark.
    """
    return Paginator(queryset, size).get_page(request.GET.get("page"))

def _audit_value(value):
    """JSON-safe form of a field value for audit metadata (mirrors services.person_snapshot)."""
    if isinstance(value, BaseManager):
        return sorted(str(pk) for pk in value.values_list("pk", flat=True))
    return None if value is None else str(value)

def _compliance_matrix(org):
    """One table for every obligation, whichever model stores it.

    CMP-0 generalises the matrix's *subject*; the two subjects still live in two models, because
    `Credential` rows attach to a `CredentialType` and rewiring that FK is not what makes a duty
    enterable. What an operator needs is one comparison, so the rows are adapted to the same columns
    here instead of in two templates that can drift apart — and the honest differences (what has to
    hold the evidence, and whether anything measures it yet) become columns rather than disappearing.
    """
    rows = []
    for item in org.credential_types.all():
        enforcement = " ".join(filter(None, [
            "Schedule" if item.blocks_scheduling else "", "Clock-in" if item.blocks_clock_in else ""]))
        # Three states an operator has to be able to tell apart, because only the first two are
        # decisions somebody took: approved and gating; gating on the legacy escape (a row that predates
        # the approval gate and has not been revisited); and flags set but nothing gated because the row
        # is still a draft. The middle one is the debt list this ruling needs, and it is only useful if
        # it is visible on the screen the approver is already looking at.
        if enforcement and not item.is_approved:
            enforcement = (f"{enforcement} · grandfathered" if item.enforcement_grandfathered
                           else f"{enforcement} · not gating")
        rows.append({
            "name": item.name, "code": item.code, "evidence": "Credential",
            "subject": "Officers by category", "applies": item.applies_to_labels,
            "authority_url": item.authority_url, "authority_reference": item.authority_reference,
            "effective_from": item.effective_from, "effective_until": item.effective_until,
            "enforcement": enforcement or "Recorded only",
            # The sub-label has to agree with the label above it. The first draft of this put
            # "Enforcing without approval" on a row that was not enforcing anything, because it tested
            # `enforcement` before it tested whether the row may enforce — a compliance screen that
            # overstates what it is doing is worse than one that says nothing.
            "measured": ("Enforced at assignment and clock-in" if enforcement and item.is_approved
                         else "Enforcing without approval — approve it or clear the flags"
                             if enforcement and item.may_enforce
                         else "Draft: recorded, not gating" if enforcement
                         else "Reported in the queue"),
            "reminders": item.reminder_levels, "is_approved": item.is_approved, "version": item.revision,
            "warning_days": item.warning_days, "active": item.active,
            "edit_url": reverse("credential_type_edit", args=[item.pk]),
        })
    for rule in org.compliance_rules.select_related("document_type").all():
        reason = rule.unevaluated_reason
        applies = rule.applies_to_labels if rule.applies_to_subject == ComplianceRule.Subject.PEOPLE else []
        rows.append({
            "name": rule.name, "code": rule.code, "evidence": rule.get_evidence_display(),
            "subject": f"Held by: {rule.get_applies_to_subject_display()}"
                       + (f" · {rule.document_type.name}" if rule.document_type_id else ""),
            "applies": applies,
            "authority_url": rule.authority_url, "authority_reference": rule.authority_reference,
            "effective_from": rule.effective_from, "effective_until": rule.effective_until,
            # No eligibility check consults a duty yet, and the matrix must not imply one: the
            # column says what this row actually does rather than what its sibling row does.
            "enforcement": "Queue only",
            "measured": f"Not measured — {reason}" if reason else "Measured against filed records",
            "reminders": rule.reminder_levels, "is_approved": rule.is_approved, "version": rule.revision,
            "warning_days": rule.warning_days, "active": rule.active,
            "edit_url": reverse("compliance_rule_edit", args=[rule.pk]),
            "remove_url": reverse("compliance_rule_remove", args=[rule.pk]),
        })
    rows.sort(key=lambda row: row["name"].lower())
    return rows


# CMP-1's drafted Texas obligations, imported beside the screen that offers them in the same style the
# messaging and CLK-1 surfaces use: what these views touch is exactly what they import.
from .texas_rules import missing_texas_duties, seed_texas_obligations
from .forms import CompanyPostOrdersForm, InheritedPostOrdersForm

@membership_required(*MANAGERS,Membership.Role.AUDITOR)
def settings_compliance(request):
    """Rule catalogs: what the installation requires, as opposed to who holds it.

    These were previously buttons in the header of the per-person registers, which put an
    owner-approved legal control and a guard's credential row on one screen with one gate.
    """
    org=request.organization
    editable=request.membership.role in RECORD_WRITERS or request.membership.role in PRIVILEGED
    # CMP-1's drafts are offered on the screen an empty matrix currently leaves an owner to fill from
    # memory. Computed against the rows that already exist rather than assumed absent, so a company that
    # entered one of these duties by hand is not invited to create a second, competing definition of it.
    in_use=_document_types_in_use(org)
    document_types=list(org.document_types.all())
    for item in document_types:item.in_use=item.pk in in_use
    filled=_filled_custom_field_counts(org)
    field_definitions=list(org.custom_field_definitions.all())
    for item in field_definitions:item.filled=filled.get(item.pk,0)
    usage = request.GET.get("usage", "all")
    if usage not in ("all", "unused", "used"):
        messages.error(request, "Choose a supported catalog usage filter.")
        usage = "all"
    if usage != "all":
        used = usage == "used"
        document_types = [item for item in document_types if item.in_use == used]
        field_definitions = [item for item in field_definitions if bool(item.filled) == used]
    return render(request,"core/settings_compliance.html",{
        "matrix":_compliance_matrix(org),
        "document_types":document_types,
        "field_definitions":field_definitions,
        "usage": usage,
        "can_edit":editable,
        "texas_drafts":missing_texas_duties(request.organization),
        # The matrix itself is owner/administrator work: creating a legal obligation is the same
        # act for a duty and for a credential, and `can_edit` is wider than that because it also
        # covers filing evidence. Showing an HR user a link that answers 403 is how a screen loses
        # people's trust.
        "can_edit_matrix":request.membership.role in PRIVILEGED,
    })

@require_POST
@membership_required(*PRIVILEGED)
@transaction.atomic
def compliance_seed_texas(request):
    """Draft the Texas duties into the matrix — unapproved, unenforced, reviewable row by row.

    A button rather than a data migration on purpose. An installation that has never consulted a lawyer
    should not receive a set of obligations it did not choose, and a migration that wrote them would make
    "who decided this company tracks general liability at $100,000" unanswerable. The actor who clicks is
    audited, and the rows produced are the same rows `seed_texas_obligations` writes headless.
    """
    result = seed_texas_obligations(request.organization, request.user)
    if not result["rules"]:
        messages.info(request, "Every drafted Texas duty is already on this matrix, so nothing was added. "
                               "If one of them reads wrong for this company, edit that row rather than "
                               "adding a second one beside it.")
        return redirect("settings_compliance")
    messages.success(request,
        f"Drafted {len(result['rules'])} Texas obligation(s) from the statute and rule text, each with its "
        "source and its proposed reading on the row. None of them is enforced yet: open a row, change "
        "anything wrong for this company, and approve it. Until then the queue says so out loud.")
    return redirect("settings_compliance")

@membership_required()
def settings(request):
    """Settings hub: every configuration surface the actor may actually open.

    Each destination already enforced its own role gate; this page only hides what would
    answer 403, so a branch manager is not offered a link to the company's MFA policy.
    """
    role=request.membership.role
    privileged=role in PRIVILEGED
    manager=role in MANAGERS
    records=role in RECORD_READERS
    def link(label,description,name,allowed):
        return {"label":label,"description":description,"url":reverse(name),"allowed":allowed}
    sections=[
        {"title":"Company","items":[
            link("Default post orders","Baseline instructions inherited by clients, sites and posts.","company_post_orders",privileged),
            link("Brand experience","Name, colours, logo, and published versions.","branding",privileged),
            link("Custom domains","Verified hostnames for this company.","domains",privileged),
            link("Authentication security","Roles that must hold a second factor.","security_settings",privileged),
            link("Team access","Invite owners and administrators.","team",privileged),
        ]},
        {"title":"Organisation structure","items":[
            link("Clients and sites","Contract clients, posts, and geofences.","locations",manager),
            link("Branches","Operating locations that group personnel and sites.","branches",manager),
            link("Clock stations","Shared kiosks for punching in at a post, and officer PINs.","clock_kiosks",manager),
            link("Time and payroll policy","Workweek, overtime, rounding, and the contract or site rules that differ.","time_policy",privileged),
            link("Pay codes","Job and cost-centre codes customer payroll keys an hour on.","pay_codes",manager),
            link("Hour categories","What a break, holiday, training, travel or double-time hour pays, and whether it counts toward overtime.","settings_pay_categories",role in PAYROLL or privileged),
            link("Rule history","Every version a clock rule or credential requirement has had.","rule_history",privileged),
        ]},
        {"title":"Compliance rules","items":[
            link("Credential requirements","The source-cited control matrix and who it applies to.","settings_compliance",manager or role==Membership.Role.AUDITOR),
            link("Onboarding steps","What a new hire owes, by when, and who is stuck on it.","onboarding_settings",role in RECORD_WRITERS),
            link("Personnel fields","Typed custom fields for the personnel file.","custom_field_create",role in RECORD_WRITERS),
            link("Retention review","Records due for review, legal holds, and dispositions.","retention_review",privileged),
        ]},
        {"title":"Records","items":[
            link("Personnel documents","Every file across the roster.","documents",records),
            link("Training records","Courses, certificates, and hours across the roster.","training",manager),
            link("Personnel directory","People, their credentials, training, and file.","people",manager),
        ]},
        {"title":"Governance","items":[
            link("Audit log","Append-only, hash-chained activity.","audit_log",privileged or role==Membership.Role.AUDITOR),
            link("Bulk imports","Templates, dry-run preview, and apply.","imports",role in RECORD_WRITERS),
        ]},
    ]
    visible_sections = [
        {**section, "items": [entry for entry in section["items"] if entry["allowed"]]}
        for section in sections
    ]
    from .workflow_actions import setup_readiness
    return render(request, "core/settings.html", {
        "sections": [section for section in visible_sections if section["items"]],
        "setup_checks": setup_readiness(request),
    })

def health(request):
    return JsonResponse({"status": "ok", "service": "texas-security-company-manager"})

def ready(request):
    from django.core.cache import cache
    from django.db import connection
    try:
        with connection.cursor() as cursor: cursor.execute("SELECT 1"); cursor.fetchone()
        cache.set("readiness-probe","ok",10)
        if cache.get("readiness-probe")!="ok": raise RuntimeError("cache unavailable")
    except Exception:
        return JsonResponse({"status":"unavailable"},status=503)
    return JsonResponse({"status":"ready"})

@membership_required()
def theme_css(request):
    org=request.organization
    css=f":root{{--brand:{org.primary_color};--accent:{org.accent_color}}}"
    if org.dark_mode_enabled:
        css+=f"@media(prefers-color-scheme:dark){{:root{{--brand:{org.dark_primary_color};--accent:{org.dark_accent_color};--canvas:#0b1220;--surface:#111c2d;--ink:#f5f7fa;--muted:#aab6c7;--line:#2b3a50}}}}"
    return HttpResponse(css,content_type="text/css",headers={"Cache-Control":"private, max-age=300","X-Content-Type-Options":"nosniff"})

@login_required
def brand_logo(request):
    """Stream the active organization's logo.

    /media/ is deliberately never routed (config/urls.py serves it only under DEBUG), so a
    direct ``logo.url`` would 404 in production with local storage and would need a second
    image origin in the content policy with object storage.
    """
    import mimetypes
    from django.core.exceptions import ObjectDoesNotExist
    membership = getattr(request, "membership", None)
    if membership is None:
        membership = request.user.organization_memberships.filter(active=True).select_related("organization").first()
    if membership is None:
        raise Http404
    try:
        logo = membership.organization.logo
    except ObjectDoesNotExist:
        raise Http404
    if not logo:
        raise Http404
    content_type = mimetypes.guess_type(logo.name)[0] or "application/octet-stream"
    response = FileResponse(logo.open("rb"), content_type=content_type)
    response["Cache-Control"] = "private, max-age=300"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Disposition"] = 'inline; filename="logo"'
    return response

@membership_required(*PRIVILEGED)
@transaction.atomic
def team(request):
    form = MembershipInvitationForm(request.POST or None)
    invitation_url = request.session.pop("new_invitation_url", None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        if request.organization.memberships.filter(user__email__iexact=email, active=True).exists():
            form.add_error("email", "This person is already an active member.")
        else:
            replaced = list(MembershipInvitation.objects.filter(organization=request.organization, email__iexact=email, accepted_at__isnull=True))
            for previous in replaced:
                AuditEvent.objects.create(organization=request.organization, actor=request.user, action="membership.invitation_replaced", target_type="membership_invitation", target_id=str(previous.pk), metadata={"email": email})
                previous.delete()
            invitation, token = MembershipInvitation.issue(organization=request.organization, email=email, role=form.cleaned_data["role"], invited_by=request.user, expires_at=timezone.now() + timedelta(hours=72))
            AuditEvent.objects.create(organization=request.organization, actor=request.user, action="membership.invited", target_type="membership_invitation", target_id=str(invitation.pk), metadata={"email": email, "role": invitation.role})
            invitation_url=request.build_absolute_uri(reverse("invitation_accept", args=[token]))
            from .email_wording import render_email
            email_subject, email_body = render_email(
                request.organization, "membership.invitation", f"Join {request.organization.display_name}",
                f"You were invited as {invitation.get_role_display()}. Accept this single-use invitation within 72 hours: {invitation_url}",
                {"role": invitation.get_role_display(), "link": invitation_url})
            Notification.objects.create(organization=request.organization,destination=email,channel=Notification.Channel.EMAIL,event_type="membership.invitation",subject=email_subject,body=email_body,deduplication_key=f"membership-invitation:{invitation.pk}")
            request.session["new_invitation_url"] = invitation_url
            messages.success(request, "Invitation queued for delivery. The single-use link is also shown below once.")
            return redirect("team")
    return render(request, "core/team.html", {"form": form, "invitation_url": invitation_url, "memberships": _membership_authority(request.organization), "invitations": request.organization.membership_invitations.filter(accepted_at__isnull=True)})

def _membership_authority(organization):
    """Every membership with its granted scopes attached, for the team roster.

    Scopes are read in one query and matched in Python because the roster is small and the
    alternative is a correlated subquery per member on a page an owner opens to decide who
    may dispatch.
    """
    memberships = list(organization.memberships.select_related("user").order_by("role", "user__username"))
    labels = {}
    for row in AuthorityScope.objects.filter(organization=organization).select_related("branch", "client", "site__client"):
        labels.setdefault(row.membership_id, []).append(row.label)
    for membership in memberships:
        membership.authority_labels = labels.get(membership.pk, [])
        membership.scopeable = membership.role in SCOPE_CAPABLE_ROLES
    return memberships

@membership_required(*PRIVILEGED)
def authority(request, membership_id):
    """Bounded authority for one team member: the branches, contracts, and posts they act on.

    Without this screen the scope model would be a database table nobody can fill, and the
    honest answer to "who supervises this post" would still be "whoever holds the role".
    """
    target = request.organization.memberships.select_related("user", "organization").filter(pk=membership_id).first()
    if not target:
        raise Http404
    rows = list(target.authority_scopes.select_related("branch", "client", "site__client"))
    form = None
    if target.role in SCOPE_CAPABLE_ROLES:
        form = _scoped_form(
            AuthorityScopeForm, request.organization, request.POST or None,
            instance=AuthorityScope(membership=target, organization=request.organization))
        if request.method == "POST" and form.is_valid():
            item = form.save(commit=False)
            try:
                # ModelForm skips uniqueness while validating, so a second grant for the same
                # branch would otherwise reach the database as an IntegrityError.
                item.full_clean()
            except ValidationError as exc:
                for error in exc.messages:
                    form.add_error(None, error)
            else:
                item.save()
                AuditEvent.objects.create(organization=request.organization, actor=request.user, action="authority.scope_granted", target_type="authority_scope", target_id=str(item.pk), metadata={"membership": str(target.pk), "user": target.user_id, "kind": item.scope_kind, "scope": item.label})
                messages.success(request, f"{target.user} now acts on {item.label}.")
                return redirect("authority", membership_id=target.pk)
    return render(request, "core/authority.html", {
        "target": target, "rows": rows, "form": form,
        "scope": ActorScope(target, rows),
        "others": [item for item in _membership_authority(request.organization) if item.scopeable],
    })

@require_POST
@membership_required(*PRIVILEGED)
def authority_revoke(request, scope_id):
    row = AuthorityScope.objects.filter(pk=scope_id, organization=request.organization).select_related("membership__user", "branch", "client", "site__client").first()
    if not row:
        raise Http404
    target, label, kind = row.membership, row.label, row.scope_kind
    row.delete()
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action="authority.scope_revoked", target_type="authority_scope", target_id=str(row.pk), metadata={"membership": str(target.pk), "user": target.user_id, "kind": kind, "scope": label})
    messages.success(request, f"Removed {label} from {target.user}'s authority.")
    return redirect("authority", membership_id=target.pk)

@membership_required(*PRIVILEGED)
@transaction.atomic
def membership_access(request, membership_id):
    """Promote, demote, deactivate, or reactivate one team member.

    Accepting an invitation never changes an existing member's role, so this is the one place a
    role moves. Only an owner may grant Owner or touch an owner's access, the company always keeps
    an active owner, and nobody can deactivate their own sign-in from here.
    """
    target = request.organization.memberships.select_for_update().select_related("user").filter(pk=membership_id).first()
    if not target:
        raise Http404
    if target.role == Membership.Role.OWNER and request.membership.role != Membership.Role.OWNER:
        messages.error(request, "Only an owner can change another owner's access.")
        return redirect("team")
    form = MembershipAccessForm(request.POST or None, actor_role=request.membership.role,
                                initial={"role": target.role, "active": target.active})
    if request.method == "POST" and form.is_valid():
        role, active = form.cleaned_data["role"], form.cleaned_data["active"]
        before = {"role": target.role, "active": target.active}
        if role == target.role and active == target.active:
            messages.info(request, "Nothing changed.")
            return redirect("team")
        if target.user_id == request.user.id and not active:
            form.add_error("active", "You cannot deactivate your own access. Ask another owner or administrator.")
        elif (target.role == Membership.Role.OWNER and target.active
              and (role != Membership.Role.OWNER or not active)
              and not request.organization.memberships.select_for_update().filter(
                  role=Membership.Role.OWNER, active=True).exclude(pk=target.pk).exists()):
            form.add_error("role", "This is the only active owner. Make someone else an owner first.")
        else:
            granting = active and (role != target.role or not target.active)
            permitted, refusal = role_domain_gate(request.organization, target.user.email, role) if granting else (True, "")
            if not permitted:
                form.add_error("role", refusal)
            else:
                target.role, target.active = role, active
                target.save(update_fields=["role", "active"])
                removed = []
                if role not in SCOPE_CAPABLE_ROLES:
                    # Bounded authority only means something for a scheduler or supervisor; left behind,
                    # an old grant would silently return if the person were later promoted again.
                    removed = [row.label for row in target.authority_scopes.select_related("branch", "client", "site__client")]
                    target.authority_scopes.all().delete()
                AuditEvent.objects.create(organization=request.organization, actor=request.user,
                                          action="membership.access_changed", target_type="membership",
                                          target_id=str(target.pk),
                                          metadata={"user": target.user_id, "before": before,
                                                    "after": {"role": role, "active": active},
                                                    "scopes_removed": removed})
                name = target.user.get_full_name() or target.user.email or target.user.username
                messages.success(request, f"{name}: {target.get_role_display() if active else 'deactivated'}.")
                if target.user_id == request.user.id and role not in PRIVILEGED:
                    return redirect("dashboard")
                return redirect("team")
    name = target.user.get_full_name() or target.user.email or target.user.username
    return render(request, "core/form.html", {
        "form": form, "title": f"Change access · {name}", "eyebrow": "Team access",
        "note": ("Changes apply on their next page load, including any sign-in verification the new "
                 "role requires. A deactivated member keeps their history but can no longer use this "
                 "company. Scheduler and supervisor branch/post limits are cleared when the new role "
                 "does not use them."),
        "cancel_url": reverse("team")})

@transaction.atomic
def invitation_accept(request, token):
    invitation = MembershipInvitation.objects.select_for_update().select_related("organization", "person").filter(token_hash=MembershipInvitation.digest_token(token)).first()
    if not invitation or invitation.accepted_at or invitation.expires_at <= timezone.now():
        return render(request, "core/invitation_accept.html", {"invalid": True}, status=410)
    User = get_user_model()
    existing = User.objects.filter(email__iexact=invitation.email).first() or User.objects.filter(username__iexact=invitation.email).first()
    if existing and (not request.user.is_authenticated or request.user.pk != existing.pk):
        messages.info(request, "Sign in to the invited account before accepting this invitation.")
        return redirect(f'{reverse("account_login")}?next={request.path}')
    form = None if existing else InvitationAcceptanceForm(request.POST or None, organization=invitation.organization)
    if request.method=="POST" and (existing or form.is_valid()):
        user = existing
        if user is None:
            user = User(username=invitation.email, email=invitation.email, first_name=form.cleaned_data["first_name"], last_name=form.cleaned_data["last_name"])
            user.set_password(form.cleaned_data["password"])
            user.save()
        # An invitation grants access; it never changes the role of someone who already holds an
        # active one. A personnel-profile invitation offers no Owner/Administrator choice, so
        # letting it overwrite the role would demote an owner who accepts one — possibly the last.
        # Role changes belong to Team access.
        current = Membership.objects.filter(organization=invitation.organization, user=user, active=True).first()
        granted_role = current.role if current else invitation.role
        # AUTH-3: the company's approved-identity rule is checked here, at the only place a role is
        # ever granted, and before any side effect — an invitation this firm's rule cannot honour must
        # not verify the address, create the membership, or consume the token. Nothing is written, so a
        # person who signed in on the wrong account can sign in on the right one and try again.
        permitted, refusal = (True, "") if current else role_domain_gate(
            invitation.organization, user.email or invitation.email, invitation.role)
        if not permitted:
            AuditEvent.objects.create(organization=invitation.organization, actor=user,
                                      action="membership.invitation_refused",
                                      target_type="membership_invitation", target_id=str(invitation.pk),
                                      metadata={"role": invitation.role, "email": user.email,
                                                "reason": "approved-identity-domain"})
            messages.error(request, refusal)
            return render(request, "core/invitation_accept.html",
                          {"invitation": invitation, "form": None, "refused": True}, status=403)
        # Accepting proved possession of the mailbox the single-use link was delivered to, so
        # record the address as verified. Otherwise allauth refuses to enroll an MFA
        # authenticator for the new member, and a role with required MFA could never sign in.
        from allauth.account.models import EmailAddress
        EmailAddress.objects.update_or_create(user=user, email=invitation.email, defaults={"verified": True, "primary": not EmailAddress.objects.filter(user=user, verified=True).exists()})
        membership, _ = Membership.objects.update_or_create(organization=invitation.organization, user=user, defaults={"role": granted_role, "active": True})
        invitation.accepted_at = timezone.now(); invitation.save(update_fields=["accepted_at"])
        metadata = {"role": membership.role}
        if current and current.role != invitation.role:
            metadata.update({"invited_role": invitation.role, "role_kept": True})
        AuditEvent.objects.create(organization=invitation.organization, actor=user, action="membership.invitation_accepted", target_type="membership", target_id=str(membership.pk), metadata=metadata)
        # A person-bound invitation provisions the personnel link that the officer clock,
        # their document queue, and correction requests all depend on.
        conflict = _link_invited_person(invitation, user)
        _capture_signup_text_consent(request, invitation, form, user)
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        request.session["active_organization_id"] = str(invitation.organization_id)
        if conflict:
            messages.warning(request, conflict)
        else:
            messages.success(request, f"Welcome to {invitation.organization.display_name}.")
        return redirect("dashboard")
    return render(request, "core/invitation_accept.html", {"invitation": invitation, "form": form})

def _link_invited_person(invitation, user):
    """Bind the personnel record an invitation was issued for. Returns a problem, or None.

    The record is re-read under a row lock: the invitation may have been issued days ago, and
    the person may have been linked to another account since. Acting on the copy loaded with
    the invitation would let a stale link move an existing assignment.
    """
    if invitation.person_id is None:
        return None
    organization = invitation.organization
    person = Person.objects.select_for_update().get(pk=invitation.person_id, organization=organization)
    already = organization.people.filter(user=user).exclude(pk=person.pk).first()
    if person.user_id and person.user_id != user.pk:
        AuditEvent.objects.create(organization=organization, actor=user, action="person.signin_conflict", target_type="person", target_id=str(person.pk), metadata={"reason": "record-already-linked", "invitation": str(invitation.pk)})
        return "Your team access was created, but this personnel record is already linked to another sign-in. Ask HR / Compliance to review it."
    if already:
        AuditEvent.objects.create(organization=organization, actor=user, action="person.signin_conflict", target_type="person", target_id=str(person.pk), metadata={"reason": "user-already-has-record", "other_person": str(already.pk)})
        return "Your team access was created, but this sign-in is already linked to a different personnel record. Ask HR / Compliance to review it."
    person.user = user
    person.save(update_fields=["user"])
    AuditEvent.objects.create(organization=organization, actor=user, action="person.signin_linked", target_type="person", target_id=str(person.pk), metadata={"invitation": str(invitation.pk)})
    return None

@membership_required(*RECORD_WRITERS)
@transaction.atomic
def person_access_invite(request, person_id):
    """Provision sign-in for a personnel record.

    The officer clock, "My documents", and time-correction requests all key off
    ``Person.user``. Before this screen the only way to set that link was Django admin,
    which a production deployment closes to every source address — so an invited guard
    force could not clock in at all.
    """
    person = _profile_person(request, person_id)
    if person.user_id:
        messages.error(request, "This person already has a sign-in.")
        return redirect("person_detail", person_id=person.pk)
    if not person.email:
        messages.error(request, "Add an email address to this personnel record before inviting a sign-in.")
        return redirect("person_detail", person_id=person.pk)
    form = PersonAccessForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = person.email
        for previous in list(request.organization.membership_invitations.filter(email__iexact=email, accepted_at__isnull=True)):
            AuditEvent.objects.create(organization=request.organization, actor=request.user, action="membership.invitation_replaced", target_type="membership_invitation", target_id=str(previous.pk), metadata={"email": email})
            previous.delete()
        invitation, token = MembershipInvitation.issue(
            organization=request.organization, email=email, role=form.cleaned_data["role"],
            invited_by=request.user, expires_at=timezone.now() + timedelta(hours=72), person=person)
        invitation_url = request.build_absolute_uri(reverse("invitation_accept", args=[token]))
        from .email_wording import render_email
        email_subject, email_body = render_email(
            request.organization, "membership.invitation.person",
            f"Set up your sign-in for {request.organization.display_name}",
            f"{person.full_name}, you were given access to {request.organization.display_name}. "
            f"Create your sign-in within 72 hours: {invitation_url}",
            {"officer": person, "role": invitation.get_role_display(), "link": invitation_url},
            first_name=person.first_name)
        Notification.objects.create(
            organization=request.organization, destination=email, channel=Notification.Channel.EMAIL,
            event_type="membership.invitation", subject=email_subject, body=email_body,
            deduplication_key=f"membership-invitation:{invitation.pk}")
        AuditEvent.objects.create(organization=request.organization, actor=request.user, action="person.signin_invited", target_type="person", target_id=str(person.pk), metadata={"role": invitation.role, "email": email})
        messages.success(request, f"Sign-in invitation queued for {email}.")
        return render(request, "core/form_result.html", {
            "title": "Invitation sent", "eyebrow": "Personnel access",
            "summary": f"A single-use 72-hour link was queued for {email}. Until mail delivery is configured, send this link to the person directly:",
            "detail": invitation_url, "back_url": reverse("person_detail", args=[person.pk]),
            "back_label": "Back to profile"})
    return render(request, "core/form.html", {
        "form": form, "title": "Invite sign-in", "eyebrow": "Personnel access",
        "note": f"The invitation is sent to {person.email} and links that sign-in to {person.full_name}.",
        "cancel_url": _record_return(person, "profile")})

@membership_required(*RECORD_WRITERS)
@transaction.atomic
def person_access_link(request, person_id):
    """Attach a current team member's sign-in to this personnel record. Their role is untouched."""
    person = _profile_person(request, person_id)
    person = Person.objects.select_for_update().get(pk=person.pk)
    if person.user_id:
        messages.error(request, "This person already has a sign-in.")
        return redirect("person_detail", person_id=person.pk)
    form = PersonLinkForm(request.POST or None, organization=request.organization, actor_role=request.membership.role)
    if request.method == "POST" and form.is_valid():
        member = form.cleaned_data["member"]
        person.user = member.user
        person.save(update_fields=["user"])
        AuditEvent.objects.create(organization=request.organization, actor=request.user, action="person.signin_linked",
                                  target_type="person", target_id=str(person.pk),
                                  metadata={"membership": str(member.pk), "user": member.user_id, "role": member.role})
        messages.success(request, f"{person.full_name} is now linked to {member.user.email or member.user.username}.")
        return redirect("person_detail", person_id=person.pk)
    return render(request, "core/form.html", {
        "form": form, "title": "Link existing team member", "eyebrow": "Personnel access",
        "note": (f"Links {person.full_name}'s record to someone who already signs in, so the time clock and "
                 "their own records work for them. No email is sent and their role does not change."),
        "cancel_url": _record_return(person, "profile")})

def _search(request, queryset, fields):
    """Apply the register's ``?q=`` box in SQL.

    A search box that only filters the rows already in the DOM is worse than none once the list
    is paginated: it reports "no matches" for a person two pages away. These registers are
    searched where the rows are.
    """
    term = request.GET.get("q", "").strip()
    if not term:
        return queryset
    criteria = Q()
    for field in fields:
        criteria |= Q(**{f"{field}__icontains": term})
    return queryset.filter(criteria)

@membership_required(*MANAGERS)
def people(request):
    scope = scope_for(request)
    records = _search(request, scope.filter_people(request.organization.people.select_related("branch")),
                      ("first_name", "last_name", "email", "employee_id", "job_title", "city"))
    status = request.GET.get("status", "")
    if status in Person.Status.values:
        records = records.filter(status=status)
    else:
        status = ""
    records = _page(request, records.order_by("last_name","first_name"))
    return render(request, "core/people.html", {"people": records, "paginator": records.paginator, "is_paginated": records.has_other_pages(),
                                                "q": request.GET.get("q","").strip(),
                                                "status": status, "statuses": Person.Status.choices,
                                                "authority_scope": scope if scope.restricted else None})

@membership_required(*MANAGERS)
@transaction.atomic
def person_create(request):
    form = _scope_querysets(PersonForm(request.POST or None), request.organization, scope_for(request))
    if request.method == "POST" and form.is_valid():
        person = form.save(commit=False); person.organization = request.organization; person.save()
        AuditEvent.objects.create(organization=request.organization, actor=request.user, action="person.created", target_type="person", target_id=str(person.pk), metadata={"name": person.full_name})
        messages.success(request, f"{person.full_name} was added.")
        return redirect("people")
    return render(request, "core/form.html", {"form": form, "title": "Add person", "eyebrow": "Personnel directory"})

def _profile_person(request, person_id):
    """Resolve a person visible to this actor, or 404.

    Every per-person route goes through here so a personnel child record can never be
    created against a person the actor cannot open in their own tenant — nor against one
    outside the branch, contract, or post their authority is bounded to.
    """
    person = request.organization.people.select_related("branch", "user").filter(pk=person_id).first()
    if not person:
        raise Http404
    manager = request.membership.role in MANAGERS
    if not manager and person.user_id != request.user.id:
        raise Http404
    if not scope_for(request).permits_person(person):
        raise Http404
    return person


def _record_return(person, tab):
    """Where a bound create/edit form goes after saving, and on cancel."""
    return reverse("person_detail", args=[person.pk]) + f"?tab={tab}" if person else ""


def _workforce_documents():
    """Company records issued to every worker, which have no person on the row.

    Only the current revision. A worker offered the 2024 handbook and the 2026 one signs the
    wrong document, and a superseded version would sit in the queue as outstanding for ever
    because nobody is being asked to sign it any more.
    """
    return Q(person__isnull=True, document_type__audience=DocumentType.Audience.WORKFORCE, revisions__isnull=True)


@membership_required()
def person_detail(request, person_id):
    person = _profile_person(request, person_id)
    manager = request.membership.role in MANAGERS
    is_self = person.user_id == request.user.id
    can_read_records = is_self or request.membership.role in RECORD_READERS
    tab = request.GET.get("tab", "profile")
    if tab not in PERSON_TABS:
        tab = "profile"
    context = {
        "person": person, "manager": manager, "is_self": is_self,
        "can_private_personnel": request.membership.role in (Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR),
        "can_read_records": can_read_records,
        "can_write_credentials": manager,
        "can_write_records": request.membership.role in RECORD_WRITERS,
        "availability_url": (reverse("person_availability", args=[person.pk]) if manager
                             else reverse("availability") if is_self else ""),
        "time_off_url": reverse("time_off") if manager else reverse("my_time_off") if is_self else "",
        "tab": tab,
        # The checklist is a management surface and a self surface; an officer with no reason to see
        # another company officer's onboarding state does not get a tab that 404s when opened.
        "tabs": [(name, "Schedule & time" if name == "time" else name.capitalize()) for name in PERSON_TABS
                 if (name == "documents" and can_read_records) or (name == "onboarding" and (manager or is_self))
                 or (name == "history" and manager) or (name not in ("documents", "onboarding", "history"))],
    }
    if tab == "documents" and not can_read_records:
        raise Http404
    if tab == "onboarding" and not (manager or is_self):
        raise Http404
    if tab == "history" and not manager:
        raise Http404
    if tab == "profile":
        current_post = person.shifts.filter(status=Shift.Status.PUBLISHED,
            starts_at__lte=timezone.now(), ends_at__gt=timezone.now()).select_related("site__client").order_by("starts_at").first()
        context["current_post"] = current_post
        values = {item.definition_id: item for item in person.custom_values.select_related("definition")}
        custom = []
        for definition in request.organization.custom_field_definitions.filter(active=True):
            if definition.sensitive and not manager:
                continue
            custom.append((definition, values.get(definition.id)))
        context["custom"] = custom
    elif tab == "onboarding":
        board = onboarding_board(request.organization, person, reader=_record_reader(request))
        context["board"] = board
        # Which buttons appear is decided here and enforced again on the route: an officer may close
        # their own steps, only the role that files records may waive one, and only the office may
        # reopen a decision somebody else made.
        context["can_office"] = manager
        context["can_waive"] = request.membership.role in RECORD_WRITERS
    elif tab == "credentials":
        credentials = person.credentials.select_related("credential_type").order_by("expires_on", "credential_type__name")
        context["credentials"] = credentials
        # One query for the newest registry look-up behind each row, so the tab can say when we last
        # checked rather than leaving the officer's licence looking verified when nobody has opened
        # the registry page this quarter (CMP-3).
        checks = registry_checks_by_credential(request.organization, [item.pk for item in credentials])
        for item in credentials:
            # Attached to the row rather than passed as a dict keyed by pk, because the template loop
            # already walks these rows and a dict lookup by a variable key is not something the
            # template language can express.
            item.registry_state = credential_registry_state(item.credential_type, item, checks.get(item.pk))
        context["registry_results"] = CredentialRegistryCheck.Result.choices
    elif tab == "training":
        context["training"] = person.training_records.order_by("-completed_on")
    elif tab == "documents":
        # The secrecy ladder, not just the tab gate: an HR manager opening their own file is the
        # subject of the sealed rows in it, and "about you" has to outrank "you run HR".
        visible=person.documents.filter(_record_visibility(request), deleted_at__isnull=True)
        # Archived records are out of the file but not gone, so the tab says where they went rather
        # than letting a count fall silently.
        context["archived_count"]=visible.filter(archived_at__isnull=False).count()
        context["documents"]=visible.filter(archived_at__isnull=True).select_related("document_type")
    elif tab == "time":
        context["shifts"] = person.shifts.select_related("site__client").order_by("-starts_at")[:30]
        context["punches"] = person.punches.select_related("shift__site").order_by("-occurred_at")[:30]
        # Stated availability and approved absence belong on the same panel as the posts, because
        # that is the comparison a supervisor is making when they wonder why someone was not stood.
        context["availability"] = person.availability_rules.all()
        context["time_off"] = person.time_off_requests.order_by("-starts_at")[:20]
    elif tab == "history":
        context["history"] = person.history.select_related("changed_by").all()[:50]
    return render(request, "core/person_detail.html", context)

@membership_required(*MANAGERS)
@transaction.atomic
def person_edit(request,person_id):
    person=_profile_person(request,person_id)
    before=person_snapshot(person);definitions=request.organization.custom_field_definitions.filter(active=True)
    form=_scope_querysets(PersonForm(request.POST or None,instance=person),request.organization,scope_for(request))
    custom_errors=[]
    if request.method=="POST" and form.is_valid():
        values={}
        for definition in definitions:
            try:values[definition]=coerce_custom_value(definition,request.POST.get(f"custom_{definition.key}"))
            except ValidationError as exc:custom_errors.append(str(exc))
        if not custom_errors:
            person=form.save();record_person_history(person,before,request.user)
            for definition,value in values.items():PersonCustomValue.objects.update_or_create(organization=request.organization,person=person,definition=definition,defaults={"value":value})
            messages.success(request,"Personnel profile updated.");return redirect("person_detail",person_id=person.pk)
    current={item.definition.key:item.value for item in person.custom_values.select_related("definition")}
    return render(request,"core/person_edit.html",{"form":form,"person":person,"definitions":definitions,"current":current,"custom_errors":custom_errors})

@membership_required(*RECORD_WRITERS)
def custom_field_create(request):
    form=CustomFieldDefinitionForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        item=form.save(commit=False);item.organization=request.organization;item.save();AuditEvent.objects.create(organization=request.organization,actor=request.user,action="custom_field.created",target_type="custom_field_definition",target_id=str(item.pk),metadata={"key":item.key});messages.success(request,"Custom field created.");return redirect("settings_compliance")
    return render(request,"core/form.html",{"form":form,"title":"Add personnel field","eyebrow":"HCRM configuration"})

@membership_required(*RECORD_WRITERS)
@transaction.atomic
def custom_field_edit(request,field_id):
    definition=request.organization.custom_field_definitions.filter(pk=field_id).first()
    if not definition:raise Http404
    return _catalog_edit(request,form_class=CustomFieldDefinitionForm,instance=definition,name="custom_field_definition",action="custom_field.updated",title="Edit personnel field",eyebrow="HCRM configuration",cancel_url=reverse("settings_compliance"),success="Field updated. Changing the type re-interprets stored values on the next save.")


def _custom_value_filled(value):
    # Saving a profile writes a row for every active field, so an untouched field leaves null, "" or
    # an unticked False behind. Those rows are not information anyone entered.
    return value not in (None, "", False)


def _filled_custom_field_counts(organization):
    counts = {}
    for definition_id, value in PersonCustomValue.objects.filter(organization=organization).values_list("definition_id", "value"):
        if _custom_value_filled(value):
            counts[definition_id] = counts.get(definition_id, 0) + 1
    return counts


@require_POST
@membership_required(*RECORD_WRITERS)
@transaction.atomic
def custom_field_remove(request, field_id):
    """Delete a personnel field nobody has filled in; retire one that holds answers."""
    definition = request.organization.custom_field_definitions.filter(pk=field_id).first()
    if not definition:
        raise Http404
    filled = sum(1 for value in definition.values.values_list("value", flat=True) if _custom_value_filled(value))
    if filled:
        if definition.active:
            definition.active = False
            definition.save(update_fields=["active"])
            AuditEvent.objects.create(organization=request.organization, actor=request.user, action="custom_field.retired",
                target_type="custom_field_definition", target_id=str(definition.pk), metadata={"key": definition.key, "filled": filled})
            messages.success(request, f"{definition.name} is filled in on {filled} personnel file{'' if filled == 1 else 's'}, so it was retired instead of deleted. Those answers are kept.")
        else:
            messages.error(request, f"{definition.name} still holds answers on {filled} personnel file{'' if filled == 1 else 's'} and cannot be deleted.")
        return redirect("settings_compliance")
    definition_id, key, name = definition.pk, definition.key, definition.name
    definition.delete()
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action="custom_field.deleted",
        target_type="custom_field_definition", target_id=str(definition_id), metadata={"key": key, "name": name})
    messages.success(request, f"Deleted the {name} field; no personnel file had a value in it.")
    return redirect("settings_compliance")

@membership_required(*MANAGERS)
def branches(request):
    from django.db.models import Count
    scope = scope_for(request)
    records=scope.filter_branches(request.organization.branches).annotate(
        site_count=Count("sites",filter=Q(sites__active=True),distinct=True),
        person_count=Count("people",filter=Q(people__status=Person.Status.ACTIVE),distinct=True))
    return render(request,"core/branches.html",{"branches":records,"can_configure":request.membership.role in PRIVILEGED,"authority_scope":scope if scope.restricted else None})

@membership_required(*PRIVILEGED)
@transaction.atomic
def branch_create(request):
    form = BranchForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        branch = form.save(commit=False); branch.organization = request.organization; branch.save()
        AuditEvent.objects.create(organization=request.organization, actor=request.user, action="branch.created", target_type="branch", target_id=str(branch.pk), metadata={"name": branch.name})
        messages.success(request, f"{branch.name} branch was created.")
        return redirect("branches")
    return render(request, "core/form.html", { "form": form, "title": "Add branch", "eyebrow": "Organization"})

@membership_required(*PRIVILEGED)
@transaction.atomic
def branch_edit(request, branch_id):
    branch=request.organization.branches.filter(pk=branch_id).first()
    if not branch:raise Http404
    return _catalog_edit(request,form_class=BranchForm,instance=branch,name="branch",action="branch.updated",title="Edit branch",eyebrow="Organization",cancel_url=reverse("branches"))

@membership_required(*PRIVILEGED)
@transaction.atomic
def branding(request):
    org = request.organization
    before = {"display_name": org.display_name, "primary_color": org.primary_color, "accent_color": org.accent_color}
    form = BrandForm(request.POST or None, request.FILES or None, instance=org)
    if request.method == "POST" and form.is_valid():
        item=form.save(commit=False)
        if request.FILES.get("logo"):
            try: item.logo=process_brand_image(request.FILES["logo"])
            except ValidationError as exc:
                form.add_error("logo",exc)
                return render(request,"core/branding.html",{"form":form,"versions":org.brand_versions.all()[:10]})
        item.save();create_brand_version(org,request.user)
        after = {"display_name": org.display_name, "primary_color": org.primary_color, "accent_color": org.accent_color}
        AuditEvent.objects.create(organization=org, actor=request.user, action="branding.updated", target_type="organization", target_id=str(org.pk), metadata={"before": before, "after": after})
        messages.success(request, "Brand settings published.")
        return redirect("branding")
    return render(request, "core/branding.html", {"form": form,"versions":org.brand_versions.all()[:10]})

@require_POST
@membership_required(*PRIVILEGED)
@transaction.atomic
def brand_rollback(request,version_id):
    version=request.organization.brand_versions.filter(pk=version_id).first()
    if not version: raise Http404
    org=request.organization
    for field in ("display_name","primary_color","accent_color","dark_primary_color","dark_accent_color","dark_mode_enabled","support_email","support_phone","timezone"):
        if field in version.snapshot: setattr(org,field,version.snapshot[field])
    if "logo" in version.snapshot: org.logo=version.snapshot["logo"]
    org.save();new_version=create_brand_version(org,request.user)
    AuditEvent.objects.create(organization=org,actor=request.user,action="branding.rolled_back",target_type="brand_version",target_id=str(new_version.pk),metadata={"source_version":version.version})
    messages.success(request,f"Brand restored from version {version.version}.")
    return redirect("branding")

def _scope_querysets(form, organization, scope=None):
    """Bound every choice field to this tenant — and, when told, to this actor's authority.

    A dispatcher scoped to one branch must not be able to name a site or an officer they do
    not supervise: the create form is exactly where an out-of-scope assignment would otherwise
    be invented, and the view's own filter would not catch it because the row is new.
    """
    querysets = {"branch": organization.branches, "client": organization.clients, "site": organization.sites,
                 "officer": organization.people, "person": organization.people,
                 "credential_type": organization.credential_types, "required_credentials": organization.credential_types,
                 "document_type": organization.document_types,
                 # A split tour names the tour it takes over, so that choice list is a shift list.
                 # Unbounded it would offer every post in the deployment, which is a tenant leak in a
                 # dropdown long before any guard could catch it.
                 "relief_for": organization.shifts,
                 "pay_code": organization.pay_codes, "default_pay_code": organization.pay_codes}
    if scope is not None and scope.restricted:
        querysets["branch"] = scope.filter_branches(organization.branches)
        querysets["client"] = scope.filter_clients(organization.clients)
        querysets["site"] = scope.filter_sites(organization.sites)
        querysets["officer"] = scope.filter_people(organization.people)
        querysets["relief_for"] = scope.filter_shifts(organization.shifts)
        querysets["person"] = scope.filter_people(organization.people)
    for field, model in querysets.items():
        if field in form.fields:
            choices = model.filter(active=True) if hasattr(model.model, "active") else model.all()
            if choices.model is Site:
                choices = choices.select_related("client").order_by("client__name", "client_id", "name")
            if field == "officer":
                choices = choices.filter(
                    ~Q(status=Person.Status.TERMINATED) | Q(pk=getattr(form.instance, "officer_id", None)))
            form.fields[field].queryset = choices
    if "relief_for" in form.fields:
        shifts = form.fields["relief_for"].queryset.select_related("site__client", "officer")
        if form.instance.pk:
            shifts = shifts.exclude(pk=form.instance.pk)
        form.fields["relief_for"].queryset = shifts
    if isinstance(form, InheritedPostOrdersForm):
        form.configure_post_orders(organization)
    return form

def _scoped_form(form_class, organization, *args, scope=None, **kwargs):
    # The tenant is assigned by the view, not by the form. ModelForm._post_clean copies the
    # cleaned data onto the instance and runs the model's clean() *during validation*, so an
    # instance without an organization makes every "…must belong to the same organization"
    # guard compare against None and reject the form's own valid input.
    kwargs.setdefault("instance", form_class._meta.model(organization=organization))
    return _scope_querysets(form_class(*args, **kwargs), organization, scope)

def _catalog_edit(request, *, form_class, instance, name, action, title, eyebrow, cancel_url, approval=False, success=None, revision_kind=None):
    """Shared editor for the configuration catalogs.

    Every one of these was create-only, which made a mistyped site address, a wrong warning
    window, a requirement whose applicability changed, or a branch closure an admin-database
    edit — invisible to the people who actually own the record.
    """
    model_fields = {field.name for field in instance._meta.get_fields()}
    watched = [field for field in form_class._meta.fields if field in model_fields]
    watched += ["approved_at", "approved_by_id"] if approval else []
    before: dict[str, object] = {field: _audit_value(getattr(instance, field)) for field in watched}
    prior_revision = getattr(instance, "revision", None)
    prior_snapshot = rule_snapshot(revision_kind, instance) if revision_kind is not None else None
    if prior_snapshot is not None:
        # Snapshot the watched rule values before validation writes them onto the instance, so the
        # revision number moves only when something an evaluation depends on actually moved.
        before.update(prior_snapshot)
    form=_scope_querysets(form_class(request.POST or None,instance=instance),request.organization)
    if request.method=="POST" and form.is_valid():
        if approval and request.POST.get("approve")=="yes" and not instance.approved_at:
            instance.approved_by=request.user
            instance.approved_at=timezone.now()
            # The legacy escape is only meaningful while a row is unapproved. Clearing it at the moment
            # of approval leaves one reason the row enforces, so a later reader cannot tell whether the
            # firm approved it or simply never revisited it. Cleared on approval *only* — an ordinary
            # edit of a grandfathered row must not quietly stop enforcing the obligation it has been
            # enforcing, which would be a weakening nobody asked for.
            if hasattr(instance, "enforcement_grandfathered"):
                instance.enforcement_grandfathered = False
        if revision_kind is not None:
            bump_rule_revision(instance, revision_kind, before)
        item=form.save()
        if revision_kind is not None:
            record_rule_revision(item, revision_kind, request.user,
                                 previous=(prior_revision, prior_snapshot) if prior_snapshot else None)
        after={field:_audit_value(getattr(item,field)) for field in watched}
        changes={field:{"before":before.get(field),"after":value} for field,value in after.items() if before.get(field)!=value}
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action=action,target_type=name,target_id=str(item.pk),metadata={"changes":changes})
        messages.success(request,success or f"{item} updated.")
        return redirect(cancel_url)
    return render(request,"core/form.html",{"form":form,"title":title,"eyebrow":eyebrow,"cancel_url":cancel_url,"approval_control":approval and not instance.approved_at})

@membership_required(*MANAGERS)
def locations(request):
    scope=scope_for(request)
    # The template walks client.sites, so the posts are bounded by the prefetch instead of a
    # second filter in the page: a contract supervisor must not see that client's other posts,
    # and a branch supervisor must not see posts the branch does not own.
    sites=scope.filter_sites(request.organization.sites.all()).select_related("branch").prefetch_related("checkpoints","required_credentials")
    clients=scope.filter_clients(request.organization.clients).prefetch_related(
        Prefetch("sites", queryset=sites), "required_credentials")
    # Which posts carry their own clock rule, so the page that lists the posts also marks where
    # the company baseline does not apply. Id sets, not per-row lookups.
    rules=TimePolicyOverride.objects.filter(organization=request.organization)
    return render(request,"core/locations.html",{"clients":clients,"can_configure":request.membership.role in PRIVILEGED,
                                                 "rule_sites":set(rules.filter(site__isnull=False).values_list("site_id",flat=True)),
                                                 "rule_clients":set(rules.filter(client__isnull=False).values_list("client_id",flat=True)),
                                                 "authority_scope":scope if scope.restricted else None})

@membership_required(*PRIVILEGED)
@transaction.atomic
def client_create(request):
    form=_scoped_form(ClientForm,request.organization,request.POST or None)
    if request.method=="POST" and form.is_valid():
        item=form.save(commit=False); item.organization=request.organization; item.save()
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="client.created",target_type="client",target_id=str(item.pk),metadata={"name":item.name})
        messages.success(request,"Client created."); return redirect("locations")
    return render(request,"core/form.html",{"form":form,"title":"Add client","eyebrow":"Locations"})


@membership_required(*PRIVILEGED)
@transaction.atomic
def company_post_orders(request):
    return _catalog_edit(
        request, form_class=CompanyPostOrdersForm, instance=request.organization,
        name="organization", action="organization.post_orders_updated",
        title="Company post orders", eyebrow="Company defaults", cancel_url=reverse("settings"),
        success="Company post orders updated. Posts without an override use these defaults immediately.")

@membership_required(*PRIVILEGED)
@transaction.atomic
def site_create(request):
    form=_scoped_form(SiteForm,request.organization,request.POST or None)
    if request.method=="POST" and form.is_valid():
        item=form.save(commit=False); item.organization=request.organization; item.full_clean(); item.save()
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="site.created",target_type="site",target_id=str(item.pk),metadata={"name":item.name})
        messages.success(request,"Site created."); return redirect("locations")
    return render(request,"core/form.html",{"form":form,"title":"Add site","eyebrow":"Locations"})

@membership_required(*PRIVILEGED)
@transaction.atomic
def client_edit(request,client_id):
    client=request.organization.clients.filter(pk=client_id).first()
    if not client:raise Http404
    return _catalog_edit(request,form_class=ClientForm,instance=client,name="client",action="client.updated",title="Edit client",eyebrow="Locations",cancel_url=reverse("locations"))

@membership_required(*PRIVILEGED)
@transaction.atomic
def site_edit(request,site_id):
    site=request.organization.sites.filter(pk=site_id).first()
    if not site:raise Http404
    return _catalog_edit(request,form_class=SiteForm,instance=site,name="site",action="site.updated",title="Edit site",eyebrow="Locations",cancel_url=reverse("locations"),success="Site updated. Geofence changes apply to the next punch, not to recorded evidence.")

@membership_required(*PRIVILEGED)
@transaction.atomic
def checkpoint_create(request,site_id):
    site=request.organization.sites.filter(pk=site_id,active=True).first()
    if not site:raise Http404
    form=CheckpointForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        item=form.save(commit=False);item.organization=request.organization;item.site=site;item.full_clean();item.save()
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="checkpoint.created",target_type="checkpoint",target_id=str(item.pk),metadata={"site":str(site.pk),"name":item.name,"scan_code":str(item.scan_code)})
        messages.success(request,f"Checkpoint “{item.name}” created. Print or display its code for the patrol.")
        return redirect("locations")
    return render(request,"core/form.html",{"form":form,"title":"Add checkpoint","eyebrow":"Locations","note":f"The scan code is issued to {site} and is what makes a checkpoint punch attributable instead of anonymous.","cancel_url":reverse("locations")})

@membership_required(*PRIVILEGED)
@transaction.atomic
def checkpoint_edit(request,checkpoint_id):
    checkpoint=Checkpoint.objects.select_related("site").filter(pk=checkpoint_id,organization=request.organization).first()
    if not checkpoint:raise Http404
    return _catalog_edit(request,form_class=CheckpointForm,instance=checkpoint,name="checkpoint",action="checkpoint.updated",title="Edit checkpoint",eyebrow="Locations",cancel_url=reverse("locations"))


@membership_required(*MANAGERS)
def compliance(request):
    org=request.organization
    can_read_records=request.membership.role in RECORD_READERS
    kinds_available=[item for item in COMPLIANCE_KINDS if item!="documents" or can_read_records]
    kind=request.GET.get("kind",kinds_available[0])
    if kind not in kinds_available:kind=kinds_available[0]
    show=request.GET.get("show","attention")
    if show not in ("attention","all"):show="attention"
    scope=scope_for(request)
    people=scope.filter_people(org.people.exclude(status__in=Person.NON_WORKING_STATUSES)).order_by("last_name","first_name")
    person_scope=request.GET.get("person")
    person_object=None
    if person_scope:
        person_object=people.filter(pk=person_scope).first()
        if not person_object:raise Http404
        people=people.filter(pk=person_object.pk)
    built=compliance_attendance(org,scope,people=people,reader=_record_reader(request))
    # The ladder narrows rows; this gate removes the section whole for a role that does not read
    # personnel files at all. Duties keep the reader tuple either way, because the evidence that
    # proves a duty may be a company record the role *does* open.
    if not can_read_records:
        built.pop("documents",None)
    data=built[kind]
    rows=[row for row in data["rows"] if show=="all" or row["_needs"]]
    from .workflow_actions import compliance_actions
    actionable = compliance_actions(request, {kind: data})
    for row in rows:
        match = next((item for item in actionable if item["person"] == row["person"]
                      and item["subject"] == row["subject"] and item["reference"] == row["reference"]), None)
        if match:
            row["action_url"] = match["url"]
            row["action_label"] = match["action_label"]
            row["impact"] = match["impact"]
    page=_page(request, rows)
    return render(request,"core/compliance.html",{
        "sections":{name:value for name,value in built.items()},
        "rows":page,"kind":kind,"show":show,"person":person_object,
        "paginator":page.paginator,"is_paginated":page.has_other_pages(),
        "counts":{name:value["attention"] for name,value in built.items()},
        "totals":{name:value["total"] for name,value in built.items()},
        "credential_types":org.credential_types.filter(active=True),
        "compliance_rules":org.compliance_rules.filter(active=True).select_related("document_type"),
        "authority_scope": scope if scope.restricted else None,
    })

@membership_required(*MANAGERS)
def reports(request):
    """The three numbers an operator needs before reaching for an export.

    Each is computed by the same function the working screen uses — the compliance rate is
    ``compliance_attendance``, so the report cannot disagree with the queue the supervisor is
    actually clearing — and each is shown with its denominator and its exclusions. "92%
    compliant" across one armed registration and across three hundred obligations are different
    facts, and a percentage that hides which posts it refused to count is exactly the kind of
    number the legal gate in this product exists to stop.

    Coverage counts published posts only; drafts are reported as an excluded count rather than
    silently dropped, because "I have five more planned" and "I have nothing planned" are both
    things the number must not pretend to be.
    """
    org = request.organization
    scope = scope_for(request)
    can_read_records = request.membership.role in RECORD_READERS
    try:
        days = int(request.GET.get("days", REPORT_WINDOW_DAYS))
    except (TypeError, ValueError):
        days = REPORT_WINDOW_DAYS
    days = max(1, min(56, days))
    today = timezone.localdate()
    now = timezone.now()
    window_start = timezone.make_aware(datetime.combine(today, datetime.min.time()))
    attendance = compliance_attendance(org, scope, reader=_record_reader(request))
    # The summary is built from the filtered kind list first, then the page's own drill-down rows
    # are dropped to match: a supervisor who cannot open a private record must not see a rate
    # that quietly counted it, nor the list behind the number.
    summary = compliance_summary(
        attendance, readable_kinds=tuple(k for k in COMPLIANCE_KINDS if k != "documents" or can_read_records))
    if not can_read_records:
        attendance.pop("documents", None)
    coverage = coverage_report(org, scope, window_start, window_start + timedelta(days=days))
    closed_window = tour_completion(org, scope, window_start - timedelta(days=days), now)
    # The breakdown groups the rows the panels above were built from, so the parts add up to the
    # whole by construction; the history reads stored figures, never a recomputation.
    breakdown = report_breakdown(org, attendance, coverage, closed_window)
    history = saved_report_history(org, scope)
    return render(request, "core/reports.html", {
        "summary": summary, "attendance": attendance,
        "coverage": coverage, "closed": closed_window,
        "breakdown": breakdown, "history": history, "snapshot_days": SNAPSHOT_WINDOW_DAYS,
        "can_capture": request.membership.role in PRIVILEGED,
        "days": days, "windows": REPORT_WINDOWS, "window_start": today, "window_end": today + timedelta(days=days - 1),
        "past_start": today - timedelta(days=days), "past_end": today,
        "authority_scope": scope if scope.restricted else None,
    })

@membership_required(*PRIVILEGED)
@require_POST
@transaction.atomic
def report_capture(request):
    """Save today's figures now, rather than waiting for tonight's worker pass.

    The worker captures every company nightly; this is for the moment somebody wants the number
    pinned before it moves — a client review this afternoon, a renewal filing tonight. The capture is
    idempotent, so pressing it twice does not produce two versions of today.
    """
    created = capture_report_snapshots(request.organization)
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action="report.captured",
        target_type="organization", target_id=str(request.organization.pk), metadata={"created": created})
    messages.success(request, (f"Saved {created} figure{'s' if created != 1 else ''} for today. Figures already "
                               f"saved today were left as they were — a stored record is not rewritten."
                               if created else "Today's figures are already saved; nothing was overwritten."))
    return redirect("saved_reports")


@membership_required(*MANAGERS)
def saved_reports(request):
    """Every figure the installation has stored, newest first, within the actor's own subjects."""
    scope = scope_for(request)
    metric = request.GET.get("metric", "")
    if metric not in [item.value for item in ReportSnapshot.Metric]:
        metric = ""
    subject = request.GET.get("subject", "")
    stored = ReportSnapshot.objects.filter(organization=request.organization)
    if scope.restricted:
        keys = {f"branch:{pk}" for pk in scope.branch_ids} | {f"client:{pk}" for pk in scope.client_ids}
        # An empty grant set has no saved subject at all. Filtering by a key that cannot exist is
        # deliberate: dropping the filter here would hand a bounded manager the company's numbers.
        stored = stored.filter(subject_key__in=list(keys) or ["-"])
    rows = stored.select_related("branch", "client")
    if metric:
        rows = rows.filter(metric=metric)
    if subject:
        rows = rows.filter(subject_key=subject)
    rows = _page(request, rows.order_by("-period_date", "metric", "subject_key"))
    branches = {row.pk: row.name for row in request.organization.branches.all()}
    clients = {row.pk: row.name for row in request.organization.clients.all()}

    def label(branch_id, client_id):
        if branch_id:
            return f"{branches.get(branch_id, 'Removed branch')} (branch)"
        if client_id:
            return f"{clients.get(client_id, 'Removed contract')} (contract)"
        return "Whole company"

    # The filter offers only this actor's subjects, and `order_by()` with no arguments is what makes
    # the dedup actually deduplicate: the model's default ordering appends `metric` and
    # `period_date` to the SELECT list, so a DISTINCT over the subject columns returns one option
    # per metric instead of one per subject.
    subjects = []
    for subject_key, branch_id, client_id in stored.values_list("subject_key", "branch_id", "client_id").order_by().distinct():
        subjects.append((subject_key, label(branch_id, client_id)))
    subjects.sort(key=lambda item: item[1])
    return render(request, "core/saved_reports.html", {
        "reports": rows, "paginator": rows.paginator, "is_paginated": rows.has_other_pages(),
        "metrics": ReportSnapshot.Metric.choices, "metric": metric, "subject": subject,
        "subjects": subjects, "snapshot_days": SNAPSHOT_WINDOW_DAYS,
        "authority_scope": scope if scope.restricted else None,
    })


@membership_required(*MANAGERS)
def report_snapshot_download(request, snapshot_id):
    """One stored figure and the items behind it.

    The row is fetched by tenant and by the actor's subjects, so a saved branch figure the reader's
    authority does not reach answers 404 rather than streaming another branch's exceptions.
    """
    row = ReportSnapshot.objects.filter(organization=request.organization, pk=snapshot_id).select_related(
        "branch", "client").first()
    if not row:
        raise Http404
    scope = scope_for(request)
    if scope.restricted:
        keys = {f"branch:{pk}" for pk in scope.branch_ids} | {f"client:{pk}" for pk in scope.client_ids}
        if row.subject_key not in keys:
            raise Http404
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action="report.exported",
        target_type="report_snapshot", target_id=str(row.pk),
        metadata={"metric": row.metric, "subject": row.subject_key, "period": row.period_date.isoformat()})
    response = HttpResponse(snapshot_csv(row), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="report-{row.metric}-{row.period_date}.csv"'
    response["Cache-Control"] = "private, no-store"
    return response


@membership_required(*PRIVILEGED)
@transaction.atomic
def credential_type_create(request):
    form=CredentialTypeForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        item=form.save(commit=False); item.organization=request.organization
        if request.POST.get("approve") == "yes": item.approved_by=request.user;item.approved_at=timezone.now()
        # A requirement created from this screen is new, so it has no legacy enforcement to preserve:
        # it may refuse an assignment or a clock-in only once a named person has approved it. The column
        # defaults True because the rows that predate this rule must keep behaving as their firms rely
        # on; clearing it here is what stops that default leaking forward into rows made after the ruling.
        item.enforcement_grandfathered = False
        item.save()
        record_rule_revision(item, RuleRevision.Kind.CREDENTIAL_RULE, request.user)
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="credential_type.created",target_type="credential_type",target_id=str(item.pk),metadata={"name":item.name,"approved":item.is_approved,"enforcing":item.may_enforce,"authority":item.authority_reference,"version":item.revision})
        messages.success(request,"Credential type created." if item.is_approved else
                         "Requirement recorded. It is a draft: it will not refuse an assignment or a "
                         "clock-in until you approve it, and the matrix says so beside it.")
        return redirect("settings_compliance")
    return render(request,"core/form.html",{"form":form,"title":"Add credential type","eyebrow":"Compliance","approval_control":True})

@membership_required(*PRIVILEGED)
@transaction.atomic
def credential_type_edit(request,type_id):
    credential_type=request.organization.credential_types.filter(pk=type_id).first()
    if not credential_type:raise Http404
    return _catalog_edit(request,form_class=CredentialTypeForm,instance=credential_type,name="credential_type",action="credential_type.updated",title="Edit credential requirement",eyebrow="Compliance",cancel_url=reverse("settings_compliance"),approval=True,revision_kind=RuleRevision.Kind.CREDENTIAL_RULE,success="Requirement updated. Enforcement changes apply to the next eligibility check, not to recorded assignments.")

@membership_required(*PRIVILEGED)
@transaction.atomic
def compliance_rule_create(request):
    form=ComplianceRuleForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        item=form.save(commit=False); item.organization=request.organization
        if request.POST.get("approve") == "yes": item.approved_by=request.user;item.approved_at=timezone.now()
        item.save()
        record_rule_revision(item, RuleRevision.Kind.COMPLIANCE_RULE, request.user)
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="compliance_rule.created",target_type="compliance_rule",target_id=str(item.pk),metadata={"name":item.name,"evidence":item.evidence,"subject":item.applies_to_subject,"approved":item.is_approved,"authority":item.authority_reference,"version":item.revision})
        messages.success(request,"Compliance duty created."); return redirect("settings_compliance")
    return render(request,"core/form.html",{"form":form,"title":"Add compliance duty","eyebrow":"Compliance","approval_control":True})

@membership_required(*PRIVILEGED)
@transaction.atomic
def compliance_rule_edit(request,rule_id):
    rule=request.organization.compliance_rules.filter(pk=rule_id).first()
    if not rule:raise Http404
    return _catalog_edit(request,form_class=ComplianceRuleForm,instance=rule,name="compliance_rule",action="compliance_rule.updated",title="Edit compliance duty",eyebrow="Compliance",cancel_url=reverse("settings_compliance"),approval=True,revision_kind=RuleRevision.Kind.COMPLIANCE_RULE,success="Duty updated. The queue re-reads it on the next view; recorded evidence is never reinterpreted.")

@require_POST
@membership_required(*PRIVILEGED)
@transaction.atomic
def compliance_rule_remove(request, rule_id):
    """Delete a company duty. Its revision history stays, so earlier reports still resolve."""
    rule = request.organization.compliance_rules.filter(pk=rule_id).first()
    if not rule:
        raise Http404
    metadata = {"name": rule.name, "code": rule.code, "version": rule.revision, "approved": rule.is_approved,
                "authority": rule.authority_reference}
    rule_pk = rule.pk
    rule.delete()
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action="compliance_rule.deleted",
        target_type="compliance_rule", target_id=str(rule_pk), metadata=metadata)
    messages.success(request, f"Deleted the duty {metadata['name']}. Its version history is kept in rule history.")
    return redirect("settings_compliance")

@membership_required(*MANAGERS)
@transaction.atomic
def credential_edit(request,credential_id):
    """Record a renewal or a status change on a credential that already exists.

    Without this the only way to file a renewed licence was a second row, which left the
    expired one in the register and made the compliance queue show a guard both current and
    lapsed at the same time.
    """
    credential=request.organization.credentials.select_related("person","credential_type").filter(pk=credential_id).first()
    if not credential:raise Http404
    if not scope_for(request).permits_person(credential.person):raise Http404
    before={"status":credential.status,"number":credential.number,"issued_on":_audit_value(credential.issued_on),"expires_on":_audit_value(credential.expires_on)}
    form=CredentialForm(request.POST or None,instance=credential)
    form.fields.pop("person",None)
    form.fields["credential_type"].queryset=request.organization.credential_types.filter(active=True)
    if request.method=="POST" and form.is_valid():
        number=form.cleaned_data.get("number","").strip()
        if number and Credential.objects.filter(person=credential.person,credential_type=form.cleaned_data["credential_type"],number=number).exclude(pk=credential.pk).exists():
            form.add_error("number","This person already has a record of that type with this number.")
        else:
            item=form.save()
            after={"status":item.status,"number":item.number,"issued_on":_audit_value(item.issued_on),"expires_on":_audit_value(item.expires_on)}
            changes={field:{"before":before[field],"after":value} for field,value in after.items() if before[field]!=value}
            AuditEvent.objects.create(organization=request.organization,actor=request.user,action="credential.updated",target_type="credential",target_id=str(item.pk),metadata={"person":str(item.person_id),"changes":changes})
            messages.success(request,"Credential updated.")
            return redirect(_record_return(item.person,"credentials"))
    return render(request,"core/form.html",{"form":form,"title":"Update credential","eyebrow":"Compliance","person":credential.person,"cancel_url":_record_return(credential.person,"credentials"),"note":f"{credential.person.full_name} — {credential.credential_type.name}. Update the dates when a credential is renewed."})

@membership_required(*MANAGERS)
@transaction.atomic
def credential_create(request,person_id=None):
    form=_scoped_form(CredentialForm,request.organization,request.POST or None,scope=scope_for(request))
    bound=_profile_person(request,person_id) if person_id else None
    if bound: form.bind_person(bound)
    if request.method=="POST" and form.is_valid():
        item=form.save(commit=False); item.organization=request.organization; item.full_clean(); item.save()
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="credential.created",target_type="credential",target_id=str(item.pk),metadata={"person":str(item.person_id),"status":item.status})
        messages.success(request,"Credential recorded.")
        return redirect(_record_return(bound,"credentials") or reverse("compliance"))
    return render(request,"core/form.html",{"form":form,"title":"Record credential","eyebrow":"Compliance","person":bound,"cancel_url":_record_return(bound,"credentials")})

WEEK_WINDOW = 26
OPEN_POST_HORIZON_DAYS = 14

@membership_required(*MANAGERS)
def schedule(request):
    """One week of posts, grouped by day, with the unfilled ones counted.

    The page used to render every shift the tenant ever created with no window: a dispatcher
    could not see the week they are staffing, and an unfilled post two months out looked
    identical to one starting in an hour.
    """
    org = request.organization
    from .attendance import attendance_widget
    scope = scope_for(request)
    today = timezone.localdate()
    try:
        offset = int(request.GET.get("week", "0"))
    except ValueError:
        offset = 0
    offset = max(-WEEK_WINDOW, min(WEEK_WINDOW, offset))
    site_id = request.GET.get("site")
    site = org.sites.filter(pk=site_id).first() if site_id else None
    if site_id and (not site or not scope.permits_site(site)):
        raise Http404
    week_start = schedule_week_start(today, offset, organization=org)
    start_at = timezone.make_aware(datetime.combine(week_start, datetime.min.time()))
    end_at = start_at + timedelta(days=7)
    window = scope.filter_shifts(org.shifts.filter(starts_at__lt=end_at, ends_at__gte=start_at)).exclude(status=Shift.Status.CANCELLED)
    if site:
        window = window.filter(site=site)
    show = request.GET.get("show", "all")
    if show not in ("all", "open", "draft", "risk"):
        messages.error(request, "Choose a supported schedule filter.")
        show = "all"
    if show == "open":
        window = window.filter(status=Shift.Status.PUBLISHED, officer__isnull=True)
    elif show == "draft":
        window = window.filter(status=Shift.Status.DRAFT)
    shifts = list(window.select_related("site__client__default_pay_code", "organization", "site__client__organization", "officer", "template", "pay_code",
        "site__default_pay_code").prefetch_related("required_credentials", "claims__officer").order_by("starts_at"))
    policies = {}
    for shift in shifts:
        # Show what the post demands and pays *after* inheritance, so a dispatcher is not told
        # a rule applies without being able to see that the contract, not the shift, set it.
        shift.requirements = post_requirements(shift)
        shift.rates = effective_rates(shift)
        # The job code the customer's payroll will key this line on, with the level that set it —
        # shown next to the rates because a dispatcher pricing a post is answering the same query.
        shift.pay_code_info = effective_pay_code(shift)
        # The rounding rule that will be applied to this post's hours, named with its scope: a
        # dispatcher who sees "nearest 15 · site" knows the client agreed to it, and a driver
        # waiting at the gate knows why the timecard will not match the clock.
        key = shift.site_id
        if key not in policies:
            policies[key] = effective_clock_policy(org, shift.site)
        shift.clock_policy = policies[key].rounding
    # Approved leave that overlaps this week, in one query: the assignment gate already refuses
    # it, and the page has to show the hole a dispatcher is being asked to fill.
    leaves = {}
    for row in TimeOffRequest.objects.filter(organization=org, status=TimeOffRequest.Status.APPROVED,
            starts_at__lt=end_at, ends_at__gt=start_at).only("person_id", "starts_at", "ends_at"):
        leaves.setdefault(row.person_id, []).append(row)
    for shift in shifts:
        shift.leave_conflict = next((row for row in leaves.get(shift.officer_id, ())
                                     if row.starts_at < shift.ends_at and row.ends_at > shift.starts_at), None)
        if show == "risk":
            shift.assignment_eligible, shift.assignment_reasons = shift_eligibility(shift) if shift.officer_id else (False, ["No officer assigned"])
    if show == "risk":
        shifts = [shift for shift in shifts if shift.status == Shift.Status.PUBLISHED and shift.officer_id
                  and (not shift.assignment_eligible or shift.leave_conflict)]
    days = {}
    for shift in shifts:
        days.setdefault(timezone.localtime(shift.starts_at).date(), []).append(shift)
    labelled = sorted(days.items(), key=lambda item: item[0])
    layout = "list" if request.GET.get("layout") == "list" else "grid"
    grid = (_schedule_grid(org, scope, shifts, week_start, leaves, today=today, everyone=show == "all" and site is None)
            if layout == "grid" else None)
    return render(request, "core/schedule.html", {
        "shifts": shifts, "days": labelled, "week_start": week_start, "week_end": end_at, "week_last": week_start + timedelta(days=6), "site": site,
        "offset": offset, "is_current": offset == 0, "layout": layout, "grid": grid,
        "keep": "".join(f"&{key}={value}" for key, value in (("site", site.pk if site else None), ("layout", "list" if layout == "list" else None)) if value),
        "show": show,
        "open_posts": sum(1 for shift in shifts if not shift.officer_id),
        "published_count": sum(1 for shift in shifts if shift.status == Shift.Status.PUBLISHED),
        "draft_count": sum(1 for shift in shifts if shift.status == Shift.Status.DRAFT),
        "authority_scope": scope if scope.restricted else None,
        "attendance_widget": attendance_widget(org, scope, site),
    })

def _schedule_grid(org, scope, shifts, week_start, leaves, *, today, everyone):
    """The week as a roster: one row per officer, one column per day, open posts on top.

    A dispatcher filling a week reads it across (is this guard over hours, off on Thursday?) and
    down (who is on Tuesday night?). Unfiltered, everyone in scope gets a row even with no posts,
    because an empty row is the officer who can still be given one. A post sits on the day it
    starts, which is the day an overnight tour is booked against.
    """
    week_days = [week_start + timedelta(days=offset) for offset in range(7)]
    column = {day: index for index, day in enumerate(week_days)}
    bounds = [(timezone.make_aware(datetime.combine(day, datetime.min.time())),
               timezone.make_aware(datetime.combine(day + timedelta(days=1), datetime.min.time()))) for day in week_days]
    open_cells = [[] for _ in week_days]
    placed = {}
    for shift in shifts:
        index = column.get(timezone.localtime(shift.starts_at).date())
        if index is None:
            continue
        if shift.officer_id:
            placed.setdefault(shift.officer_id, [[] for _ in week_days])[index].append(shift)
        else:
            open_cells[index].append(shift)
    roster = {person.pk: person for person in (shift.officer for shift in shifts if shift.officer_id)}
    if everyone:
        for person in scope.filter_people(org.people.exclude(status__in=Person.NON_WORKING_STATUSES)):
            roster.setdefault(person.pk, person)
    rows = []
    for person in sorted(roster.values(), key=lambda person: (person.last_name.lower(), person.first_name.lower())):
        cells = placed.get(person.pk, [[] for _ in week_days])
        hours = sum((shift.ends_at - shift.starts_at).total_seconds() for cell in cells for shift in cell) / 3600
        rows.append({"person": person, "hours": round(hours, 2), "cells": [
            {"day": day, "shifts": cell, "leave": any(row.starts_at < end and row.ends_at > start for row in leaves.get(person.pk, ()))}
            for day, cell, (start, end) in zip(week_days, cells, bounds)]})
    counts = [sum(1 for row in rows for _ in row["cells"][index]["shifts"]) + len(open_cells[index]) for index in range(7)]
    return {
        "days": [{"date": day, "count": counts[index], "open": len(open_cells[index]), "today": day == today}
                 for index, day in enumerate(week_days)],
        "open": [{"day": day, "shifts": cell} for day, cell in zip(week_days, open_cells)],
        "rows": rows,
    }

def _schedule_return(shift):
    """The schedule week a post sits in, so saving a post next week does not land on this one."""
    offset = week_offset_for(timezone.localtime(shift.starts_at).date(), organization=shift.organization)
    if offset == 0 or abs(offset) > WEEK_WINDOW:
        return reverse("schedule")
    return f"{reverse('schedule')}?week={offset}"

def _shift_prefill(request, scope):
    """Starting values from the grid's "+" on an empty cell: that day, and that officer's row."""
    initial = {}
    try:
        day = datetime.strptime(request.GET.get("date", ""), "%Y-%m-%d").date()
    except ValueError:
        day = None
    if day:
        initial["starts_at"] = f"{day:%Y-%m-%d}T08:00"
        initial["ends_at"] = f"{day:%Y-%m-%d}T16:00"
    officer = request.organization.people.exclude(status__in=Person.NON_WORKING_STATUSES).filter(pk=_uuid_or_none(request.GET.get("officer"))).first()
    if officer and scope.permits_person(officer):
        initial["officer"] = officer.pk
    site = request.organization.sites.filter(pk=_uuid_or_none(request.GET.get("site")), active=True).first()
    if site and scope.permits_site(site):
        initial["site"] = site.pk
    return initial

def _uuid_or_none(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None

def _persist_shift(request, form, previous_status):
    """Write a shift, then tell whoever the change concerns.

    Publishing is the moment a compliance rule has to bite — after this the post is what
    someone is expected to stand. An *assigned* officer must therefore be eligible, but an
    unfilled published post is legitimate: that is how a coverage gap is announced. Its gate
    is applied to each officer who asks to take it (see ``shift_claim``) and again on the
    dispatcher's approval, never to the publication itself.
    """
    item = form.save(commit=False)
    item.organization = request.organization
    posted = list(form.cleaned_data.get("required_credentials") or [])
    if item.status == Shift.Status.PUBLISHED and item.officer_id:
        allowed, reasons = shift_eligibility(item, requirements=post_requirements(item, posted))
        if not allowed:
            for reason in reasons:
                form.add_error("officer", reason)
            form.add_error("status", "The assigned officer does not qualify for this post as configured.")
            return None
    item.full_clean()
    item.save()
    form.save_m2m()
    if item.status == Shift.Status.PUBLISHED:
        stamp = timezone.now().strftime("%Y%m%d%H%M")
        if previous_status != Shift.Status.PUBLISHED:
            if item.officer_id:
                queue_shift_notice(item, event_type="shift.published", dedup_key=f"shift-published:{item.pk}:{stamp}",
                                   subject="You are scheduled", body=f"{item.site} on {shift_when(item)}.")
            else:
                # An unfilled post is only useful if the qualified people hear about it.
                queue_open_post_notice(item)
        elif item.officer_id:
            queue_shift_notice(item, event_type="shift.changed", dedup_key=f"shift-changed:{item.pk}:{stamp}",
                               subject="Your shift changed", body=f"{item.site} now runs {shift_when(item)}.")
    # Availability and the overtime threshold are warnings, not refusals: the dispatcher who
    # needs the post covered should hear what it costs and decide, not be blocked silently.
    if item.officer_id:
        for advisory in shift_advisories(item):
            messages.warning(request, advisory)
    return item

def queue_shift_notice(shift, *, event_type, subject, body, dedup_key, sms=None):
    recipients = shift_participants(shift)
    if not recipients:
        return 0
    # NTF-1. The officer the post belongs to is named as the subject, because "your post moved to 6pm"
    # is the one notice a text is genuinely worth configuring. `shift_participants` currently returns
    # that officer alone, so at this call site the subject set is the whole recipient set — stating it
    # explicitly is what makes the routing hold when the helper grows the managing roles its own
    # docstring already promises, and the managers' audience will then be resolved by role instead.
    subject_ids = {shift.officer.user_id} if shift.officer_id and shift.officer.user_id else set()
    return queue_notice(organization=shift.organization, recipients=recipients, event_type=event_type,
                        subject=subject, body=body, dedup_key=dedup_key, subject_user_ids=subject_ids,
                        sms={"shift": shift, **(sms or {})})

def queue_open_post_notice(shift):
    from .services import open_post_candidates
    candidates, _ = open_post_candidates(shift)
    if not candidates:
        return 0
    return queue_notice(organization=shift.organization, recipients=candidates, event_type="shift.open",
                        subject="An open post needs coverage",
                        body=f"{shift.site} on {shift_when(shift)} is unfilled. Ask to take it from Open posts.",
                        dedup_key=f"shift-open:{shift.pk}:{shift.starts_at:%Y%m%d%H%M}", sms={"shift": shift})

@membership_required()
def open_posts(request):
    """Published posts nobody is assigned to, filtered by what this officer may lawfully work."""
    org = request.organization
    person = org.people.select_related("user").filter(user=request.user).first()
    now = timezone.now()
    if not person:
        return render(request, "core/open_posts.html", {"person": None, "offers": [], "requests": [], "claimed": set(), "horizon_days": OPEN_POST_HORIZON_DAYS})
    horizon = now + timedelta(days=OPEN_POST_HORIZON_DAYS)
    offers = []
    for shift in org.shifts.filter(status=Shift.Status.PUBLISHED, officer__isnull=True, ends_at__gte=now, starts_at__lt=horizon) \
            .select_related("site__client__organization").prefetch_related("required_credentials").order_by("starts_at"):
        allowed, reasons = shift_eligibility(shift, officer=person)
        offers.append({"shift": shift, "allowed": allowed, "reasons": reasons})
    claimed = set(person.shift_claims.filter(status=ShiftClaim.Status.REQUESTED).values_list("shift_id", flat=True))
    return render(request, "core/open_posts.html", {
        "person": person, "offers": offers, "claimed": claimed, "horizon_days": OPEN_POST_HORIZON_DAYS,
        "requests": person.shift_claims.select_related("shift__site").filter(status=ShiftClaim.Status.REQUESTED).order_by("shift__starts_at"),
    })

@require_POST
@membership_required()
@transaction.atomic
def shift_claim(request):
    """An officer asks to work an open post. A dispatcher still has to approve it."""
    org = request.organization
    person = org.people.select_related("user").filter(user=request.user).first()
    if not person:
        return JsonResponse({"error": "Your login is not linked to a personnel record."}, status=403)
    shift = org.shifts.select_related("site").filter(pk=request.POST.get("shift_id"), status=Shift.Status.PUBLISHED, officer__isnull=True).first()
    if not shift:
        messages.error(request, "That post is not open.")
        return redirect("open_posts")
    if shift.ends_at <= timezone.now():
        messages.error(request, "That post has already ended.")
        return redirect("open_posts")
    if person.shift_claims.filter(shift=shift, status=ShiftClaim.Status.REQUESTED).exists():
        messages.error(request, "You have already asked for this post.")
        return redirect("open_posts")
    allowed, reasons = shift_eligibility(shift, officer=person)
    if not allowed:
        messages.error(request, "This post is not open to you: " + " ".join(reasons))
        return redirect("open_posts")
    claim = ShiftClaim.objects.create(organization=org, shift=shift, officer=person, note=request.POST.get("note","").strip()[:255])
    dispatch = dispatch_recipients_for_shift(shift, person)
    queue_notice(organization=org, recipients=dispatch, event_type="shift.claim_requested",
                 subject=f"{person.full_name} asked to take an open post",
                 body=f"{person.full_name} requested {shift.site} on {shift_when(shift)}." + (f" Note: {claim.note}" if claim.note else ""),
                 dedup_key=f"shift-claim:{claim.pk}",
                 sms={"shift": shift, "officer": person, "note": claim.note})
    AuditEvent.objects.create(organization=org, actor=request.user, action="shift.claim_requested", target_type="shift_claim", target_id=str(claim.pk), metadata={"shift": str(shift.pk), "officer": str(person.pk)})
    messages.success(request, "Request sent for approval.")
    return redirect("open_posts")

@require_POST
@membership_required()
@transaction.atomic
def shift_claim_withdraw(request, claim_id):
    claim = ShiftClaim.objects.select_related("shift").filter(pk=claim_id, organization=request.organization, officer__user=request.user, status=ShiftClaim.Status.REQUESTED).first()
    if not claim:
        raise Http404
    claim.status = ShiftClaim.Status.WITHDRAWN
    claim.decided_by = request.user
    claim.decided_at = timezone.now()
    claim.save(update_fields=["status", "decided_by", "decided_at"])
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action="shift.claim_withdrawn", target_type="shift_claim", target_id=str(claim.pk), metadata={"shift": str(claim.shift_id)})
    messages.success(request, "Request withdrawn.")
    return redirect("open_posts")

@membership_required(*MANAGERS)
def shift_requests(request, shift_id):
    shift = request.organization.shifts.select_related("site__client", "officer").filter(pk=shift_id).first()
    if not shift:
        raise Http404
    if not scope_for(request).permits_shift(shift):
        raise Http404
    claims = list(shift.claims.select_related("officer__user", "decided_by").order_by("-created_at"))
    # Show the dispatcher whether each requester still qualifies at this moment, not when
    # they asked; approval re-checks the same rule before anything is written.
    for claim in claims:
        claim.eligible, claim.reasons = shift_eligibility(shift, officer=claim.officer)
    return render(request, "core/shift_requests.html", {"shift": shift, "claims": claims})

@require_POST
@membership_required(*MANAGERS)
@transaction.atomic
def shift_claim_decide(request, claim_id):
    """Approve a request by assigning the officer — after re-checking eligibility.

    The state that mattered when the officer asked may have lapsed since. A credential that
    expired overnight is exactly the case a Texas post cannot absorb, so approval is gated on
    the same rule as a dispatcher's own assignment.
    """
    claim = ShiftClaim.objects.select_for_update().select_related("shift__site", "officer", "shift__organization").filter(pk=claim_id, organization=request.organization, status=ShiftClaim.Status.REQUESTED).first()
    if not claim:
        raise Http404
    if not scope_for(request).permits_shift(claim.shift):
        # Deciding a request is assigning someone to a post, which is exactly what bounded
        # authority exists to limit.
        raise Http404
    action = request.POST.get("action")
    if action not in (ShiftClaim.Status.APPROVED, ShiftClaim.Status.REJECTED):
        messages.error(request, "Approve or decline the request."); return redirect("shift_requests", shift_id=claim.shift_id)
    shift = claim.shift
    uncovered_hours = 0
    if action == ShiftClaim.Status.APPROVED:
        if shift.officer_id:
            messages.error(request, "Another officer is already assigned to this post."); return redirect("shift_requests", shift_id=shift.pk)
        if shift.status not in (Shift.Status.PUBLISHED, Shift.Status.DRAFT):
            messages.error(request, f"A {shift.get_status_display().lower()} post cannot be assigned."); return redirect("shift_requests", shift_id=shift.pk)
        shift.officer = claim.officer
        allowed, reasons = shift_eligibility(shift)
        if not allowed:
            shift.officer = None
            messages.error(request, "Cannot assign: " + " ".join(reasons))
            return redirect("shift_requests", shift_id=shift.pk)
        shift.status = Shift.Status.PUBLISHED
        shift.save(update_fields=["officer", "status"])
        note = request.POST.get("note", "").strip()
        claim.status = ShiftClaim.Status.APPROVED
    else:
        note = request.POST.get("note", "").strip()
        claim.status = ShiftClaim.Status.REJECTED
        # Declining does not *create* a hole — the post was already open, which is why the officer
        # asked — but the dispatcher deciding it should see how much ground stays uncovered rather
        # than only that a name was removed. Same `uncovered_windows` the cancel path uses, so the
        # two screens cannot measure the same night differently (SCH-4).
        gaps = uncovered_windows(request.organization, scope_for(request), shift)
        uncovered_hours = round(sum(item["hours"] for item in gaps), 2)
    claim.decided_by = request.user
    claim.decided_at = timezone.now()
    claim.note = (claim.note + (f" — {note}" if note else ""))[:255]
    claim.save(update_fields=["status", "decided_by", "decided_at", "note"])
    stamp = timezone.now().strftime("%Y%m%d%H%M")
    queue_notice(organization=request.organization, recipients=({claim.officer.user_id} if claim.officer.user_id else set()),
                 event_type=f"shift.claim_{claim.status}",
                 subject=("You're scheduled — " if claim.status == ShiftClaim.Status.APPROVED else "Request declined — ") + str(shift.site),
                 body=(f"Your request for {shift.site} on {shift_when(shift)} was "
                       f"{claim.status}. " + (note if note else "")),
                 dedup_key=f"shift-claim-decision:{claim.pk}:{stamp}",
                 sms={"shift": shift, "note": note})
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action=f"shift.claim_{claim.status}", target_type="shift_claim", target_id=str(claim.pk), metadata={"shift": str(shift.pk), "officer": str(claim.officer_id), "note": note, "uncovered_hours": uncovered_hours})
    messages.success(request, ("Request approved and the post is filled." if claim.status == ShiftClaim.Status.APPROVED
                               else f"Request declined.{f' {shift.site} stays uncovered for {uncovered_hours}h — no other published post there stands those hours.' if uncovered_hours else ''}"))
    return redirect("shift_requests", shift_id=shift.pk)

@membership_required(*MANAGERS)
@transaction.atomic
def shift_create(request):
    scope=scope_for(request)
    form=_scoped_form(ShiftForm,request.organization,request.POST or None,scope=scope,initial=_shift_prefill(request,scope) if request.method!="POST" else None)
    if request.method=="POST" and form.is_valid():
        item=_persist_shift(request,form,Shift.Status.DRAFT)
        if item is not None:
            AuditEvent.objects.create(organization=request.organization,actor=request.user,action="shift.created",target_type="shift",target_id=str(item.pk),metadata={"status":item.status,"site":str(item.site_id)})
            messages.success(request,"Shift saved."); return redirect(_schedule_return(item))
    return render(request,"core/form.html",{"form":form,"title":"Create shift","eyebrow":"Scheduling","cancel_url":reverse("schedule")})

@membership_required(*MANAGERS)
@transaction.atomic
def shift_edit(request,shift_id):
    shift=request.organization.shifts.filter(pk=shift_id).first()
    if not shift:raise Http404
    if not scope_for(request).permits_shift(shift):raise Http404
    previous_status=shift.status
    form=_scoped_form(ShiftForm,request.organization,request.POST or None,instance=shift,scope=scope_for(request))
    if request.method=="POST" and form.is_valid():
        item=_persist_shift(request,form,previous_status)
        if item is not None:
            AuditEvent.objects.create(organization=request.organization,actor=request.user,action="shift.updated",target_type="shift",target_id=str(item.pk),metadata={"status":item.status,"previous_status":previous_status})
            messages.success(request,"Shift updated."); return redirect(_schedule_return(item))
    return render(request,"core/form.html",{"form":form,"title":"Edit shift","eyebrow":"Scheduling","cancel_url":_schedule_return(shift)})

@require_POST
@membership_required(*MANAGERS)
@transaction.atomic
def shift_cancel(request,shift_id):
    """Cancel a post and tell the officer who was standing it.

    A cancellation the assigned guard never hears about is how a post goes uncovered; peers
    treat it as the highest-priority schedule event.
    """
    shift=request.organization.shifts.select_related("officer__user","site").filter(pk=shift_id).first()
    if not shift:raise Http404
    if not scope_for(request).permits_shift(shift):raise Http404
    if shift.status==Shift.Status.COMPLETED:
        messages.error(request,"A completed shift is timecard evidence and cannot be cancelled.");return redirect("schedule")
    reason=request.POST.get("reason","").strip()
    if len(reason)<5:
        messages.error(request,"Give a cancellation reason of at least 5 characters.");return redirect("schedule")
    if Punch.objects.filter(shift=shift).exists():
        messages.error(request,"This post already has recorded punches. Correct the timecards instead of erasing the assignment.")
        return redirect("schedule")
    # Measured *before* the row changes. Once the post is cancelled it leaves the schedule, so the
    # page that would have shown the hole no longer contains it — which is exactly how a cancelled
    # relief becomes a dark post nobody noticed (SCH-4). `coverage_state` decides what counts as
    # covered here and inside `coverage_report`, so the warning and the report cannot disagree.
    gap = uncovered_advisory(request.organization, scope_for(request), shift) if shift.officer_id else {"gaps": [], "text": ""}
    shift.status=Shift.Status.CANCELLED;shift.save(update_fields=["status"])
    queue_shift_notice(shift,event_type="shift.cancelled",dedup_key=f"shift-cancelled:{shift.pk}:{timezone.now().strftime('%Y%m%d%H%M')}",
                       subject="Your shift was cancelled",body=f"{shift.site} on {shift_when(shift)} was cancelled: {reason}",
                       sms={"note":reason})
    hours=round(sum(item["hours"] for item in gap["gaps"]), 2)
    if gap["gaps"]:
        # The dispatcher whose authority reaches the post, never the actor who pressed the button:
        # they are the party who can still fill it.
        recipients=set(dispatch_recipients_for_shift(shift, None)) - {request.user.pk}
        if recipients:
            queue_notice(organization=request.organization, recipients=recipients, event_type="shift.coverage_gap",
                         subject=f"{shift.site} loses {hours}h of coverage",
                         body=gap["text"] + f" Cancelled post: {shift_when(shift)}. Reason: {reason}",
                         dedup_key=f"shift-gap:{shift.pk}",
                         sms={"shift": shift, "hours": f"{hours:g}", "note": reason})
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="shift.cancelled",target_type="shift",target_id=str(shift.pk),metadata={"reason":reason,"site":str(shift.site_id),"uncovered_hours":hours,"uncovered_windows":len(gap["gaps"])})
    messages.success(request,"Shift cancelled and the officer notified." + (f" {gap['text']} Post it as open, or find relief before {timezone.localtime(shift.starts_at):%a %H:%M}." if gap["gaps"] else ""))
    return redirect(_schedule_return(shift))

@membership_required(*MANAGERS)
def shift_templates(request):
    """The recurring series this firm staffs by, and what each has already produced."""
    scope = scope_for(request)
    templates = scope.filter_sites(request.organization.shift_templates.select_related("site__client", "officer")).annotate(post_count=Count("shifts"))
    return render(request, "core/shift_templates.html", {
        "templates": list(templates),
        "authority_scope": scope if scope.restricted else None,
        "open_series": sum(1 for item in templates if not item.officer_id),
        "can_remove": request.membership.role in PRIVILEGED,
    })

@membership_required(*MANAGERS)
@transaction.atomic
def shift_template_create(request):
    form = _scoped_form(ShiftTemplateForm, request.organization, request.POST or None, scope=scope_for(request))
    if request.method == "POST" and form.is_valid():
        template = form.save()
        AuditEvent.objects.create(organization=request.organization, actor=request.user,
            action="shift_template.created", target_type="shift_template", target_id=str(template.pk),
            metadata={"name": template.name, "site": str(template.site_id), "days": template.day_indexes,
                      "window": template.window_label, "pattern": template.pattern})
        messages.success(request, f"Series saved: {template.name}. Generate its posts when you are ready.")
        return redirect("shift_template_generate", template_id=template.pk)
    return render(request, "core/form.html", {"form": form, "title": "New recurring series",
        "eyebrow": "Scheduling", "cancel_url": reverse("shift_templates")})

@membership_required(*MANAGERS)
def shift_template_edit(request, template_id):
    template = request.organization.shift_templates.filter(pk=template_id).first()
    if not template or not scope_for(request).permits_site(template.site):
        raise Http404
    return _catalog_edit(request, form_class=ShiftTemplateForm, instance=template, name="shift_template",
        action="shift_template.updated", title="Edit recurring series", eyebrow="Scheduling",
        cancel_url=reverse("shift_templates"),
        success="Series updated. Posts already generated keep their own dates and rates — editing the pattern does not rewrite them.")

@membership_required(*MANAGERS)
def shift_template_generate(request, template_id):
    """Preview the dates a series would produce before any post exists.

    The same two-stage shape as a bulk import, because the decision is the same one: a dispatcher
    has to see which dates the series' own gates refuse, and why, before the schedule gains fourteen
    rows they did not ask for. Nothing is written until the second submit.
    """
    template = request.organization.shift_templates.select_related("site__client__organization", "officer").filter(pk=template_id).first()
    if not template or not scope_for(request).permits_site(template.site):
        raise Http404
    today = timezone.localdate()
    # Default the form to the next stretch that has not been generated yet, so filling the same
    # contract again is a press, not a re-typed pair of dates. A series with an end date stops on
    # its own; an open-ended one reaches four weeks.
    window = template.next_window(today)
    initial = {"range_start": window[0] if window else today,
               "range_end": window[1] if window else today}
    plan = None
    if request.method == "POST":
        form = ShiftGenerationForm(request.POST, initial=initial)
        if form.is_valid():
            start = form.cleaned_data["range_start"]
            end = form.cleaned_data["range_end"]
            status = form.cleaned_data["status"]
            if request.POST.get("action") == "generate":
                plan = apply_recurring_plan(template, start, end, status, actor=request.user)
                blocked = sum(1 for row in plan["rows"] if not row["created"])
                if plan["count"]:
                    messages.success(request, f"{plan['count']} post{'' if plan['count'] == 1 else 's'} added from {template.name}.")
                else:
                    messages.warning(request, "No posts were added — every date in that range was already scheduled or refused.")
                if blocked:
                    messages.warning(request, f"{blocked} date{'' if blocked == 1 else 's'} blocked; the preview lists the reason for each.")
                # Land the dispatcher on the week they just filled, not on the one they were looking at.
                weeks = ((start - timedelta(days=start.weekday())) - (today - timedelta(days=today.weekday()))).days // 7
                return redirect(f"{reverse('schedule')}?week={max(-WEEK_WINDOW, min(WEEK_WINDOW, weeks))}")
            plan = recurring_plan(template, start, end, status)
            plan["range"] = (start, end)
            plan["status"] = status
    else:
        form = ShiftGenerationForm(initial=initial)
    return render(request, "core/shift_template_generate.html", {
        "template": template, "form": form, "plan": plan, "window": window,
        "requirements": post_requirements(Shift(organization=request.organization, site_id=template.site_id),
                                          posted=list(template.required_credentials.all())),
    })

@require_POST
@membership_required(*MANAGERS)
def shift_template_pause(request, template_id):
    """Stop a series generating further posts without touching the roster it already built."""
    template = request.organization.shift_templates.select_related("site").filter(pk=template_id).first()
    if not template or not scope_for(request).permits_site(template.site):
        raise Http404
    template.active = not template.active
    template.save(update_fields=["active", "updated_at"])
    AuditEvent.objects.create(organization=request.organization, actor=request.user,
        action="shift_template.paused" if not template.active else "shift_template.resumed",
        target_type="shift_template", target_id=str(template.pk), metadata={"name": template.name})
    messages.success(request, f"{template.name} will generate no further posts." if not template.active
        else f"{template.name} is generating again.")
    return redirect("shift_templates")


@require_POST
@membership_required(*PRIVILEGED)
def shift_template_remove(request, template_id):
    """Retire a series. The posts it produced stay on the schedule and keep their own history.

    A generated post is a coverage fact, not a property of the pattern: it may be filled, worked,
    and paid by now. ``Shift.template`` is ``SET_NULL``, so removing the series leaves the rows and
    simply stops them generating again — which is why this is owner/admin rather than any manager.
    """
    template = request.organization.shift_templates.select_related("site").filter(pk=template_id).first()
    if not template or not scope_for(request).permits_site(template.site):
        raise Http404
    label = template.name
    remaining = template.shifts.exclude(status=Shift.Status.CANCELLED).count()
    template_id = template.pk
    template.delete()
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action="shift_template.removed",
        target_type="shift_template", target_id=str(template_id), metadata={"name": label, "posts_kept": remaining})
    messages.success(request, f"Removed {label}. {remaining} post{'' if remaining == 1 else 's'} it produced stay on the schedule.")
    return redirect("shift_templates")


SWAP_OPEN = (ShiftSwap.Status.OFFERED, ShiftSwap.Status.AGREED)
EXCHANGE_OPEN = (ShiftExchange.Status.PROPOSED, ShiftExchange.Status.AGREED)
DECIDED_WINDOW = 40


def _open_swaps(organization, scope):
    """The handoffs still awaiting an answer, bounded to what this actor may decide.

    One definition, shared by the approval queue and the overview, so a count on the landing page
    and the rows on the queue cannot disagree — the same reason the compliance queue is the only
    place "needs attention" is computed.
    """
    return [row for row in ShiftSwap.objects.filter(organization=organization, status__in=SWAP_OPEN)
            .select_related("shift__site__client", "requester", "replacement").order_by("shift__starts_at")
            if scope.permits_shift(row.shift)]


def _open_exchanges(organization, scope):
    """The two-way trades still awaiting an answer, on the same bounded authority."""
    rows = []
    for row in ShiftExchange.objects.filter(organization=organization, status__in=EXCHANGE_OPEN).select_related(
            "initiator_shift__site__client", "partner_shift__site__client", "initiator", "partner"):
        if scope.permits_shift(row.initiator_shift) and (not row.partner_shift_id or scope.permits_shift(row.partner_shift)):
            rows.append(row)
    return rows


def _linked_person(request):
    return request.organization.people.filter(user=request.user).first()


MY_WEEK_BACK = 4
MY_WEEK_AHEAD = 8

def _my_week(org, person, offset, upcoming_ids):
    """One officer's workweek as seven days: their posts, leave, stated availability and open work.

    Built on the same workweek the dispatcher's grid and payroll use, so the hours total a guard
    reads here is the number overtime will be counted against.
    """
    today = timezone.localdate()
    week_start = schedule_week_start(today, offset, organization=org)
    days = [week_start + timedelta(days=index) for index in range(7)]
    bounds = [(timezone.make_aware(datetime.combine(day, datetime.min.time())),
               timezone.make_aware(datetime.combine(day + timedelta(days=1), datetime.min.time()))) for day in days]
    start_at, end_at = bounds[0][0], bounds[-1][1]
    shifts = list(person.shifts.filter(organization=org, site__organization=org, starts_at__lt=end_at, starts_at__gte=start_at)
                  .exclude(status=Shift.Status.CANCELLED).select_related("site").order_by("starts_at"))
    ids = [shift.pk for shift in shifts]
    moving = set(ShiftSwap.objects.filter(organization=org, requester=person, status__in=SWAP_OPEN, shift_id__in=ids).values_list("shift_id", flat=True))
    for pair in ShiftExchange.objects.filter(Q(initiator_shift_id__in=ids) | Q(partner_shift_id__in=ids), organization=org,
                                             status__in=EXCHANGE_OPEN).values_list("initiator_shift_id", "partner_shift_id"):
        moving.update(pair)
    leave = list(person.time_off_requests.filter(organization=org, starts_at__lt=end_at, ends_at__gt=start_at,
                 status__in=(TimeOffRequest.Status.APPROVED, TimeOffRequest.Status.REQUESTED)))
    windows = {}
    for rule in person.availability_rules.all():
        windows.setdefault(rule.weekday, []).append(rule)
    now = timezone.now()
    open_by_day = {}
    claimed = set(person.shift_claims.filter(status=ShiftClaim.Status.REQUESTED).values_list("shift_id", flat=True))
    # Only what /open-posts/ lists, so the link from a day always lands on posts that are there.
    for shift in org.shifts.filter(status=Shift.Status.PUBLISHED, officer__isnull=True, ends_at__gte=now,
                                   starts_at__lt=min(end_at, now + timedelta(days=OPEN_POST_HORIZON_DAYS)),
                                   starts_at__gte=start_at).prefetch_related("required_credentials"):
        allowed, _ = shift_eligibility(shift, officer=person)
        if allowed or shift.pk in claimed:
            open_by_day.setdefault(timezone.localtime(shift.starts_at).date(), []).append(shift.pk in claimed)
    policy = TimePolicy.objects.filter(organization=org).only("overtime_after_hours").first()
    cells = []
    for day, (start, end) in zip(days, bounds):
        mine = [shift for shift in shifts if timezone.localtime(shift.starts_at).date() == day]
        for shift in mine:
            shift.moving = shift.pk in moving
            shift.listed = shift.pk in upcoming_ids
        away = next((row for row in leave if row.starts_at < end and row.ends_at > start), None)
        offers = open_by_day.get(day, [])
        cells.append({"date": day, "today": day == today, "past": day < today, "shifts": mine, "leave": away,
                      "availability": windows.get(day.weekday(), []),
                      "open_count": len(offers), "requested_count": sum(offers)})
    hours = round(sum((shift.ends_at - shift.starts_at).total_seconds() for shift in shifts) / 3600, 2)
    threshold = policy.overtime_after_hours if policy else None
    return {"days": cells, "start": week_start, "last": days[-1], "offset": offset, "hours": hours,
            "threshold": threshold, "over": threshold is not None and hours > threshold,
            "stated_availability": bool(windows),
            "can_back": offset > -MY_WEEK_BACK, "can_ahead": offset < MY_WEEK_AHEAD}


@membership_required()
def my_shifts(request):
    """An officer's own roster, and the one way to move a post they are already stood on.

    Nothing here existed before: ``/schedule/`` is a dispatcher's surface and ``/open-posts/``
    shows only the unfilled ones, so a guard could not see the posts assigned to them — which is
    also the page a swap has to start from.
    """
    person = _linked_person(request)
    if person is None:
        if request.GET.get("attendance"):
            raise Http404
        return render(request, "core/my_shifts.html", {"person": None, "upcoming": [], "incoming": [],
                                                   "outgoing": [], "proposals": [], "outgoing_exchanges": []})
    now = timezone.now()
    # Operational client contacts are disclosed only in the employee's own assignment
    # brief, not in peer offers or a general client directory.
    upcoming = list(person.shifts.exclude(status=Shift.Status.CANCELLED).filter(
                    organization=request.organization, site__organization=request.organization,
                    site__client__organization=request.organization, ends_at__gt=now)
                    .select_related("site__client__organization", "template").order_by("starts_at")[:40])
    open_rows = {}
    for row in ShiftSwap.objects.filter(organization=request.organization, requester=person, status__in=SWAP_OPEN).select_related("replacement"):
        open_rows.setdefault(row.shift_id, row)
    open_exchanges = {}
    for row in ShiftExchange.objects.filter(Q(initiator=person) | Q(partner=person), organization=request.organization, status__in=EXCHANGE_OPEN).select_related(
            "initiator", "partner", "initiator_shift", "partner_shift"):
        open_exchanges.setdefault(row.initiator_shift_id, row)
        if row.partner_shift_id:
            open_exchanges.setdefault(row.partner_shift_id, row)
    for shift in upcoming:
        shift.swap = open_rows.get(shift.pk)
        shift.exchange = open_exchanges.get(shift.pk)
        shift.can_offer = shift.status == Shift.Status.PUBLISHED and shift.swap is None and shift.exchange is None
    incoming = list(ShiftSwap.objects.filter(organization=request.organization, replacement=person,
                    shift__organization=request.organization, requester__organization=request.organization,
                    shift__site__organization=request.organization, shift__site__client__organization=request.organization,
                    status=ShiftSwap.Status.OFFERED, shift__status=Shift.Status.PUBLISHED, shift__starts_at__gt=now)
                    .select_related("shift__site__client", "requester").order_by("shift__starts_at"))
    for row in incoming:
        # Someone deciding whether they *want* the post should know whether they can stand it —
        # the rule that actually moves the assignment is still the manager's check at approval.
        row.eligible, row.reasons = shift_eligibility(row.shift, officer=person)
    proposals = list(ShiftExchange.objects.filter(organization=request.organization, partner=person,
        initiator__organization=request.organization, initiator_shift__organization=request.organization,
        initiator_shift__site__organization=request.organization, initiator_shift__site__client__organization=request.organization,
        status=ShiftExchange.Status.PROPOSED, initiator_shift__status=Shift.Status.PUBLISHED, initiator_shift__starts_at__gt=now)
        .select_related("initiator_shift__site__client", "initiator").order_by("initiator_shift__starts_at"))
    for row in proposals:
        row.eligible, row.reasons = shift_eligibility(row.initiator_shift, officer=person)
        # The colleague has to name which of their own posts goes into the trade, so the accept
        # panel needs the list at the moment the decision is made (Deputy and When I Work both let
        # the responder choose among several rather than making the proposer pick for them).
        row.tradeable = list(person.shifts.filter(organization=request.organization,
            site__organization=request.organization, site__client__organization=request.organization,
            status=Shift.Status.PUBLISHED, starts_at__gt=now)
            .exclude(pk=row.initiator_shift_id).select_related("site__client").order_by("starts_at"))
    outgoing = list(person.swaps_offered.filter(organization=request.organization,
        shift__organization=request.organization, shift__site__organization=request.organization,
        replacement__organization=request.organization).select_related("shift__site", "replacement")[:20])
    outgoing_exchanges = list(ShiftExchange.objects.filter(organization=request.organization, initiator=person,
        initiator_shift__organization=request.organization, initiator_shift__site__organization=request.organization,
        partner__organization=request.organization).select_related(
        "partner", "initiator_shift", "partner_shift")[:20])
    received = list(ShiftSwap.objects.filter(organization=request.organization, replacement=person,
        shift__organization=request.organization, shift__site__organization=request.organization,
        requester__organization=request.organization)
        .exclude(status=ShiftSwap.Status.OFFERED).select_related("shift__site", "requester")[:20])
    received_exchanges = list(ShiftExchange.objects.filter(organization=request.organization, partner=person,
        initiator_shift__organization=request.organization, initiator_shift__site__organization=request.organization,
        initiator__organization=request.organization)
        .exclude(status=ShiftExchange.Status.PROPOSED).select_related("initiator", "initiator_shift__site", "partner_shift__site")[:20])
    try:
        offset = max(-MY_WEEK_BACK, min(MY_WEEK_AHEAD, int(request.GET.get("week", "0"))))
    except ValueError:
        offset = 0
    week = _my_week(request.organization, person, offset, {shift.pk for shift in upcoming})
    from .models import AttendanceCase
    attendance_cases = request.organization.attendance_cases.filter(person=person).select_related("shift__site__client")
    attendance_status = request.GET.get("attendance_status", "open")
    if attendance_status not in ("open", "resolved", "all"):
        messages.error(request, "Unknown attendance filter; showing open alerts.")
        attendance_status = "open"
    selected_attendance = None
    if request.GET.get("attendance"):
        selected_attendance = get_object_or_404(attendance_cases, pk=_uuid_or_none(request.GET["attendance"]))
    attendance_rows = attendance_cases if attendance_status == "all" else attendance_cases.filter(status=attendance_status)
    attendance_page = Paginator(attendance_rows.order_by("-opened_at", "pk"), 5).get_page(request.GET.get("attendance_page"))
    return render(request, "core/my_shifts.html", {"person": person, "upcoming": upcoming, "week": week,
        "incoming": incoming, "outgoing": outgoing, "proposals": proposals,
        "outgoing_exchanges": outgoing_exchanges, "received": received,
        "received_exchanges": received_exchanges, "attendance_alerts": attendance_page,
        "attendance_status": attendance_status, "selected_attendance": selected_attendance,
        "attendance_open_count": attendance_cases.filter(status="open").count()})


@membership_required()
@transaction.atomic
def offer_post(request, shift_id):
    """Start moving a filled post: hand it to one colleague, or trade one for one.

    Peers keep these as two different things and name them differently — Connecteam: "**Offer
    Shift: a one-way handoff.** The employee gives the shift to a qualified teammate and is no
    longer scheduled for it" versus "**Swap Shift: a two-way trade.** … both stay scheduled and both
    keep their planned hours for the week." The officer chooses which one they mean on one screen,
    from one post, instead of guessing which page holds the thing they want.

    The gates here are only that the post is standing, published, in the future, and not already in
    motion. Qualification is *filtered* here (an offer to someone who could never take it wastes
    everyone's time) and still re-checked at approval (a licence that lapses overnight is what a
    Texas post cannot absorb).

    A manager may raise the same request on an officer's behalf. That is a flag on the record, not a
    different flow: the consent edges belong to the object, so a manager asking does not let anyone
    skip the colleague's answer — which is exactly the distinction Deputy draws between a
    "Manager Initiated Swap" and simply editing the assignee.
    """
    person = _linked_person(request)
    managing = request.membership.role in MANAGERS
    shift = request.organization.shifts.select_related("site__client", "officer").filter(pk=shift_id).first()
    if not shift or (shift.officer_id is None and not managing):
        raise Http404
    if person is None and not managing:
        messages.error(request, "Your sign-in is not linked to a personnel record.")
        return redirect("my_shifts")
    standing = shift.officer
    if person is not None and standing and standing.pk == person.pk:
        raising = ShiftSwap.RaisedBy.OFFICER
    elif managing:
        # A manager acting on their own post is still just an officer offering it; a manager
        # acting on someone else's is raising it for them.
        raising = (ShiftSwap.RaisedBy.OFFICER if standing and standing.pk == (person.pk if person else None)
                   else ShiftSwap.RaisedBy.MANAGER)
        if raising == ShiftSwap.RaisedBy.MANAGER and not scope_for(request).permits_shift(shift):
            raise Http404
    else:
        raise Http404
    if raising == ShiftSwap.RaisedBy.MANAGER and standing is None:
        messages.error(request, "An unfilled post is not a swap — publish it and officers can ask to take it from Open posts.")
        return redirect("schedule")
    if shift.ends_at <= timezone.now():
        messages.error(request, "That post has already ended.")
        return redirect("my_shifts" if raising == ShiftSwap.RaisedBy.OFFICER else "schedule")
    if shift.status != Shift.Status.PUBLISHED:
        messages.error(request, f"A {shift.get_status_display().lower()} post is not available to move.")
        return redirect("my_shifts" if raising == ShiftSwap.RaisedBy.OFFICER else "schedule")
    if ShiftSwap.objects.filter(shift=shift, status__in=SWAP_OPEN).exists() or \
            ShiftExchange.objects.filter(Q(initiator_shift=shift) | Q(partner_shift=shift), status__in=EXCHANGE_OPEN).exists():
        messages.error(request, "There is already an open request on this post.")
        return redirect("my_shifts" if raising == ShiftSwap.RaisedBy.OFFICER else "schedule")
    # Deputy and When I Work both restrict who may be asked at all, and for the same reason: an
    # officer who offers a post to someone who could never stand it has spent a colleague's
    # attention and filled a manager's queue with an offer that can only be refused. The name of
    # anyone excluded is shown with the rule that excluded them, because "Sam is not on the list"
    # is only useful if the officer can see that Sam's registration is what is missing.
    eligible, blocked = swap_candidates(shift, excluding=standing)
    # A trade also needs the other side to have something to put in. Only their *existence* is
    # checked here; whether this officer can actually stand that post is settled when the partner
    # names it and again at approval, because testing every pair up front is N×M eligibility runs.
    can_trade = set(Shift.objects.filter(organization=request.organization, status=Shift.Status.PUBLISHED,
        starts_at__gt=timezone.now(), officer__in=[item.pk for item in eligible]).values_list("officer_id", flat=True))
    traders = [item for item in eligible if item.pk in can_trade]
    mode = request.POST.get("mode") if request.method == "POST" else None
    handoff = ShiftSwapForm(request.POST if mode == "handoff" else None)
    handoff.fields["replacement"].queryset = Person.objects.filter(pk__in=[item.pk for item in eligible])
    trade = ShiftExchangeForm(request.POST if mode == "trade" else None)
    trade.fields["partner"].queryset = Person.objects.filter(pk__in=[item.pk for item in traders])
    if mode in ("handoff", "trade"):
        form = handoff if mode == "handoff" else trade
        if not form.is_valid():
            return render(request, "core/offer_post.html", {"form": form, "trade": trade, "handoff": handoff,
                "shift": shift, "person": standing, "blocked": blocked, "eligible": len(eligible),
                "traders": len(traders), "raising": raising})
        if mode == "handoff":
            replacement = form.cleaned_data["replacement"]
            swap = ShiftSwap.objects.create(organization=request.organization, shift=shift, requester=standing,
                replacement=replacement, raised_by=raising, created_by=request.user,
                note=form.cleaned_data["note"].strip()[:255])
            queue_notice(organization=request.organization, recipients={replacement.user_id} - {None},
                event_type="shift.swap_offered",
                subject=f"{standing.full_name} asked you to take a post",
                body=f"{shift.site} on {shift_when(shift)}."
                     + (f" Reason: {swap.note}" if swap.note else ""),
                dedup_key=f"shift-swap:{swap.pk}",
                sms={"shift": shift, "officer": standing, "note": swap.note})
            if raising == ShiftSwap.RaisedBy.MANAGER and standing.user_id:
                queue_notice(organization=request.organization, recipients={standing.user_id},
                    event_type="shift.swap_offered", subject="Cover is being arranged for your post",
                    body=f"A manager offered {shift.site} on {shift_when(shift)} to "
                         f"{replacement.full_name}. You keep the post until a manager approves the move.",
                    dedup_key=f"shift-swap-raised:{swap.pk}",
                    sms={"notice": "shift.swap_offered.officer", "shift": shift, "other": replacement})
            AuditEvent.objects.create(organization=request.organization, actor=request.user, action="shift.swap_offered",
                target_type="shift_swap", target_id=str(swap.pk),
                metadata={"shift": str(shift.pk), "requester": str(standing.pk), "replacement": str(replacement.pk),
                          "raised_by": raising})
            messages.success(request, f"Offer sent to {replacement.full_name}. A manager approves it once they accept.")
        else:
            partner = form.cleaned_data["partner"]
            exchange = ShiftExchange.objects.create(organization=request.organization, initiator=standing,
                initiator_shift=shift, partner=partner, raised_by=ShiftExchange.RaisedBy.OFFICER
                if raising == ShiftSwap.RaisedBy.OFFICER else ShiftExchange.RaisedBy.MANAGER,
                created_by=request.user, note=form.cleaned_data["note"].strip()[:255])
            queue_notice(organization=request.organization, recipients={partner.user_id} - {None},
                event_type="shift.exchange_proposed",
                subject=f"{standing.full_name} wants to trade posts with you",
                body=f"{shift.site} on {shift_when(shift)} — choose which of your own posts "
                     "to put in, from My shifts. Nothing moves until a manager approves the pair.",
                dedup_key=f"shift-exchange:{exchange.pk}",
                sms={"shift": shift, "officer": standing, "note": exchange.note})
            if raising == ShiftSwap.RaisedBy.MANAGER and standing.user_id:
                queue_notice(organization=request.organization, recipients={standing.user_id},
                    event_type="shift.exchange_proposed", subject="A trade is being arranged for your post",
                    body=f"A manager proposed trading {shift.site} on {shift_when(shift)} with "
                         f"{partner.full_name}. You keep the post until a manager approves.",
                    dedup_key=f"shift-exchange-raised:{exchange.pk}",
                    sms={"notice": "shift.exchange_proposed.officer", "shift": shift, "other": partner})
            AuditEvent.objects.create(organization=request.organization, actor=request.user, action="shift.exchange_proposed",
                target_type="shift_exchange", target_id=str(exchange.pk),
                metadata={"shift": str(shift.pk), "initiator": str(standing.pk), "partner": str(partner.pk),
                          "raised_by": exchange.raised_by})
            messages.success(request, f"Trade proposed to {partner.full_name}.")
        return redirect("my_shifts" if raising == ShiftSwap.RaisedBy.OFFICER else "schedule")
    return render(request, "core/offer_post.html", {"form": handoff, "trade": trade, "handoff": handoff,
        "shift": shift, "person": standing, "blocked": blocked, "eligible": len(eligible),
        "traders": len(traders), "raising": raising})


@require_POST
@membership_required()
@transaction.atomic
def swap_respond(request, swap_id):
    """The colleague answers: accept, and the offer goes to a manager; decline, and it stops here."""
    person = _linked_person(request)
    if person is None:
        raise Http404
    swap = ShiftSwap.objects.select_for_update().select_related("shift__site", "requester").filter(
        pk=swap_id, organization=request.organization, replacement=person,
        status=ShiftSwap.Status.OFFERED).first()
    if not swap:
        raise Http404
    action = request.POST.get("action")
    if action not in (ShiftSwap.Status.AGREED, ShiftSwap.Status.DECLINED):
        messages.error(request, "Accept or decline the offer.")
        return redirect("my_shifts")
    shift = swap.shift
    if shift.ends_at <= timezone.now():
        swap.status = ShiftSwap.Status.WITHDRAWN
        swap.decided_at = timezone.now()
        swap.save(update_fields=["status", "decided_at"])
        messages.error(request, "That post has already ended.")
        return redirect("my_shifts")
    swap.status = action
    if action == ShiftSwap.Status.AGREED:
        swap.agreed_at = timezone.now()
    swap.save(update_fields=["status", "agreed_at"])
    stamp = timezone.now().strftime("%Y%m%d%H%M")
    if action == ShiftSwap.Status.AGREED:
        # The supervisor who is told about the move is the one whose granted authority covers the
        # post, so a branch dispatcher is not asked to approve a tour they cannot staff.
        queue_notice(organization=request.organization, recipients=dispatch_recipients_for_shift(shift, swap.requester),
            event_type="shift.swap_agreed",
            subject=f"Shift swap waiting for approval — {shift.site}",
            body=f"{swap.requester.full_name} offered {shift_when(shift)} to {person.full_name}, "
                 "and they accepted. Approve it in Shift swaps.",
            dedup_key=f"shift-swap-agreed:{swap.pk}:{stamp}",
            sms={"shift": shift, "officer": swap.requester, "other": person})
    queue_notice(organization=request.organization, recipients={swap.requester.user_id} - {None},
        event_type=f"shift.swap_{swap.status}",
        subject=("Your swap was accepted — waiting on a manager" if action == ShiftSwap.Status.AGREED
                 else "Your colleague could not take the post"),
        body=f"{person.full_name} {swap.status} your offer of {shift.site} on {shift_when(shift)}.",
        dedup_key=f"shift-swap-answer:{swap.pk}:{stamp}",
        sms={"notice": "shift.swap_agreed.requester" if action == ShiftSwap.Status.AGREED else "shift.swap_declined",
             "shift": shift, "officer": person})
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action=f"shift.swap_{swap.status}",
        target_type="shift_swap", target_id=str(swap.pk), metadata={"shift": str(shift.pk)})
    messages.success(request, "Accepted — a manager whose authority covers this post is asked to approve it."
        if action == ShiftSwap.Status.AGREED else "Offer declined.")
    return redirect("my_shifts")


@require_POST
@membership_required()
def swap_withdraw(request, swap_id):
    """The officer who offered the post calls it back, any time before a manager decides."""
    person = _linked_person(request)
    swap = ShiftSwap.objects.filter(pk=swap_id, organization=request.organization,
        requester=person, status__in=SWAP_OPEN).select_related("shift", "replacement").first() if person else None
    if not swap:
        raise Http404
    swap.status = ShiftSwap.Status.WITHDRAWN
    swap.decided_by = request.user
    swap.decided_at = timezone.now()
    swap.save(update_fields=["status", "decided_by", "decided_at"])
    queue_notice(organization=request.organization, recipients={swap.replacement.user_id} - {None},
        event_type="shift.swap_withdrawn", subject="A swap offer was withdrawn",
        body=f"{swap.requester.full_name} no longer needs {swap.shift.site} on {shift_when(swap.shift)} covered.",
        dedup_key=f"shift-swap-withdrawn:{swap.pk}",
        sms={"shift": swap.shift, "officer": swap.requester})
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action="shift.swap_withdrawn",
        target_type="shift_swap", target_id=str(swap.pk), metadata={"shift": str(swap.shift_id)})
    messages.success(request, "Offer withdrawn.")
    return redirect("my_shifts")


@require_POST
@membership_required()
@transaction.atomic
def exchange_respond(request, exchange_id):
    """The colleague answers a trade — and chooses which of their own posts goes into it.

    Deputy and When I Work both have the responder collapse several candidates into one pair ("If
    the team member has offered the shift for more than one of your shifts, you can select which one
    you'd like to swap"), so the proposer never picks a colleague's post for them. Declining ends it
    with nothing moved; accepting is the *second* of the three consents, and the manager's decision
    over the pair is still to come.
    """
    person = _linked_person(request)
    if person is None:
        raise Http404
    exchange = ShiftExchange.objects.select_for_update().select_related(
        "initiator_shift", "partner_shift", "initiator", "organization").filter(
        pk=exchange_id, organization=request.organization, partner=person,
        status=ShiftExchange.Status.PROPOSED).first()
    if not exchange:
        raise Http404
    action = request.POST.get("action")
    now = timezone.now()
    if exchange.initiator_shift.officer_id != exchange.initiator_id or exchange.initiator_shift.ends_at <= now:
        exchange.status = ShiftExchange.Status.EXPIRED
        exchange.review_note = "The post on offer was no longer there to trade."
        _decide_exchange(exchange, request)
        messages.error(request, "That trade is no longer available — the post on offer has moved or ended.")
        return redirect("my_shifts")
    if action == ShiftExchange.Status.DECLINED:
        exchange.status = ShiftExchange.Status.DECLINED
        _decide_exchange(exchange, request)
        queue_notice(organization=request.organization, recipients={exchange.initiator.user_id} - {None},
            event_type="shift.exchange_declined", subject="Your colleague could not trade",
            body=f"{person.full_name} declined to trade posts with you. You keep {exchange.initiator_shift.site} "
                 "on the schedule — ask someone else, or offer the post outright.",
            dedup_key=f"shift-exchange-declined:{exchange.pk}",
            sms={"shift": exchange.initiator_shift, "officer": person})
        AuditEvent.objects.create(organization=request.organization, actor=request.user,
            action="shift.exchange_declined", target_type="shift_exchange", target_id=str(exchange.pk),
            metadata={"partner": str(person.pk)})
        messages.success(request, "Trade declined. Nothing has moved.")
        return redirect("my_shifts")
    if action != ShiftExchange.Status.AGREED:
        messages.error(request, "Accept or decline the trade.")
        return redirect("my_shifts")
    form = ExchangeAcceptForm(request.POST)
    form.fields["partner_shift"].queryset = person.shifts.filter(
        status=Shift.Status.PUBLISHED, starts_at__gt=now).exclude(pk=exchange.initiator_shift_id).select_related("site")
    if not form.is_valid():
        messages.error(request, "Choose one of your own future posts to put in the trade.")
        return redirect("my_shifts")
    exchange.partner_shift = form.cleaned_data["partner_shift"]
    exchange.note = (exchange.note + (f" — {form.cleaned_data['note'].strip()}" if form.cleaned_data["note"].strip() else ""))[:255]
    exchange.status = ShiftExchange.Status.AGREED
    exchange.agreed_at = now
    exchange.save(update_fields=["partner_shift", "note", "status", "agreed_at"])
    managers = dispatch_recipients_for_shift(exchange.initiator_shift, exchange.initiator) | \
        dispatch_recipients_for_shift(exchange.partner_shift, exchange.partner)
    managers -= exchange.involved_user_ids()
    queue_notice(organization=request.organization, recipients=managers,
        event_type="shift.exchange_agreed",
        subject=f"Shift trade waiting for approval — {exchange.initiator_shift.site}",
        body=f"{exchange.initiator.full_name} and {person.full_name} agreed to trade two posts. "
             "Approve or refuse the pair in Shift moves.",
        dedup_key=f"shift-exchange-agreed:{exchange.pk}:{now.strftime('%Y%m%d%H%M')}",
        sms={"shift": exchange.initiator_shift, "officer": exchange.initiator, "other": person})
    queue_notice(organization=request.organization, recipients={exchange.initiator.user_id} - {None},
        event_type="shift.exchange_agreed", subject="Your trade was accepted — waiting on a manager",
        body=f"{person.full_name} put in {exchange.partner_shift.site} on "
             f"{shift_when(exchange.partner_shift)}. Nothing moves until a manager approves the pair.",
        dedup_key=f"shift-exchange-accepted:{exchange.pk}",
        sms={"notice": "shift.exchange_agreed.initiator", "shift": exchange.partner_shift, "officer": person})
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action="shift.exchange_agreed",
        target_type="shift_exchange", target_id=str(exchange.pk),
        metadata={"initiator_shift": str(exchange.initiator_shift_id), "partner_shift": str(exchange.partner_shift_id)})
    messages.success(request, "Trade agreed — a manager approves both halves together.")
    return redirect("my_shifts")


@require_POST
@membership_required()
def exchange_withdraw(request, exchange_id):
    person = _linked_person(request)
    exchange = ShiftExchange.objects.filter(pk=exchange_id, organization=request.organization,
        initiator=person, status__in=EXCHANGE_OPEN).select_related("partner", "initiator_shift").first() if person else None
    if not exchange:
        raise Http404
    exchange.status = ShiftExchange.Status.WITHDRAWN
    exchange.decided_by = request.user
    exchange.decided_at = timezone.now()
    exchange.save(update_fields=["status", "decided_by", "decided_at"])
    queue_notice(organization=request.organization, recipients={exchange.partner.user_id} - {None},
        event_type="shift.exchange_withdrawn", subject="A trade offer was withdrawn",
        body=f"{exchange.initiator.full_name} no longer wants to trade {exchange.initiator_shift.site} "
             f"{shift_when(exchange.initiator_shift)}. Nothing has moved.",
        dedup_key=f"shift-exchange-withdrawn:{exchange.pk}",
        sms={"shift": exchange.initiator_shift, "officer": exchange.initiator})
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action="shift.exchange_withdrawn",
        target_type="shift_exchange", target_id=str(exchange.pk))
    messages.success(request, "Trade withdrawn.")
    return redirect("my_shifts")


@require_POST
@membership_required(*MANAGERS)
@transaction.atomic
def exchange_decide(request, exchange_id):
    """Decide the pair once, or not at all.

    This is the whole reason an exchange is its own object: approving one leg of a two-way trade
    releases an officer from a post while nobody has taken the other, which is a coverage hole the
    schedule cannot show as anything but filled. So both legs are re-checked inside one transaction —
    each officer still holds the post they are giving up, neither post has started, and **each is
    qualified for the other** — and either both assignments move or nothing does.

    A manager who is one of the two officers cannot decide their own trade (When I Work's queue
    lists "Requests I Can Approve… excluding your personal requests"), because then the one
    independent consent in the chain is the one that is missing.
    """
    exchange = ShiftExchange.objects.select_for_update().select_related(
        "initiator_shift__site__client", "partner_shift__site__client", "initiator", "partner").filter(
        pk=exchange_id, organization=request.organization, status__in=EXCHANGE_OPEN).first()
    if not exchange:
        raise Http404
    scope = scope_for(request)
    if not scope.permits_shift(exchange.initiator_shift) or not scope.permits_shift(exchange.partner_shift):
        raise Http404
    person = _linked_person(request)
    action = request.POST.get("action")
    if action not in (ShiftExchange.Status.APPROVED, ShiftExchange.Status.REFUSED):
        messages.error(request, "Approve or refuse the trade.")
        return redirect("swaps")
    if action == ShiftExchange.Status.APPROVED and exchange.parties & ({person.pk} if person else set()):
        messages.error(request, "You cannot approve a trade you are a party to. Ask another manager or a dispatcher.")
        return redirect("swaps")
    note = request.POST.get("note", "").strip()[:255]
    exchange.review_note = note
    if action == ShiftExchange.Status.APPROVED:
        if exchange.status != ShiftExchange.Status.AGREED:
            messages.error(request, "The colleague has not chosen a post to trade yet.")
            return redirect("swaps")
        now = timezone.now()
        for post, giver, taker in exchange.legs:
            if post.officer_id != giver.pk:
                exchange.status = ShiftExchange.Status.REFUSED
                exchange.review_note = f"{giver.full_name} no longer holds {post.site}."
                _decide_exchange(exchange, request, notify=True)
                messages.error(request, f"That trade is closed — {giver.full_name} no longer holds {post.site}.")
                return redirect("swaps")
            if post.starts_at <= now:
                messages.error(request, f"{post.site} has already started — correct the timecard instead.")
                return redirect("swaps")
        for post, giver, taker in exchange.legs:
            allowed, reasons = shift_eligibility(post, officer=taker)
            if not allowed:
                messages.error(request, f"Cannot approve: {taker.full_name} — " + " ".join(reasons))
                return redirect("swaps")
        for post, giver, taker in exchange.legs:
            post.officer = taker
            post.save(update_fields=["officer", "updated_at"])
        exchange.status = ShiftExchange.Status.APPROVED
        for stale in ShiftSwap.objects.filter(Q(shift=exchange.initiator_shift) | Q(shift=exchange.partner_shift),
                status__in=SWAP_OPEN):
            stale.status = ShiftSwap.Status.REFUSED
            stale.review_note = "An approved trade moved this post."
            _decide_swap(stale, request, notify=True)
    else:
        exchange.status = ShiftExchange.Status.REFUSED
    _decide_exchange(exchange, request)
    stamp = timezone.now().strftime("%Y%m%d%H%M")
    body = (f"{exchange.initiator.full_name} now stands {exchange.partner_shift.site} on "
            f"{shift_when(exchange.partner_shift)}, and {exchange.partner.full_name} stands "
            f"{exchange.initiator_shift.site} on {shift_when(exchange.initiator_shift)}."
            if action == ShiftExchange.Status.APPROVED else
            f"The trade of {exchange.initiator_shift.site} and {exchange.partner_shift.site} was not approved."
            + (f" Reason: {note}" if note else " Both officers keep their current posts."))
    for officer, other in ((exchange.initiator, exchange.partner), (exchange.partner, exchange.initiator)):
        queue_notice(organization=request.organization, recipients={officer.user_id} - {None},
            event_type=f"shift.exchange_{exchange.status}",
            subject=("Your trade is approved" if action == ShiftExchange.Status.APPROVED else "Trade refused"),
            body=body + (" Your punches for time already worked stay on your own timecard."
                         if action == ShiftExchange.Status.APPROVED else ""),
            dedup_key=f"shift-exchange-{exchange.status}:{exchange.pk}:{stamp}",
            sms={"shift": exchange.initiator_shift, "officer": officer, "other": other, "note": note})
    AuditEvent.objects.create(organization=request.organization, actor=request.user,
        action=f"shift.exchange_{exchange.status}", target_type="shift_exchange", target_id=str(exchange.pk),
        metadata={"initiator_shift": str(exchange.initiator_shift_id), "partner_shift": str(exchange.partner_shift_id),
                  "initiator": str(exchange.initiator_id), "partner": str(exchange.partner_id), "note": note})
    messages.success(request, "Both posts moved together — the trade is approved."
        if action == ShiftExchange.Status.APPROVED else "Trade refused; both officers keep their posts.")
    return redirect("swaps")


def _decide_exchange(exchange, request, notify=False):
    exchange.decided_by = request.user
    exchange.decided_at = timezone.now()
    exchange.save(update_fields=["status", "review_note", "decided_by", "decided_at"])
    if notify:
        queue_notice(organization=exchange.organization, recipients=exchange.involved_user_ids(),
            event_type=f"shift.exchange_{exchange.status}", subject="A trade offer closed",
            body=f"{exchange.review_note} No action needed from you.",
            dedup_key=f"shift-exchange-closed:{exchange.pk}",
            sms={"notice": "shift.exchange_closed", "note": exchange.review_note})


@membership_required(*MANAGERS)
def swaps(request):
    """The approval queue for both kinds of move: a handoff, and a two-way trade.

    One queue, because a manager decides both with the same posture — is this post going to be stood
    by somebody qualified — and two surfaces is how half a trade gets approved while the other half
    sits unseen. Open offers are few by nature and each needs its eligibility re-read; the decided
    list is history and is only ever scrolled.
    """
    scope = scope_for(request)
    now = timezone.now()
    pending = _open_swaps(request.organization, scope)
    selected_swap = request.GET.get("swap")
    selected_exchange = request.GET.get("exchange")
    if selected_swap:
        pending = [row for row in pending if str(row.pk) == selected_swap]
        if not pending:
            raise Http404
    for row in pending:
        # A manager weighing a move has to see whether the colleague can actually stand the post
        # now, whether the officer who offered it still holds it, and whether anyone has already
        # punched — their recorded time stays theirs however the assignment ends up moving.
        row.eligible, row.reasons = shift_eligibility(row.shift, officer=row.replacement)
        row.punch_count = row.shift.punches.count()
        row.still_offered_by = row.shift.officer_id == row.requester_id
        row.has_run = row.shift.ends_at <= now
        row.impact = assignment_impact(request.organization, [(row.shift, row.requester, row.replacement)])
    exchanges = _open_exchanges(request.organization, scope)
    if selected_exchange:
        exchanges = [row for row in exchanges if str(row.pk) == selected_exchange]
        if not exchanges:
            raise Http404
    if selected_swap:
        exchanges = []
    elif selected_exchange:
        pending = []
    for row in exchanges:
        if row.partner_shift_id:
            # Both directions are shown because both are refused if either fails; the manager is
            # approving a pair, not a preference.
            row.forward_eligible, row.forward_reasons = shift_eligibility(row.initiator_shift, officer=row.partner)
            row.backward_eligible, row.backward_reasons = shift_eligibility(row.partner_shift, officer=row.initiator)
            row.impact = assignment_impact(request.organization, row.legs)
        else:
            row.forward_eligible, row.forward_reasons = None, []
            row.backward_eligible, row.backward_reasons = None, []
            row.impact = []
        row.still_held = row.initiator_shift.officer_id == row.initiator_id
        row.has_run = row.initiator_shift.starts_at <= now
    decided = [row for row in ShiftSwap.objects.filter(organization=request.organization)
        .exclude(status__in=SWAP_OPEN).select_related("shift__site", "requester", "replacement", "decided_by")
        .order_by("-decided_at")[:DECIDED_WINDOW] if scope.permits_shift(row.shift)]
    decided_exchanges = [row for row in ShiftExchange.objects.filter(organization=request.organization)
        .exclude(status__in=EXCHANGE_OPEN).select_related("initiator_shift__site", "partner_shift__site",
            "initiator", "partner", "decided_by").order_by("-decided_at")[:DECIDED_WINDOW]
        if scope.permits_shift(row.initiator_shift)]
    return render(request, "core/swaps.html", {"pending": pending, "decided": decided,
        "exchanges": exchanges, "decided_exchanges": decided_exchanges,
        "authority_scope": scope if scope.restricted else None})


@require_POST
@membership_required(*MANAGERS)
@transaction.atomic
def swap_decide(request, swap_id):
    """Move the assignment, after re-checking everything that mattered when the offer was made.

    Three things can have changed since the colleague accepted: their credentials, the post's
    assignment (a dispatcher may have filled it another way), and whether the post has run at all.
    All three are checked here rather than trusted from the earlier step, for the same reason
    ``shift_claim_decide`` re-runs eligibility — an expired registration is what a Texas post cannot
    absorb, and the move is the moment the post stops being covered by the person who was stood on it.
    """
    swap = ShiftSwap.objects.select_for_update().select_related("shift__site__client", "requester", "replacement").filter(
        pk=swap_id, organization=request.organization, status__in=SWAP_OPEN).first()
    if not swap:
        raise Http404
    if not scope_for(request).permits_shift(swap.shift):
        raise Http404
    action = request.POST.get("action")
    if action not in (ShiftSwap.Status.APPROVED, ShiftSwap.Status.REFUSED):
        messages.error(request, "Approve or refuse the swap.")
        return redirect("swaps")
    # A supervisor who is also a guard can stand in this queue. They may not approve a move that
    # names them: the manager's decision is the one independent consent in the chain, and spending
    # it on yourself is how a handoff becomes self-assignment (When I Work excludes a supervisor's
    # own requests from the list they can act on for exactly this reason).
    actor = _linked_person(request)
    if action == ShiftSwap.Status.APPROVED and actor is not None and actor.pk in swap.parties:
        messages.error(request, "You cannot approve an offer you are a party to. Ask another manager.")
        return redirect("swaps")
    note = request.POST.get("note", "").strip()[:255]
    shift = swap.shift
    swap.review_note = note
    if action == ShiftSwap.Status.APPROVED:
        if swap.status != ShiftSwap.Status.AGREED:
            messages.error(request, "The colleague has not accepted this offer yet.")
            return redirect("swaps")
        if shift.officer_id != swap.requester_id:
            swap.status = ShiftSwap.Status.REFUSED
            swap.review_note = "The post was reassigned before this was approved."
            _decide_swap(swap, request)
            messages.error(request, "That post no longer belongs to the officer who offered it.")
            return redirect("swaps")
        if shift.ends_at <= timezone.now():
            messages.error(request, "That post has already ended — correct the timecard instead.")
            return redirect("swaps")
        shift.officer = swap.replacement
        allowed, reasons = shift_eligibility(shift)
        if not allowed:
            shift.officer = swap.requester
            messages.error(request, "Cannot approve: " + " ".join(reasons))
            return redirect("swaps")
        shift.save(update_fields=["officer", "updated_at"])
        swap.status = ShiftSwap.Status.APPROVED
        # A second open offer on the same post is now moot; leaving it live would keep asking a
        # manager to decide something that has already been decided.
        for sibling in ShiftSwap.objects.filter(shift=shift, status__in=SWAP_OPEN).exclude(pk=swap.pk):
            sibling.status = ShiftSwap.Status.REFUSED
            sibling.review_note = "Another offer for this post was approved."
            _decide_swap(sibling, request, notify=True)
    else:
        swap.status = ShiftSwap.Status.REFUSED
    _decide_swap(swap, request)
    stamp = timezone.now().strftime("%Y%m%d%H%M")
    if action == ShiftSwap.Status.APPROVED:
        queue_notice(organization=request.organization, recipients={swap.replacement.user_id} - {None},
            event_type="shift.swap_approved", subject="You're scheduled — " + str(shift.site),
            body=f"{shift.site} on {shift_when(shift)} is yours."
                 + (f" Note: {note}" if note else ""),
            dedup_key=f"shift-swap-approved:{swap.pk}:{stamp}",
            sms={"shift": shift, "note": note})
        queue_notice(organization=request.organization, recipients={swap.requester.user_id} - {None},
            event_type="shift.swap_approved", subject="Your swap was approved",
            body=f"{swap.replacement.full_name} is standing {shift.site} on {shift_when(shift)}."
                 + (" Your punches for time already worked stay on your timecard." if Punch.objects.filter(shift=shift, person=swap.requester).exists() else ""),
            dedup_key=f"shift-swap-approved-requester:{swap.pk}:{stamp}",
            sms={"notice": "shift.swap_approved.requester", "shift": shift, "officer": swap.replacement})
        for advisory in shift_advisories(shift):
            messages.warning(request, advisory)
    else:
        queue_notice(organization=request.organization,
            recipients={swap.requester.user_id, swap.replacement.user_id} - {None},
            event_type="shift.swap_refused", subject="Swap refused — " + str(shift.site),
            body=f"The swap of {shift.site} on {shift_when(shift)} was not approved."
                 + (f" Reason: {note}" if note else " You are still scheduled."),
            dedup_key=f"shift-swap-refused:{swap.pk}:{stamp}",
            sms={"shift": shift, "note": note or "You're still scheduled."})
    AuditEvent.objects.create(organization=request.organization, actor=request.user,
        action=f"shift.swap_{swap.status}", target_type="shift_swap", target_id=str(swap.pk),
        metadata={"shift": str(shift.pk), "requester": str(swap.requester_id),
                  "replacement": str(swap.replacement_id), "note": note})
    messages.success(request, "Swap approved — the post now belongs to " + swap.replacement.full_name + "."
        if action == ShiftSwap.Status.APPROVED else "Swap refused; both officers keep their current assignment.")
    return redirect("swaps")


def _decide_swap(swap, request, notify=False):
    swap.decided_by = request.user
    swap.decided_at = timezone.now()
    swap.save(update_fields=["status", "review_note", "decided_by", "decided_at"])
    if notify and swap.replacement.user_id:
        queue_notice(organization=swap.organization, recipients={swap.replacement.user_id},
            event_type="shift.swap_refused", subject="A swap offer closed",
            body=f"{swap.review_note} No action needed from you.",
            dedup_key=f"shift-swap-closed:{swap.pk}",
            sms={"notice": "shift.swap_closed", "shift": swap.shift, "note": swap.review_note})


def _time_off_managers(organization, person):
    """Who decides a leave request: the company roles plus the managers covering this person.

    The same list the reminders use, so the person who is told a registration lapsed is also the
    person who is asked to approve the week off that follows it.
    """
    from .scope import manager_recipients_by_person
    return compliance_recipients(organization, person, managers=manager_recipients_by_person(organization).get(person.pk, ()))


def colliding_posts(request):
    """The live posts an absence overlaps — the hole the approver is being asked to accept."""
    return list(request.organization.shifts.filter(
        officer=request.person, starts_at__lt=request.ends_at, ends_at__gt=request.starts_at,
    ).exclude(status=Shift.Status.CANCELLED).select_related("site__client").order_by("starts_at"))


@membership_required()
def availability(request, person_id=None):
    """The hours an officer is willing to stand, stated by the officer.

    Managers reach it through the profile and the officer through the sidebar. Availability is
    advisory at assignment (``services.shift_advisories``) rather than a gate, so it has to be
    correctable by the person it describes — a pattern nobody can fix is a pattern nobody trusts.
    """
    if person_id:
        person = _profile_person(request, person_id)
        if request.membership.role not in MANAGERS:
            raise Http404
    else:
        # No personnel record is a state the clock and open posts already explain rather than
        # 404 on: the sign-in exists, the file does not, and the officer cannot fix that here.
        person = request.organization.people.filter(user=request.user).first()
        if not person:
            return render(request, "core/availability.html", {"person": None, "rules": [], "form": None, "managed": False, "return_url": None})
    form = AvailabilityRuleForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        rule = form.save(commit=False)
        rule.organization = request.organization
        rule.person = person
        try:
            rule.full_clean(); rule.save()
        except ValidationError as exc:
            for error in exc.messages:
                form.add_error(None, error)
        else:
            AuditEvent.objects.create(organization=request.organization, actor=request.user, action="availability.added", target_type="availability_rule", target_id=str(rule.pk), metadata={"person": str(person.pk), "window": str(rule)})
            messages.success(request, f"Availability recorded: {rule}.")
            return redirect("person_availability" if person_id else "availability")
    return render(request, "core/availability.html", {
        "person": person, "rules": person.availability_rules.all(), "form": form,
        "managed": bool(person_id), "return_url": reverse("person_detail", args=[person.pk]) if person_id else None,
    })


@require_POST
@membership_required()
def availability_remove(request, rule_id):
    rule = AvailabilityRule.objects.filter(pk=rule_id, organization=request.organization).select_related("person").first()
    if not rule:
        raise Http404
    own = rule.person.user_id == request.user.id
    if not own and (request.membership.role not in MANAGERS or not scope_for(request).permits_person(rule.person)):
        raise Http404
    rule.delete()
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action="availability.removed", target_type="availability_rule", target_id=str(rule.pk), metadata={"person": str(rule.person_id), "window": str(rule)})
    messages.success(request, f"Removed {rule} from the stated availability.")
    if own:
        return redirect("availability")
    return redirect("person_availability", rule.person_id)


@membership_required()
@transaction.atomic
def my_time_off(request):
    """An officer's own leave requests: ask, see the decision, withdraw while it is open."""
    person = request.organization.people.filter(user=request.user).first()
    if not person:
        return render(request, "core/my_time_off.html", {"person": None, "form": None, "requests": [], "collisions": 0})
    form = TimeOffRequestForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.organization = request.organization; item.person = person; item.requested_by = request.user
        item.full_clean(); item.save()
        queue_notice(organization=request.organization, recipients=_time_off_managers(request.organization, person),
                     event_type="timeoff.requested",
                     subject=f"{person.full_name} asked for time off",
                     body=f"{person.full_name} requested {date_span(request.organization, item.starts_at, item.ends_at)} off." + (f" Reason: {item.reason}" if item.reason else ""),
                     dedup_key=f"timeoff-requested:{item.pk}",
                     sms={"officer": person, "dates": (item.starts_at, item.ends_at), "note": item.reason})
        AuditEvent.objects.create(organization=request.organization, actor=request.user, action="timeoff.requested", target_type="time_off_request", target_id=str(item.pk), metadata={"person": str(person.pk), "starts_at": item.starts_at.isoformat(), "ends_at": item.ends_at.isoformat()})
        messages.success(request, "Time off requested. A manager will decide it.")
        return redirect("my_time_off")
    collisions = []
    for item in person.time_off_requests.filter(status=TimeOffRequest.Status.REQUESTED):
        collisions.extend(colliding_posts(item))
    return render(request, "core/my_time_off.html", {
        "person": person, "form": form, "requests": person.time_off_requests.all(),
        "collisions": len(collisions),
    })


@require_POST
@membership_required()
def my_time_off_cancel(request, request_id):
    item = TimeOffRequest.objects.filter(pk=request_id, organization=request.organization,
                                         person__user=request.user, status=TimeOffRequest.Status.REQUESTED).first()
    if not item:
        raise Http404
    item.status = TimeOffRequest.Status.CANCELLED
    item.decided_by = request.user; item.decided_at = timezone.now()
    item.save(update_fields=["status", "decided_by", "decided_at"])
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action="timeoff.cancelled", target_type="time_off_request", target_id=str(item.pk), metadata={"person": str(item.person_id)})
    messages.success(request, "Request withdrawn.")
    return redirect("my_time_off")


@membership_required(*MANAGERS)
def time_off(request):
    """The leave queue, bounded by the actor's authority like every other surface."""
    scope = scope_for(request)
    show = request.GET.get("show", "open")
    items = scope.filter_by_person(request.organization.time_off_requests.select_related("person"))
    if request.GET.get("request"):
        try:
            request_id = uuid.UUID(request.GET["request"])
        except ValueError:
            raise Http404
        selected = items.filter(pk=request_id).first()
        if selected is None:
            raise Http404
        items = items.filter(pk=selected.pk)
        show = "open" if selected.status == TimeOffRequest.Status.REQUESTED else "decided"
    if show == "decided":
        items = items.exclude(status=TimeOffRequest.Status.REQUESTED)
    else:
        items = items.filter(status=TimeOffRequest.Status.REQUESTED)
    rows = []
    for item in items.order_by("starts_at"):
        rows.append({"request": item, "collisions": colliding_posts(item)})
    return render(request, "core/time_off.html", {
        "rows": rows, "show": show, "decided_count": scope.filter_by_person(request.organization.time_off_requests).exclude(status=TimeOffRequest.Status.REQUESTED).count(),
        "authority_scope": scope if scope.restricted else None,
    })


@require_POST
@membership_required(*MANAGERS)
@transaction.atomic
def time_off_decide(request, request_id):
    """Approve or decline a leave request, with the coverage hole named either way.

    Approval does not silently cancel the posts it overlaps: those are the dispatcher's to move,
    and an approval that erased a published post would take coverage away from a client without
    an audit trail anyone chose.
    """
    item = TimeOffRequest.objects.select_for_update().select_related("person").filter(
        pk=request_id, organization=request.organization, status=TimeOffRequest.Status.REQUESTED).first()
    if not item:
        raise Http404
    if not scope_for(request).permits_person(item.person):
        raise Http404
    action = request.POST.get("action")
    if action not in (TimeOffRequest.Status.APPROVED, TimeOffRequest.Status.DECLINED):
        messages.error(request, "Approve or decline the request.")
        return redirect("time_off")
    note = request.POST.get("note", "").strip()
    item.status = action
    item.decided_by = request.user; item.decided_at = timezone.now(); item.review_note = note[:255]
    item.save(update_fields=["status", "decided_by", "decided_at", "review_note"])
    stamp = timezone.now().strftime("%Y%m%d%H%M")
    queue_notice(organization=request.organization, recipients=({item.person.user_id} if item.person.user_id else set()),
                 event_type=f"timeoff.{action}",
                 subject=("Time off approved — " if action == TimeOffRequest.Status.APPROVED else "Time off declined — ") + item.person.full_name,
                 body=(f"Your request for {date_span(request.organization, item.starts_at, item.ends_at)} was {action}."
                       + (f" Note: {note}" if note else "")),
                 dedup_key=f"timeoff-decision:{item.pk}:{stamp}",
                 sms={"dates": (item.starts_at, item.ends_at), "note": note})
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action=f"timeoff.{action}", target_type="time_off_request", target_id=str(item.pk), metadata={"person": str(item.person_id), "note": note, "starts_at": item.starts_at.isoformat(), "ends_at": item.ends_at.isoformat()})
    collisions = colliding_posts(item) if action == TimeOffRequest.Status.APPROVED else []
    if collisions:
        messages.warning(request, (f"Approved, but {len(collisions)} live post(s) fall inside it: "
                                   + "; ".join(f"{post.site} {post.starts_at:%a %H:%M}" for post in collisions[:3])
                                   + ". Move or cancel them, or the officer is stood on a post they were granted off."))
    else:
        messages.success(request, "Request approved." if action == TimeOffRequest.Status.APPROVED else "Request declined.")
    return redirect("time_off")


@membership_required()
def clock(request):
    person=request.organization.people.filter(user=request.user).first()
    now=timezone.now()
    shifts=person.shifts.filter(starts_at__lte=now+timedelta(hours=12),ends_at__gte=now-timedelta(hours=12)).select_related("site__client") if person else []
    checkpoints=Checkpoint.objects.filter(organization=request.organization,active=True,site__active=True).select_related("site","site__client").order_by("site__client__name","site__name","name") if person else Checkpoint.objects.none()
    recent=person.punches.select_related("shift__site","checkpoint")[:8] if person else []
    # CLK-2. The clock and the station share one credential, so the page that can use the station has
    # to say whether the officer can yet. A blank PIN with no pointer is a dead end at the guard shack,
    # and "ask someone" is the friction this whole feature exists to remove.
    kiosks=request.organization.clock_kiosks.filter(active=True).order_by("name")
    # CLK-1. Whether to open a camera is decided per post, not per company: the rule resolves along
    # site → contract → company, so a contract that waived the photo must not point a lens at an
    # officer standing a post it covers. Resolved once per distinct site rather than once per shift,
    # and keyed as a string because the template compares it against `shift.site_id`.
    selfie_sites = {str(site.pk): effective_clock_policy(request.organization, site).selfie["required"]
                    for site in {shift.site for shift in shifts if shift.site_id}}
    return render(request,"core/clock.html",{"person":person,"shifts":shifts,"checkpoints":checkpoints,"recent":recent,
        "has_pin":bool(person and person.clock_pin),"kiosks":kiosks,
        "selfie_sites":selfie_sites,"selfie_sites_json":json.dumps(selfie_sites),
        "selfie_endpoint":reverse("clock_selfie_upload"),
        "any_selfie":any(selfie_sites.values())})

@require_POST
@membership_required()
def clock_device_enroll(request):
    person=request.organization.people.filter(user=request.user).first()
    if not person:return JsonResponse({"error":"Your login is not linked to a personnel record."},status=403)
    device=OfflineClockDevice.objects.create(organization=request.organization,person=person,user=request.user,label=request.headers.get("User-Agent","")[:120])
    token=signing.dumps({"device":str(device.pk),"organization":str(request.organization.pk),"user":request.user.pk},salt="offline-clock")
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="clock_device.enrolled",target_type="offline_clock_device",target_id=str(device.pk))
    return JsonResponse({"device_id":str(device.pk),"token":token,"sequence":0})

# CLK-1's own imports, in the same style as the messaging block below: the selfie surface reads as one
# section, and what it touches is what it imports.
from .services import store_clock_selfie

def _selfie_claim(organization, data):
    """Look up the frame the page says it just took, without judging it yet.

    Scoped to the tenant here and checked for belonging everywhere else: an id that is not this
    company's is *not found*, not "forbidden", because telling a guesser that a document exists in
    somebody else's personnel file is a disclosure of its own. `services.resolve_clock_selfie` then
    answers whose face, what kind of record, how old and already used — the difference between
    reading an id and accepting a claim about it.
    """
    document_id = data.get("selfie_document_id")
    if not document_id:
        return None
    document = organization.person_documents.filter(pk=document_id).first()
    if document is None:
        raise ValidationError("That photo was not found in this organization.")
    return document


# CLK-1's upload route. The frame is filed one round trip *before* the punch, so the clock event can
# carry its own evidence id and there is no interval in which a punch exists while its photo is still in
# transit — and no path where a refused upload leaves a silent, photo-less punch behind. Deliberately
# one route and not two: a station punch is exempt from the requirement by the owner's own clause, so a
# kiosk camera would be asking for evidence the PIN already supplied.
@require_POST
@membership_required()
def clock_selfie_upload(request):
    """The officer's own device: a frame for the punch it is about to make."""
    person = request.organization.people.filter(user=request.user).first()
    if not person:
        return JsonResponse({"error": "Your login is not linked to a personnel record."}, status=403)
    try:
        document = store_clock_selfie(organization=request.organization, person=person,
                                      upload=request.FILES.get("selfie"), actor=request.user,
                                      replaces=request.POST.get("replaces") or None)
    except ValidationError as exc:
        return JsonResponse({"error": exc.messages[0]}, status=400)
    return JsonResponse({"document_id": str(document.pk)}, status=201)


def _clock_targets(organization, data):
    """Resolve the tenant-scoped shift/site a punch claims to belong to."""
    shift=None
    if data.get("shift_id"):
        shift=organization.shifts.select_related("site").filter(pk=data["shift_id"]).first()
        if not shift: raise ValidationError("Shift was not found in this organization.")
    site=None
    if data.get("site_id"):
        site=organization.sites.filter(pk=data["site_id"],active=True).first()
        if not site: raise ValidationError("Site was not found in this organization.")
    if shift and site and shift.site_id!=site.pk: raise ValidationError("Site does not match the shift.")
    return shift,site

def _location_claims(data):
    """The numbers a clock page sends about *where* the officer was, read the same way on every path.

    Three routes take the same payload (the officer's own device, an offline queue, a shared station)
    and a fourth will. One reader means one answer to "what does a missing or unparseable accuracy
    mean", and the answer matters: an absent reading must stay `None`, because a `0` would be read by
    CLK-4 as the most suspicious value a phone can report. A page that never sends the field must not
    turn every officer into a suspect.
    """
    def number(name):
        raw = data.get(name)
        if raw is None or raw == "":
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    return {"latitude": number("latitude"), "longitude": number("longitude"),
            "accuracy_m": number("accuracy"), "fix_age_seconds": number("fix_age_seconds")}

def _resolve_checkpoint(organization, data, shift):
    """Attribute a checkpoint punch to a patrol point, or refuse the claim.

    A checkpoint scan that cannot name a checkpoint proves a button was pressed, not that
    anyone was anywhere, so the same rules guard the online and offline paths: it must be a
    checkpoint punch, on the shift's own site, inside the point's geofence when the point has
    coordinates.
    """
    if not data.get("checkpoint_code"):
        if data.get("kind")==Punch.Kind.CHECKPOINT:
            raise ValidationError("A checkpoint scan must name the patrol point that was reached.")
        return None
    checkpoint=Checkpoint.objects.select_related("site").filter(organization=organization,scan_code=data["checkpoint_code"],active=True).first()
    if not checkpoint: raise ValidationError("Checkpoint code was not found in this organization.")
    if data.get("kind")!=Punch.Kind.CHECKPOINT: raise ValidationError("A checkpoint code requires a checkpoint punch.")
    if not shift or shift.site_id!=checkpoint.site_id: raise ValidationError("Checkpoint does not belong to the selected shift site.")
    if checkpoint.latitude is not None:
        if data.get("latitude") is None or data.get("longitude") is None: raise ValidationError("Checkpoint location is required.")
        if haversine_meters(data["latitude"],data["longitude"],checkpoint.latitude,checkpoint.longitude)>checkpoint.radius_meters:
            raise ValidationError("Checkpoint scan was outside its geofence.")
    return checkpoint

# Device-token authenticated (see clock_device_enroll); the signed tenant/user/device
# credential, not the browser session, authorizes this request, so it stays CSRF-exempt.
@csrf_exempt
@require_POST
@transaction.atomic
def offline_punch_sync(request):
    try:
        data=json.loads(request.body); claims=signing.loads(data.pop("device_token"),salt="offline-clock",max_age=60*60*24*30)
        device=OfflineClockDevice.objects.select_for_update().select_related("organization","person","user").get(pk=claims["device"],organization_id=claims["organization"],user_id=claims["user"],active=True)
        sequence=int(data["device_sequence"])
        existing=Punch.objects.filter(client_event_id=data.get("client_event_id"),organization=device.organization,person=device.person,device_id=device.id,device_sequence=sequence).first()
        if existing:return JsonResponse({"id":str(existing.pk),"created":False,"review_status":existing.review_status,"exception":existing.exception_reason})
        if sequence<=device.last_sequence: raise ValidationError("This device sequence was already processed.")
        shift,site=_clock_targets(device.organization,data)
        checkpoint=_resolve_checkpoint(device.organization,data,shift)
        occurred_at=parse_punch_timestamp(data.get("occurred_at"))
        punch,created=record_punch(organization=device.organization,person=device.person,shift=shift,site=site,client_event_id=uuid.UUID(data["client_event_id"]),kind=data["kind"],occurred_at=occurred_at,offline=bool(data.get("offline")),source="offline-pwa",actor=device.user,checkpoint=checkpoint,**_location_claims(data),selfie=_selfie_claim(device.organization,data))
        if created:
            punch.device_id=device.id;punch.device_sequence=sequence;punch.save(update_fields=["device_id","device_sequence"])
        device.last_sequence=sequence;device.last_seen_at=timezone.now();device.save(update_fields=["last_sequence","last_seen_at"])
        return JsonResponse({"id":str(punch.pk),"created":created,"review_status":punch.review_status,"exception":punch.exception_reason},status=201 if created else 200)
    except (KeyError,ValueError,ValidationError,signing.BadSignature,OfflineClockDevice.DoesNotExist,Shift.DoesNotExist,Checkpoint.DoesNotExist) as exc:
        return JsonResponse({"error":str(exc)},status=400)

@require_POST
@membership_required()
def punch_api(request):
    person=request.organization.people.filter(user=request.user).first()
    if not person: return JsonResponse({"error":"Your login is not linked to a personnel record."},status=403)
    try:
        data=json.loads(request.body)
        shift,site=_clock_targets(request.organization,data)
        checkpoint=_resolve_checkpoint(request.organization,data,shift)
        occurred_at=parse_punch_timestamp(data.get("occurred_at"))
        punch,created=record_punch(organization=request.organization,person=person,shift=shift,site=site,client_event_id=uuid.UUID(data["client_event_id"]),kind=data["kind"],occurred_at=occurred_at,offline=bool(data.get("offline")),source="pwa",actor=request.user,checkpoint=checkpoint,**_location_claims(data),selfie=_selfie_claim(request.organization,data))
        return JsonResponse({"id":str(punch.pk),"created":created,"review_status":punch.review_status,"exception":punch.exception_reason,"checkpoint":str(checkpoint.pk) if checkpoint else None},status=201 if created else 200)
    except (KeyError,ValueError,ValidationError) as exc:
        message=exc.messages[0] if isinstance(exc,ValidationError) else str(exc)
        return JsonResponse({"error":message},status=400)

# ── CLK-2: the shared clock station ──────────────────────────────────────────────
#
# None of the four routes below has a signed-in user, and that is the feature. A guard shack tablet is
# used by people who may not have accepted a sign-in invitation at all, and a clock that requires an
# account is a clock that gets worked around with a shared login — which destroys the one thing
# timekeeping evidence is for. Identity comes from the PIN, provenance from the station, and both are
# checked against the resolved clock policy on every request rather than once at enrolment.

def _kiosk_or_404(kiosk_id):
    """Look a station up by its random UUID.

    The uuid is what makes the page unguessable; it is not what makes a punch, and it is not revoked
    by expiring. ``active`` is read from the database on every request, so retiring a station stops it
    mid-keystroke instead of at the end of a token's life.
    """
    kiosk = ClockKiosk.objects.select_related("organization", "site", "site__client").filter(pk=kiosk_id).first()
    if not kiosk:
        raise Http404
    return kiosk

def clock_kiosk(request, kiosk_id):
    """The pad an officer stands in front of. No navigation, no roster, no name on screen."""
    kiosk = _kiosk_or_404(kiosk_id)
    if not kiosk.active:
        return render(request, "core/kiosk.html", {"kiosk": kiosk, "closed": True})
    response = render(request, "core/kiosk.html", {
        "kiosk": kiosk, "closed": False,
        "organization_name": kiosk.organization.display_name,
        "where": str(kiosk.site) if kiosk.site_id else "this company",
    })
    # The page is a shell, but it is a shell that has already named a company and a post, and a shared
    # browser is the one place a cached page outlives the person who loaded it.
    response.headers["Cache-Control"] = "private, no-store"
    return response

@require_POST
def clock_kiosk_identify(request, kiosk_id):
    """Trade typed digits for a ninety-second session, and hand back the posts it unlocks.

    Not in a transaction on purpose. A refusal has to survive the raise, and the station's failure
    counter is the only record that guessing was attempted — see ``register_kiosk_pin_failure``.
    """
    kiosk = _kiosk_or_404(kiosk_id)
    if not kiosk.active:
        return JsonResponse({"error": "This clock station has been retired. Use the one your supervisor points you to."}, status=410)
    try:
        data = json.loads(request.body)
    except ValueError:
        return JsonResponse({"error": "Send the PIN as JSON."}, status=400)
    refusal = kiosk_policy_refusal(kiosk.organization, kiosk.site)
    if refusal:
        return JsonResponse({"error": refusal}, status=403)
    try:
        person = find_person_by_pin(kiosk.organization, data.get("pin"), kiosk=kiosk)
    except ValidationError as exc:
        return JsonResponse({"error": exc.messages[0]}, status=401)
    offers, recommended = kiosk_shifts(person)
    return JsonResponse({"identity": kiosk_identity_token(kiosk, person), "first_name": person.first_name,
                         "name": person.full_name, "shifts": offers, "recommended": recommended,
                         "expires_in": KIOSK_IDENTITY_SECONDS, "station": kiosk.name})

@require_POST
@transaction.atomic
def clock_kiosk_punch(request, kiosk_id):
    """Record the punch a verified officer asks for, at the station they are standing at."""
    kiosk = _kiosk_or_404(kiosk_id)
    if not kiosk.active:
        return JsonResponse({"error": "This clock station has been retired."}, status=410)
    try:
        data = json.loads(request.body)
    except ValueError:
        return JsonResponse({"error": "Send the punch as JSON."}, status=400)
    try:
        person = kiosk_identity(data.get("identity"), kiosk)
        shift, _ = _clock_targets(kiosk.organization, data)
        # Re-checked here, not only at enrolment: the contract that forbids a shared clock on Tuesday
        # has to stop the station on Tuesday. The post the punch is *for* governs, because that is the
        # post the client is billed against; a station with no post of its own falls back to company.
        gate_site = (shift.site if shift is not None and shift.site_id else None) or (kiosk.site if kiosk.site_id else None)
        refusal = kiosk_policy_refusal(kiosk.organization, gate_site)
        if refusal:
            raise ValidationError(refusal)
        checkpoint = _resolve_checkpoint(kiosk.organization, data, shift)
        occurred_at = parse_punch_timestamp(data.get("occurred_at"))
        punch, created = record_punch(organization=kiosk.organization, person=person, shift=shift,
            site=None if shift else kiosk.site, client_event_id=uuid.UUID(data["client_event_id"]),
            kind=data["kind"], occurred_at=occurred_at,
            offline=False, source="kiosk", actor=person.user, checkpoint=checkpoint,
            evidence={"kiosk": str(kiosk.pk), "kiosk_name": kiosk.name, "identified_by": "pin"},
            selfie=_selfie_claim(kiosk.organization, data), **_location_claims(data))
        if created:
            punch.device_id = kiosk.pk
            punch.save(update_fields=["device_id"])
        kiosk.last_seen_at = timezone.now()
        kiosk.save(update_fields=["last_seen_at"])
        return JsonResponse({"id": str(punch.pk), "created": created, "review_status": punch.review_status,
                             "exception": punch.exception_reason, "name": person.full_name,
                             "checkpoint": str(checkpoint.pk) if checkpoint else None}, status=201 if created else 200)
    except (KeyError, ValueError, ValidationError, signing.BadSignature) as exc:
        if isinstance(exc, ValidationError):
            return JsonResponse({"error": exc.messages[0]}, status=400)
        if isinstance(exc, signing.BadSignature):
            # Expired and forged are the same answer to the person standing at the pad: type it again.
            return JsonResponse({"error": "That PIN session has ended. Enter the PIN again."}, status=401)
        return JsonResponse({"error": str(exc)}, status=400)

@membership_required(*MANAGERS)
def clock_kiosks(request):
    """Every station the company runs, and the address each officer stands at."""
    if request.method == "POST" and request.POST.get("action") == "open":
        form = _scope_querysets(ClockKioskForm(request.POST), request.organization)
        if form.is_valid():
            try:
                kiosk = open_clock_kiosk(request.organization, form.cleaned_data["name"],
                                         site=form.cleaned_data["site"] or None, actor=request.user)
            except ValidationError as exc:
                for error in exc.messages:
                    messages.error(request, error)
            else:
                messages.success(request, f"{kiosk.name} is open. Give the officers this address and keep it out of sight of anyone who should not be able to use it.")
                return redirect("kiosk", kiosk_id=kiosk.pk)
        return render(request, "core/kiosk_settings.html", {"form": form,
            "kiosks": request.organization.clock_kiosks.select_related("site__client")})
    if request.method == "POST":
        return redirect("clock_kiosks")
    form = _scope_querysets(ClockKioskForm(), request.organization)
    return render(request, "core/kiosk_settings.html", {"form": form,
        "kiosks": request.organization.clock_kiosks.select_related("site__client").order_by("-active", "name"),
        "policy_note": kiosk_policy_refusal(request.organization, None)})

@require_POST
@membership_required(*MANAGERS)
def clock_kiosk_close(request, kiosk_id):
    kiosk = request.organization.clock_kiosks.filter(pk=kiosk_id).first()
    if not kiosk:
        raise Http404
    close_clock_kiosk(kiosk, request.user)
    messages.success(request, f"{kiosk.name} is retired. The punches it recorded stay attributed to it.")
    return redirect("clock_kiosks")

@require_POST
@membership_required(*MANAGERS)
def clock_kiosk_clear(request, kiosk_id):
    """Unlock a station paused by failed PIN attempts without waiting out the window."""
    kiosk = request.organization.clock_kiosks.filter(pk=kiosk_id).first()
    if not kiosk:
        raise Http404
    clear_kiosk_pin_failures(kiosk, request.user)
    messages.success(request, f"{kiosk.name} will accept a PIN again.")
    return redirect("clock_kiosks")

@membership_required()
def clock_pin_settings(request):
    """The officer's own clock PIN: set it once, change it when it gets worn."""
    person = request.organization.people.filter(user=request.user).first()
    if not person:
        messages.error(request, "This sign-in is not linked to a personnel record, so there is no PIN to set. Ask an owner or HR manager to link you from your personnel profile.")
        return redirect("clock")
    has_pin = bool(person.clock_pin)
    locked = clock_pin_lockout(person)
    form = ClockPinForm(request.POST or None, ask_current=has_pin)
    if request.method == "POST" and form.is_valid():
        if has_pin:
            try:
                verify_clock_pin(person, form.cleaned_data["current_pin"])
            except ValidationError as exc:
                form.add_error("current_pin", exc.messages[0])
        if not form.errors:
            try:
                set_clock_pin(person, form.cleaned_data["pin"], actor=request.user, source="self")
            except ValidationError as exc:
                form.add_error("pin", exc.messages[0])
            else:
                messages.success(request, "Clock PIN saved. It works at every shared station and on this page's own clock.")
                return redirect("clock")
    return render(request, "core/clock_pin.html", {"form": form, "person": person, "has_pin": has_pin,
        "locked_minutes": (locked + 59) // 60, "kiosks": request.organization.clock_kiosks.filter(active=True).order_by("name")})

@membership_required(*RECORD_WRITERS)
@transaction.atomic
def person_pin_issue(request, person_id):
    """Issue a PIN to an officer who cannot or will not set their own.

    Owner, administrator, HR — the rungs that already hold a personnel file. A supervisor who can
    schedule a post cannot hand out the credential that authenticates time on it, because that is how
    a shared clock starts recording whoever the supervisor likes.
    """
    person = request.organization.people.filter(pk=person_id).first()
    if not person:
        raise Http404
    form = ClockPinForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            set_clock_pin(person, form.cleaned_data["pin"], actor=request.user, source="issued")
        except ValidationError as exc:
            form.add_error("pin", exc.messages[0])
        else:
            messages.success(request, f"A clock PIN is set for {person.full_name}. Tell them in person, and have them change it from Their PIN.")
            return redirect("person_detail", person_id=person.pk)
    return render(request, "core/form.html", {"form": form, "title": f"Issue a clock PIN for {person.full_name}",
        "eyebrow": "Timekeeping", "cancel_url": reverse("person_detail", args=[person.pk]),
        "note": "The PIN is never shown after it is saved and is not stored in a form anyone can read back. "
                "Hand it over the same way you would hand over a key."})

POLICY_FIELDS = RULE_WATCHED[RuleRevision.Kind.CLOCK_POLICY]
# The override row's own watched set, named once instead of three times inside the view. These
# tuples are the snapshot the version bump compares against, so a field that is missing here is a
# field whose change never raises a revision — the exact drift PAY-5 left behind, which is why both
# lists now come out of RULE_WATCHED rather than being retyped beside it.
OVERRIDE_FIELDS = RULE_WATCHED[RuleRevision.Kind.CLOCK_RULE]
# Tri-state selects speak "yes"/"no"/"" while the row stores True/False/None. The mapping is a
# dict-comp over this tuple so a new nullable field cannot be left out of the edit screen: an
# explicit False rendered as "inherit" is how a waiver silently becomes a second waiver.
TRISTATE_FIELDS = ("require_geofence", "allow_kiosk", "flag_spoof_risk", "require_selfie",
                   "arrival_alert_enabled", "departure_alert_enabled")

@membership_required(*PRIVILEGED)
@transaction.atomic
def time_policy(request):
    """The company's time rules, and every contract or property that differs from them.

    The baseline form and the override list share a screen because they are one decision: an
    operator changing rounding company-wide has to see which posts already claim their own rule,
    or the baseline edit silently rewrites terms a contract agreed with its client. The preview
    is computed from the submitted values, before anything is saved.
    """
    policy, _ = TimePolicy.objects.get_or_create(organization=request.organization, defaults={"timezone": request.organization.timezone})
    before = {name: _audit_value(getattr(policy, name)) for name in POLICY_FIELDS}
    # Snapshotted before validation writes the POST onto this same object, so the version being
    # replaced can be recorded under its own number rather than lost.
    prior_baseline = rule_snapshot(RuleRevision.Kind.CLOCK_POLICY, policy)
    prior_revision = policy.revision
    form = TimePolicyForm(request.POST or None, instance=policy)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        bump_policy_revision(item, before)
        item.save()
        record_rule_revision(item, RuleRevision.Kind.CLOCK_POLICY, request.user,
                             previous=(prior_revision, prior_baseline))
        after = {name: _audit_value(getattr(item, name)) for name in POLICY_FIELDS}
        changes = {name: {"before": before.get(name), "after": value} for name, value in after.items() if before.get(name) != value}
        AuditEvent.objects.create(organization=request.organization, actor=request.user, action="time_policy.updated",
                                  target_type="time_policy", target_id=str(item.pk), metadata={"changes": changes, "version": item.revision})
        messages.success(request, "Time policy saved." if changes else "Nothing changed.")
        return redirect("time_policy")
    coverage, site_count = clock_policy_coverage(request.organization)
    ensure_rule_history(request.organization)
    return render(request, "core/time_policy.html", {
        "form": form, "policy": policy,
        "overrides": request.organization.time_policy_overrides.select_related("client", "site", "authorized_by"),
        "coverage": coverage, "site_count": site_count,
        "history_count": RuleRevision.objects.filter(organization=request.organization).count(),
        "preview": rounding_preview(policy.rounding_mode, policy.rounding_minutes),
    })

@membership_required(*PRIVILEGED)
@transaction.atomic
def time_policy_override(request, override_id=None):
    """Add or edit one deviation, and record who authorized it.

    The author lives on the row, not only in the audit chain: "who said this property does not
    need a geofence" is a question the rule itself has to keep answering after the chain has
    moved on, and a waiver with no name attached to it is not a waiver anyone can defend.
    """
    editing = None
    if override_id:
        editing = request.organization.time_policy_overrides.filter(pk=override_id).first()
        if not editing:
            raise Http404
    before = {name: _audit_value(getattr(editing, name)) for name in OVERRIDE_FIELDS} if editing else {}
    prior_rule = rule_snapshot(RuleRevision.Kind.CLOCK_RULE, editing) if editing else None
    prior_revision = editing.revision if editing else None
    # The tri-state select speaks "yes"/"no"/"" while the row stores True/False/None. Without
    # this mapping an edit screen would render "inherit" for a site that explicitly requires a
    # fence, and saving anything else on that form would silently take the rule away.
    initial = {name: {True: "yes", False: "no"}.get(getattr(editing, name), "") for name in TRISTATE_FIELDS} if editing else {}
    form = _scope_querysets(TimePolicyOverrideForm(
        request.POST or None, instance=editing or TimePolicyOverride(organization=request.organization), initial=initial),
        request.organization)
    if editing:
        # _scope_querysets offers only active targets, which is right for a new rule and a trap
        # for an existing one: deactivate the site and its own rule would become unsaveable,
        # because "Select a valid choice" is not a way to take a fence back down.
        if editing.site_id:
            form.fields["site"].queryset = form.fields["site"].queryset | Site.objects.filter(pk=editing.site_id)
        if editing.client_id:
            form.fields["client"].queryset = form.fields["client"].queryset | Client.objects.filter(pk=editing.client_id)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.organization = request.organization
        if not editing:
            item.authorized_by = request.user
        clash = request.organization.time_policy_overrides.exclude(pk=item.pk)
        if item.site_id and clash.filter(site_id=item.site_id).exists():
            form.add_error("site", "This site already has its own rule; edit it instead.")
        if item.client_id and clash.filter(client_id=item.client_id).exists():
            form.add_error("client", "This contract already has its own rule; edit it instead.")
        if not form.errors:
            try:
                item.full_clean()
            except ValidationError as exc:
                for error in exc.messages:
                    form.add_error(None, error)
            else:
                # Checked here as well as in the model: MySQL cannot enforce a conditional unique
                # constraint, so the database would accept a second rule for the same site.
                if editing:
                    # Only an edit can raise the version. On a create there is nothing to compare
                    # against, and bumping against an empty snapshot made every brand-new rule
                    # arrive as version 2 — which both wasted the number and left version 1, the one
                    # the first timecards were punched under, permanently unrecorded.
                    bump_policy_revision(item, before)
                item.save()
                record_rule_revision(item, RuleRevision.Kind.CLOCK_RULE, request.user,
                                     previous=(prior_revision, prior_rule) if prior_rule else None)
                AuditEvent.objects.create(organization=request.organization, actor=request.user,
                    action="time_policy.override_saved" if editing else "time_policy.override_created",
                    target_type="time_policy_override", target_id=str(item.pk),
                    metadata={"scope": item.scope, "target": item.label, "changed": [name for name, value in ((field, _audit_value(getattr(item, field))) for field in OVERRIDE_FIELDS) if before.get(name) != value],
                              "version": item.revision, "inherited": [name for name in OVERRIDE_FIELDS if getattr(item, name) in (None, "")]})
                messages.success(request, f"Rule saved for {item.label}.")
                return redirect("time_policy")
    submitted = request.POST if request.method == "POST" else None
    return render(request, "core/form.html", {
        "form": form, "title": "Edit clock rule" if editing else "Clock rule for a contract or site",
        "eyebrow": "Timekeeping", "cancel_url": reverse("time_policy"),
        "note": "Leave a field on “inherit” to follow the company policy; only what you set here applies differently.",
        "preview": rounding_preview((submitted or {}).get("rounding_mode") or "", (submitted or {}).get("rounding_minutes") or ""),
    })

@require_POST
@membership_required(*PRIVILEGED)
def time_policy_override_remove(request, override_id):
    row = request.organization.time_policy_overrides.select_related("client", "site").filter(pk=override_id).first()
    if not row:
        raise Http404
    label, scope, revision = row.label, row.scope, row.revision
    row.delete()
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action="time_policy.override_removed",
        target_type="time_policy_override", target_id=str(row.pk), metadata={"scope": scope, "target": label, "version": revision})
    messages.success(request, f"Removed the rule for {label}; it follows the higher scope again. "
        f"Its {revision} recorded version{'' if revision == 1 else 's'} stay in the rule history, so a "
        "punch or timecard stamped against it still reads back the numbers it was worked under.")
    return redirect("time_policy")


@membership_required(*PRIVILEGED)
def rule_history(request, kind=None, rule_id=None):
    """Every version a rule has had, and the ones no rule row is left to point at.

    The list is the audit answer to POL-1: a stamp like ``4:2`` on a timecard is only evidence if
    someone can say what version 2 said. Rules that have been deleted are shown here under their
    own kind and id with no live row behind them, because a history that only lists what still
    exists is a current-state screen with extra steps.
    """
    organization = request.organization
    ensure_rule_history(organization)
    kinds = [(choice, label) for choice, label in RuleRevision.Kind.choices]
    if kind and kind not in dict(kinds):
        raise Http404
    rows = RuleRevision.objects.filter(organization=organization).select_related("saved_by")
    if kind:
        rows = rows.filter(kind=kind)
    if rule_id:
        rows = rows.filter(rule_id=rule_id)
    revisions = list(rows.order_by("kind", "rule_id", "revision"))
    grouped = []
    for key, group in groupby(revisions, key=lambda row: (row.kind, row.rule_id)):
        group = list(group)
        grouped.append({"kind": group[0].get_kind_display(), "kind_value": key[0], "rule_id": key[1],
                        "label": group[-1].label or key[1], "revisions": group, "current": group[-1].revision})
    return render(request, "core/rule_history.html", {
        "kinds": kinds, "grouped": grouped, "chosen": kind or "", "revisions": revisions,
        "rule_id": rule_id or "",
    })

@membership_required(*MANAGERS)
def pay_codes(request):
    """The job and cost-centre codes this firm bills labour under.

    Every payroll system on the receiving end keys an hour on one of these, so the alternative to a
    maintained list is a payroll clerk mapping free text by hand each period — the step that quietly
    drops the codes nobody spelled the same way twice.
    """
    codes = list(request.organization.pay_codes.annotate(
        site_count=Count("used_by_sites"), client_count=Count("used_by_clients"), shift_count=Count("shifts")))
    return render(request, "core/pay_codes.html", {"codes": codes, "can_remove": request.membership.role in PRIVILEGED})


@membership_required(*MANAGERS)
@transaction.atomic
def pay_code_create(request):
    form = _scope_querysets(PayCodeForm(request.POST or None, instance=PayCode(organization=request.organization)),
                            request.organization)
    if request.method == "POST" and form.is_valid():
        item = form.save()
        AuditEvent.objects.create(organization=request.organization, actor=request.user, action="pay_code.created",
            target_type="pay_code", target_id=str(item.pk), metadata={"code": item.code, "name": item.name})
        messages.success(request, f"Pay code {item.code} added.")
        return redirect("pay_codes")
    return render(request, "core/form.html", {"form": form, "title": "New pay code",
        "eyebrow": "Payroll", "cancel_url": reverse("pay_codes")})


@membership_required(*MANAGERS)
def pay_code_edit(request, code_id):
    code = request.organization.pay_codes.filter(pk=code_id).first()
    if not code:
        raise Http404
    return _catalog_edit(request, form_class=PayCodeForm, instance=code, name="pay_code",
        action="pay_code.updated", title="Edit pay code", eyebrow="Payroll", cancel_url=reverse("pay_codes"),
        success="Pay code updated. Timecards already exported keep the code they were produced with.")


@require_POST
@membership_required(*PRIVILEGED)
def pay_code_remove(request, code_id):
    """Retire a code without rewriting the posts that used it.

    Deleting the row would silently change what a past shift says it was billed under, so a code is
    closed instead: it stops being offered to new posts and every historical row still names it.
    """
    code = request.organization.pay_codes.filter(pk=code_id).first()
    if not code:
        raise Http404
    used = code.shifts.count() + code.used_by_sites.count() + code.used_by_clients.count()
    if used:
        code.active = False
        code.save(update_fields=["active"])
        AuditEvent.objects.create(organization=request.organization, actor=request.user, action="pay_code.closed",
            target_type="pay_code", target_id=str(code.pk), metadata={"code": code.code, "in_use": used})
        messages.success(request, f"{code.code} closed — it is on {used} existing record{'' if used == 1 else 's'}, so it was retired rather than deleted.")
    else:
        code_id = code.pk
        code.delete()
        AuditEvent.objects.create(organization=request.organization, actor=request.user, action="pay_code.deleted",
            target_type="pay_code", target_id=str(code_id), metadata={"code": code.code})
        messages.success(request, f"Removed {code.code}; nothing was using it.")
    return redirect("pay_codes")


@membership_required(*PAYROLL)
def payroll_export(request):
    try: start=datetime.fromisoformat(request.GET["start"]); end=datetime.fromisoformat(request.GET["end"])
    except (KeyError,ValueError):
        end=timezone.now(); start=end-timedelta(days=7)
    if timezone.is_naive(start): start=timezone.make_aware(start)
    if timezone.is_naive(end): end=timezone.make_aware(end)
    content=payroll_csv(request.organization,start,end)
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="payroll.exported",target_type="organization",target_id=str(request.organization.pk),metadata={"start":start.isoformat(),"end":end.isoformat()})
    response=HttpResponse(content,content_type="text/csv"); response["Content-Disposition"]='attachment; filename="payroll.csv"'; return response

@membership_required(*RECORD_READERS)
def documents(request):
    # Exists rather than Count("revisions"): a joined aggregate changes what .count() means, and
    # the paginator's total has to be the number of records, not the number of revision rows.
    superseded=PersonDocument.objects.filter(supersedes=OuterRef("pk"))
    # Archived is a third state, not a filter anybody should have to guess: the default register is
    # the active file, and `?archived=1` is the shelf records were filed onto. A record that
    # vanishes from both views is a record that looks deleted.
    show_archived=request.GET.get("archived") == "1"
    scope=_record_visibility(request)
    if show_archived:
        scope = scope & Q(archived_at__isnull=False)
    else:
        scope = scope & Q(archived_at__isnull=True)
    records=_search(request,request.organization.person_documents.filter(scope, deleted_at__isnull=True).select_related("person","document_type","uploaded_by","supersedes"),
                    ("original_name","document_type__name","person__first_name","person__last_name"))
    records=_page(request,records.annotate(superseded_by=Exists(superseded)))
    return render(request,"core/documents.html",{"documents": records, "paginator": records.paginator, "is_paginated": records.has_other_pages(),
                                                 "q": request.GET.get("q","").strip(), "show_archived": show_archived,
                                                 "archived_count":request.organization.person_documents.filter(
                                                     _record_visibility(request),
                                                     deleted_at__isnull=True,archived_at__isnull=False).count(),
                                                 "types":request.organization.document_types.all()})

@membership_required(*RECORD_WRITERS)
@transaction.atomic
def document_type_create(request):
    form=DocumentTypeForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        item=form.save(commit=False); item.organization=request.organization; item.save()
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="document_type.created",target_type="document_type",target_id=str(item.pk),metadata={"name":item.name})
        messages.success(request,"Document type created."); return redirect("settings_compliance")
    return render(request,"core/form.html",{"form":form,"title":"Add document type","eyebrow":"HCRM records"})

@membership_required(*RECORD_WRITERS)
@transaction.atomic
def document_type_edit(request,type_id):
    document_type=request.organization.document_types.filter(pk=type_id).first()
    if not document_type:raise Http404
    return _catalog_edit(request,form_class=DocumentTypeForm,instance=document_type,name="document_type",action="document_type.updated",title="Edit record type",eyebrow="HCRM records",cancel_url=reverse("settings_compliance"),success="Record type updated. Existing files keep the retention date they were filed with.")


def _document_type_usage(document_type):
    """What still points at a record type, by kind. Deleted and archived files count: they are records."""
    usage = {
        "file": document_type.documents.count(),
        "signing request": document_type.signingrequest_set.count(),
        "onboarding item": document_type.onboarding_items.count(),
        "compliance duty": document_type.rules_requiring_it.count(),
    }
    return {label: count for label, count in usage.items() if count}


def _document_types_in_use(organization):
    used = set(organization.person_documents.values_list("document_type_id", flat=True))
    used |= set(organization.signing_requests.values_list("document_type_id", flat=True))
    used |= set(organization.onboarding_items.values_list("document_type_id", flat=True))
    used |= set(organization.compliance_rules.values_list("document_type_id", flat=True))
    used.discard(None)
    return used


@require_POST
@membership_required(*RECORD_WRITERS)
@transaction.atomic
def document_type_remove(request, type_id):
    """Delete an unused record type; retire one that files, signatures or rules still name."""
    document_type = request.organization.document_types.filter(pk=type_id).first()
    if not document_type:
        raise Http404
    usage = _document_type_usage(document_type)
    if usage:
        summary = ", ".join(f"{count} {label}{'' if count == 1 else 's'}" for label, count in usage.items())
        if document_type.active:
            document_type.active = False
            document_type.save(update_fields=["active"])
            AuditEvent.objects.create(organization=request.organization, actor=request.user, action="document_type.retired",
                target_type="document_type", target_id=str(document_type.pk), metadata={"code": document_type.code, "in_use": usage})
            messages.success(request, f"{document_type.name} is still used by {summary}, so it was retired instead of deleted. It is no longer offered for new uploads.")
        else:
            messages.error(request, f"{document_type.name} is still used by {summary} and cannot be deleted.")
        return redirect("settings_compliance")
    type_pk, code, name = document_type.pk, document_type.code, document_type.name
    document_type.delete()
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action="document_type.deleted",
        target_type="document_type", target_id=str(type_pk), metadata={"code": code, "name": name})
    messages.success(request, f"Deleted the {name} record type; nothing was filed under it.")
    return redirect("settings_compliance")

@membership_required(*RECORD_WRITERS)
@transaction.atomic
def document_upload(request,person_id=None):
    form=DocumentUploadForm(request.POST or None,request.FILES or None)
    form.fields["person"].queryset=request.organization.people.all(); form.fields["document_type"].queryset=request.organization.document_types.filter(active=True)
    # Only a version nobody has already replaced can be superseded, or the chain would fork.
    form.fields["revises"].queryset=request.organization.person_documents.filter(deleted_at__isnull=True,revisions__isnull=True)
    bound=_profile_person(request,person_id) if person_id else None
    form.fields["revises"].queryset = form.fields["revises"].queryset.filter(
        _record_visibility(request)
    ).select_related("person", "document_type")
    if bound:
        form.bind_person(bound)
        form.fields["revises"].queryset = form.fields["revises"].queryset.filter(person=bound)
    if request.method=="POST" and form.is_valid():
        person=bound or form.cleaned_data["person"]
        try:
            document=store_person_document(organization=request.organization,person=person,document_type=form.cleaned_data["document_type"],upload=form.cleaned_data["file"],actor=request.user,expires_on=form.cleaned_data["expires_on"],supersedes=form.cleaned_data["revises"])
        except ValidationError as exc: form.add_error("file",exc)
        else:
            if document.revision_number>1:
                messages.success(request,f"Document uploaded and scanned as revision {document.revision_number}. Signatures already given stay with the version they were given for, so the queue asks the roster again.")
            else:
                messages.success(request,"Document uploaded and scanned.")
            return redirect(_record_return(bound,"documents") or reverse("documents"))
    return render(request,"core/form.html",{"form":form,"title":"Upload personnel document","eyebrow":"HCRM records","person":bound,"cancel_url":_record_return(bound,"documents")})

@membership_required()
def document_download(request,document_id):
    document=request.organization.person_documents.select_related("person","document_type").filter(pk=document_id,deleted_at__isnull=True,scan_status=PersonDocument.ScanStatus.CLEAN).first()
    if not document: raise Http404
    # One decision for every record route: the audience rule (a company record issued to every
    # worker is readable by any active member; a management-audience one stays with officers) and
    # the secrecy rung are both inside `record_readable`, which is the row-level twin of the
    # filter the register and the person tab use. A worker may open their own file; a sealed
    # investigation about them is not theirs to open even from an HR sign-in.
    if not _record_open(request, document): raise Http404
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="document.downloaded",target_type="person_document",target_id=str(document.pk))
    response=FileResponse(document.file.open("rb"),as_attachment=True,filename=document.original_name,content_type="application/octet-stream")
    response["X-Content-Type-Options"]="nosniff"; response["Cache-Control"]="private, no-store"; return response


@membership_required()
def document_preview(request,document_id):
    """Show a record inside the page. Owner ruling of 2026-10-03: permission to read is permission
    to view — the access decision is the same one the download route makes, not a weaker copy of it.

    Two transports, one policy. For an image, where the object store can produce a genuinely signed
    link, the response is a redirect to it: the bytes never pass through this process, the link expires in
    `PREVIEW_URL_SECONDS`, and the fetch is served with *this application's* content type because the
    signature overrides the object's own metadata. PDFs and text are read by page script and so always
    stream (see `preview_source`), as does everything on local disk or a custom domain whose `url()`
    would come back unsigned: the view streams the file itself and puts
    the equivalent headers on the response, re-reading the head bytes first.

    Either way the served type is from the verified allowlist, never from the uploader's declaration,
    and nothing scriptable is on that list at all. A record whose bytes were never verified
    (everything filed before the column existed) simply has no preview and still downloads.
    """
    document=request.organization.person_documents.select_related("person","document_type").filter(pk=document_id,deleted_at__isnull=True,scan_status=PersonDocument.ScanStatus.CLEAN).first()
    if not document: raise Http404
    if not _record_open(request, document): raise Http404
    try:
        url,handle,content_type,kind=preview_source(document)
    except PreviewUnavailable as exc:
        messages.info(request,f"{exc} Use Download instead.")
        return redirect(_record_return(document.person,"documents") or reverse("documents"))
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="document.previewed",target_type="person_document",target_id=str(document.pk),metadata={"kind":kind,"content_type":content_type,"signed":bool(url)})
    if url is not None:
        response=redirect(url)
    else:
        response=FileResponse(handle,as_attachment=False,filename=preview_filename(document),content_type=content_type)
    # The browser-side policy that made "never inline" the safe answer, restored here rather than
    # waived: no scripting, no plugins, no navigation from these bytes, and no sniffing into a type we
    # did not choose. The page's PDF.js reads these bytes with fetch and draws them itself, so nothing
    # here is ever rendered by a plugin. `frame-ancestors 'self'` remains for a same-origin opener and
    # replaces the global `frame-ancestors 'none'`, which the security middleware applies with
    # setdefault and so does not overwrite.
    response["Content-Security-Policy"]=("default-src 'none'; base-uri 'none'; form-action 'none'; "
        "script-src 'none'; object-src 'none'; frame-ancestors 'self'; img-src 'self' data:; media-src 'self'")
    response["X-Content-Type-Options"]="nosniff"
    response["X-Frame-Options"]="SAMEORIGIN"
    response["Cache-Control"]="private, no-store"
    response["Content-Disposition"]=f'inline; filename="{preview_filename(document)}"'
    return response

@membership_required()
def my_documents(request):
    person=request.organization.people.filter(user=request.user).first()
    if person:
        scope=Q(person=person)|_workforce_documents()
    else:
        scope=_workforce_documents()
    records=PersonDocument.objects.filter(scope,deleted_at__isnull=True,archived_at__isnull=True,scan_status=PersonDocument.ScanStatus.CLEAN).filter(_record_visibility(request)).select_related("document_type").distinct()
    # Per-signer state, never the shared column: PersonDocument.acknowledged_at says someone
    # signed, so showing it here hid the Review action from every worker after the first one.
    acknowledged=set(DocumentAcknowledgment.objects.filter(
        person=person,document__in=records,
    ).values_list("document_id",flat=True)) if person else set()
    return render(request,"core/my_documents.html",{"person":person,"documents":records,"acknowledged":acknowledged})

@membership_required(*RECORD_READERS)
def document_acknowledgments(request,document_id):
    """Who has and has not signed a company record issued to every worker."""
    document=request.organization.person_documents.select_related("document_type","supersedes").filter(pk=document_id,deleted_at__isnull=True).first()
    if not document:raise Http404
    # This page names every worker who has *not* signed, so a sealed record's roster is as
    # revealing as the record itself: the same ladder decides both.
    if not _record_open(request, document):raise Http404
    signed=document.acknowledgments.select_related("person").order_by("-acknowledged_at")
    # One roster for both questions on this page, so "outstanding on this text" and "never signed
    # any version" cannot be computed against different populations and disagree by construction.
    roster=scope_for(request).filter_people(request.organization.people.exclude(status__in=Person.NON_WORKING_STATUSES))
    return render(request,"core/document_acknowledgments.html",{
        "document":document,"signed":signed,
        "outstanding":outstanding_acknowledgments(document,people=roster),
        "lineage":signature_lineage(document,people=roster),
        # The next version, if someone has already filed one: signatures on this row are
        # history from that moment, and the roster that matters is on the other page.
        "successor":document.revisions.filter(deleted_at__isnull=True).order_by("-created_at").first(),
        "can_remind":request.membership.role in RECORD_WRITERS and document.document_type.audience==DocumentType.Audience.WORKFORCE,
    })

@require_POST
@membership_required(*RECORD_WRITERS)
def document_remind(request,document_id):
    document=request.organization.person_documents.filter(pk=document_id,deleted_at__isnull=True).first()
    if not document:raise Http404
    try:
        queued=queue_acknowledgment_reminders(document,request.user)
    except ValidationError as exc:
        messages.error(request,str(exc))
    else:
        messages.success(request,f"Queued {queued} reminder{'s' if queued!=1 else ''} for the workers who have not acknowledged this record.")
    return redirect("document_acknowledgments",document_id=document.pk)

@membership_required()
@transaction.atomic
def document_acknowledge(request,document_id):
    import hashlib,hmac
    from django.conf import settings
    person=request.organization.people.filter(user=request.user).first();document=request.organization.person_documents.select_related("document_type","person").filter(pk=document_id,deleted_at__isnull=True,scan_status=PersonDocument.ScanStatus.CLEAN).first()
    if not document or not person:raise Http404
    # This route has already loaded the viewer's own record; hand it to the ladder instead of
    # paying for the same lookup twice.
    request._record_subject=person
    if document.person_id and document.person_id!=person.pk:raise Http404
    if document.document_type.audience!=DocumentType.Audience.PERSON and document.person_id:raise Http404
    # Signing a replaced version would record agreement with text nobody is bound to any more,
    # so the old row stays readable as history and the action moves to the current revision.
    successor=document.revisions.filter(deleted_at__isnull=True,scan_status=PersonDocument.ScanStatus.CLEAN).order_by("-created_at").first()
    if successor:
        messages.error(request,f"“{document.original_name}” has been replaced by revision {successor.revision_number}. Sign the current version from My documents.")
        return redirect("my_documents")
    # A record the ladder does not release to this reader is not theirs to sign, and the route
    # must refuse before it renders the form: a member who guesses the UUID could otherwise read
    # the title of a sealed investigation file and file a signature against it. The management
    # audience rule and the secrecy rung are the same decision, made in one place.
    if not _record_open(request, document):raise Http404
    form=DocumentAcknowledgmentForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        signature=form.cleaned_data["signature_name"].strip()
        if document.document_type.signature_required and not signature:form.add_error("signature_name","A typed signature is required.")
        else:
            ip=request.META.get("REMOTE_ADDR","");ip_hash=hmac.new(settings.SECRET_KEY.encode(),ip.encode(),hashlib.sha256).hexdigest()
            item,_=DocumentAcknowledgment.objects.update_or_create(document=document,person=person,defaults={"organization":request.organization,"user":request.user,"signature_name":signature,"statement":"I reviewed and acknowledge this document.","document_sha256":document.sha256,"ip_hash":ip_hash})
            if document.person_id:
                # Only a record with exactly one possible signer is "complete" on its own row.
                document.acknowledged_at=item.acknowledged_at;document.save(update_fields=["acknowledged_at"])
            AuditEvent.objects.create(organization=request.organization,actor=request.user,action="document.acknowledged",target_type="person_document",target_id=str(document.pk),metadata={"sha256":document.sha256,"signed":bool(signature),"person":str(person.pk)});messages.success(request,"Acknowledgment recorded.");return redirect("my_documents")
    return render(request,"core/document_acknowledge.html",{"form":form,"document":document})

@membership_required()
def person_export(request, person_id):
    """One person's file as a ZIP: dossier, manifest, and the records this reader may open.

    Two doors, one rule. A record reader gets the file within their authority scope; the officer the
    file is about gets their own, with the same sealed-from-subject carve-out the download route
    applies. Both are audited with the record types that went in, because an export is the one read
    that leaves the building — a log saying "the file was downloaded" does not answer "what did the
    company hand over".
    """
    person = request.organization.people.filter(pk=person_id).select_related("branch").first()
    if not person:
        raise Http404
    subject = _record_subject(request)
    is_self = bool(subject) and subject.pk == person.pk
    staff = request.membership.role in RECORD_READERS
    if not (is_self or staff):
        raise Http404
    if staff and not scope_for(request).permits_person(person):
        raise Http404
    bundle = personnel_file_bundle(request.organization, person, request.membership.role,
                                 subject.pk if subject else None,
                                 sensitive_fields=request.membership.role in MANAGERS)
    archive = personnel_file_zip(bundle)
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action="person.exported",
        target_type="person", target_id=str(person.pk),
        metadata={"self_service": is_self and not staff, "records": len(bundle["manifest"]),
                  "record_types": sorted({row["record_type"] for row in bundle["manifest"]}),
                  "files_included": len(bundle["files"]), "withheld": len(bundle["notes"]),
                  "sections": sorted(bundle["dossier"].keys()), "bytes": len(archive)})
    filename = f"personnel-file-{person.last_name.lower() or 'person'}-{person.first_name.lower()}-{timezone.localdate()}.zip"
    response = HttpResponse(archive, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN)
def retention_review(request):
    live=request.organization.person_documents.filter(deleted_at__isnull=True)
    # Three states, because "past its retention date", "filed away" and "gone" are three different
    # questions with three different answers available. Listing an archived record as due for review
    # would invite a second disposition on a record already under one, and dropping the tombstones
    # from the page entirely would make a deletion look like it never happened.
    due=live.filter(archived_at__isnull=True,retain_until__isnull=False,retain_until__lte=timezone.localdate()).select_related("person","document_type")
    archived=live.filter(archived_at__isnull=False).select_related("person","document_type").order_by("-archived_at")
    tombstones=request.organization.person_documents.filter(deleted_at__isnull=False).select_related("person","document_type").order_by("-deleted_at")
    pending=request.organization.disposition_requests.select_related("document__person","requested_by","approved_by","restored_by")[:100]
    return render(request,"core/retention.html",{"due":due,"archived":archived,"tombstones":tombstones,"pending":pending})

@require_POST
@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN)
@transaction.atomic
def document_restore(request,document_id):
    """Reverse an archive disposition. Deletion is not reversible and says so on the page.

    Keyed by the document rather than the request, because that is what the screen lists and what a
    person holding a paper file would name — and it refuses when the latest executed disposition on
    the row was a deletion, so the button cannot be pointed at the wrong decision.
    """
    document=request.organization.person_documents.filter(pk=document_id).first()
    if not document: raise Http404
    item=document.disposition_requests.filter(status=DispositionRequest.Status.EXECUTED).order_by("-executed_at").first()
    if not item:
        messages.error(request,"This record has no executed disposition to reverse.")
        return redirect("retention_review")
    reason=(request.POST.get("reason") or "").strip()
    try:
        restore_disposition(item,request.user,reason)
    except ValidationError as exc:
        messages.error(request,str(exc))
    else:
        messages.success(request,"Record restored to the personnel file. The disposition row now reads as reversed, with you named against it.")
    return redirect("retention_review")

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN)
@transaction.atomic
def disposition_request(request,document_id):
    document=request.organization.person_documents.filter(pk=document_id,deleted_at__isnull=True).first()
    if not document: raise Http404
    form=DispositionRequestForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        item=DispositionRequest.objects.create(organization=request.organization,document=document,requested_by=request.user,action=form.cleaned_data["action"],reason=form.cleaned_data["reason"])
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="document.disposition_requested",target_type="disposition_request",target_id=str(item.pk),metadata={"document":str(document.pk),"action":item.action})
        # NTF-3: a disposition waits on a *second* approver, and nothing tells them. A request that
        # nobody knows about is indistinguishable from a record nobody asked to delete.
        queue_notice(organization=request.organization,recipients=set(role_recipients(request.organization,PRIVILEGED))-{request.user.pk},event_type="retention.disposition_requested",
            subject=f"Second approval needed: {item.get_action_display().lower()} “{document.original_name}”",
            body=f"{request.user} asked to {item.action.lower()} “{document.original_name}” ({document.document_type.name}). Reason: {item.reason}. A second owner or administrator must authorize it.",
            dedup_key=f"retention.requested:{item.pk}",
            sms={"item": document.original_name, "action": item.get_action_display().lower(),
                 "actor": request.user, "note": item.reason})
        messages.success(request,"Disposition request created.");return redirect("retention_review")
    return render(request,"core/form.html",{"form":form,"title":"Request record disposition","eyebrow":"Retention"})

@require_POST
@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN)
def disposition_execute(request,request_id):
    item=request.organization.disposition_requests.select_related("document").filter(pk=request_id).first()
    if not item: raise Http404
    try: execute_disposition(item,request.user)
    except ValidationError as exc: messages.error(request,str(exc))
    else: messages.success(request,"Disposition completed and tombstone recorded.")
    return redirect("retention_review")

@require_POST
@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN)
def legal_hold_toggle(request,document_id):
    document=request.organization.person_documents.filter(pk=document_id,deleted_at__isnull=True).first()
    if not document: raise Http404
    before=document.legal_hold;document.legal_hold=not before;document.save(update_fields=["legal_hold"])
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="document.legal_hold_changed",target_type="person_document",target_id=str(document.pk),metadata={"before":before,"after":document.legal_hold})
    # NTF-3: RB is explicit that a legal hold overrides scheduled disposition, so the notice goes to
    # the records desk that would otherwise work the retention queue and find the row simply gone.
    queue_notice(organization=request.organization,recipients=role_recipients(request.organization,RECORD_WRITERS),event_type="retention.hold_changed",
        subject=f"Legal hold {'applied' if document.legal_hold else 'released'}: {document.original_name}",
        body=(f"{request.user} {'placed' if document.legal_hold else 'removed'} a legal hold on “{document.original_name}” "
              f"({document.document_type.name}). Disposition is {'blocked while the hold stands' if document.legal_hold else 'possible again'}."),
        dedup_key=f"retention.hold:{document.pk}:{document.legal_hold}",
        sms={"status": "applied" if document.legal_hold else "released", "item": document.original_name,
             "actor": request.user})
    messages.success(request,"Legal hold updated.");return redirect("retention_review")

@membership_required(*MANAGERS)
def training(request):
    scope=scope_for(request)
    records=_page(request,_search(request,scope.filter_by_person(request.organization.training_records.select_related("person")).order_by("-completed_on"),
                                 ("course_name","provider","certificate_number","person__first_name","person__last_name")))
    return render(request,"core/training.html",{"records":records,"paginator":records.paginator,"is_paginated":records.has_other_pages(),
                                                "q": request.GET.get("q","").strip(),
                                                "can_write":request.membership.role in RECORD_WRITERS,"authority_scope":scope if scope.restricted else None})

@membership_required(*RECORD_WRITERS)
@transaction.atomic
def training_create(request,person_id=None):
    form=_scoped_form(TrainingRecordForm,request.organization,request.POST or None); form.fields["person"].queryset=request.organization.people.all()
    bound=_profile_person(request,person_id) if person_id else None
    if bound: form.bind_person(bound)
    if request.method=="POST" and form.is_valid():
        item=form.save(commit=False); item.organization=request.organization; item.full_clean(); item.save()
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="training.created",target_type="training_record",target_id=str(item.pk),metadata={"person":str(item.person_id),"course":item.course_name})
        messages.success(request,"Training record added.")
        return redirect(_record_return(bound,"training") or reverse("training"))
    return render(request,"core/form.html",{"form":form,"title":"Add training record","eyebrow":"Compliance","person":bound,"cancel_url":_record_return(bound,"training")})

@membership_required(*RECORD_WRITERS)
@transaction.atomic
def training_edit(request,record_id):
    record=request.organization.training_records.select_related("person").filter(pk=record_id).first()
    if not record:raise Http404
    before={field:_audit_value(getattr(record,field)) for field in ("course_name","provider","completed_on","expires_on","certificate_number","hours")}
    form=TrainingRecordForm(request.POST or None,instance=record)
    form.fields.pop("person",None)
    if request.method=="POST" and form.is_valid():
        item=form.save()
        after={field:_audit_value(getattr(item,field)) for field in ("course_name","provider","completed_on","expires_on","certificate_number","hours")}
        changes={field:{"before":before[field],"after":value} for field,value in after.items() if before[field]!=value}
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="training.updated",target_type="training_record",target_id=str(item.pk),metadata={"person":str(item.person_id),"changes":changes})
        messages.success(request,"Training record updated.")
        return redirect(_record_return(item.person,"training"))
    return render(request,"core/form.html",{"form":form,"title":"Update training record","eyebrow":"Compliance","person":record.person,"cancel_url":_record_return(record.person,"training")})

@membership_required(*RECORD_WRITERS)
def imports(request):
    form=CsvImportForm(request.POST or None,request.FILES or None); batch=None
    if request.method=="POST" and form.is_valid():
        try: batch,_=preview_csv_import(organization=request.organization,entity=form.cleaned_data["entity"],upload=form.cleaned_data["file"],actor=request.user)
        except ValidationError as exc: form.add_error("file",exc)
    return render(request,"core/imports.html",{"form":form,"batch":batch,"batches":request.organization.import_batches.all()[:20]})

@membership_required(*MANAGERS)
def import_template(request,entity):
    from .services import IMPORT_COLUMNS
    columns=IMPORT_COLUMNS.get(entity)
    if not columns: raise Http404
    response=HttpResponse(",".join(sorted(columns))+"\n",content_type="text/csv")
    response["Content-Disposition"]=f'attachment; filename="{entity}-import-template.csv"'
    return response

@membership_required(*MANAGERS)
def import_errors(request,batch_id):
    import csv
    from io import StringIO
    batch=request.organization.import_batches.filter(pk=batch_id).first()
    if not batch: raise Http404
    output=StringIO();writer=csv.writer(output);writer.writerow(["row","errors"])
    for item in batch.errors:writer.writerow([item.get("row"),"; ".join(item.get("errors",[]))])
    response=HttpResponse(output.getvalue(),content_type="text/csv");response["Content-Disposition"]=f'attachment; filename="{batch.entity}-import-errors.csv"';return response

@require_POST
@membership_required(*RECORD_WRITERS)
def import_apply(request,batch_id):
    batch=request.organization.import_batches.filter(pk=batch_id).first()
    if not batch: raise Http404
    try: count=apply_csv_import(batch,request.user)
    except Exception as exc:
        messages.error(request,f"Import could not be applied: {exc}")
    else: messages.success(request,f"Imported {count} rows.")
    return redirect("imports")

@membership_required()
def notifications(request):
    from .notification_actions import notification_action
    items=_page(request,request.user.workforce_notifications.filter(organization=request.organization).order_by("-created_at"))
    for item in items:
        item.action = notification_action(request, item)
    return render(request,"core/notifications.html",{"notifications":items,"paginator":items.paginator,"is_paginated":items.has_other_pages()})

@require_POST
@membership_required()
def notification_read(request,notification_id):
    item=request.user.workforce_notifications.filter(organization=request.organization,pk=notification_id).first()
    if not item: raise Http404
    if item.channel==Notification.Channel.IN_APP: item.status=Notification.Status.READ; item.save(update_fields=["status"])
    return redirect("notifications")

# ── NTF-4: consent, suppression, and the provider's side of the conversation ─────
#
# Three surfaces. The officer's own page answers "did I agree to texts, and about which number"; the
# owner's messaging page shows the callback address, who is suppressed and why, and what the provider
# last said; and one ingest endpoint takes whatever a provider posts, retains it, and acts on the parts
# that change whether the next message may go out.

def _capture_signup_text_consent(request, invitation, form, user):
    """Record the opt-in that came with an accepted invitation, if there was one.

    A number with no tick stores the number and writes **no consent row at all**. The absence of a
    decision is the true state, and inventing a "no" row to represent silence would make a later, real
    "yes" read as the reversal of something the person never declined.
    """
    if form is None or invitation.person_id is None:
        return
    mobile = str(form.cleaned_data.get("mobile_phone") or "").strip()
    person = invitation.person
    if mobile and person.mobile_phone != mobile:
        person.mobile_phone = mobile
        person.save(update_fields=["mobile_phone"])
    if not form.cleaned_data.get("text_alerts") or not mobile:
        return
    record_consent(organization=invitation.organization, person=person, destination=mobile,
        state=MessageConsent.State.GRANTED, source=MessageConsent.Source.SIGNUP,
        wording=sms_consent_wording(invitation.organization),
        evidence={"surface": "invitation_accept", "ip": client_ip(request)}, actor=user)

@login_required
def about(request):
    """The release number and the open-source notices every component here obliges us to show.

    Any signed-in user may read it, including one not yet placed in a company, because the
    attribution duty runs to everyone using the software. It stays behind sign-in so the exact
    dependency versions are not handed to anonymous visitors.
    """
    import platform
    from .about import SERVICES, bundled_components, python_components, runtime_license
    packages = python_components()
    bundled = bundled_components()
    return render(request, "core/about.html", {
        "packages": packages, "bundled": bundled, "services": SERVICES,
        "python_version": platform.python_version(), "python_license": runtime_license(),
        "component_count": len(packages) + len(bundled),
    })

@membership_required()
def my_account(request):
    """One place for everything a signed-in person owns about themselves."""
    organization = request.organization
    person = organization.people.select_related("branch").filter(user=request.user).first()
    consent = current_consent(organization, person.mobile_phone) if person and person.mobile_phone else None
    from allauth.mfa.utils import is_mfa_enabled
    return render(request, "core/my_account.html", {
        "person": person, "consent": consent,
        "mfa_enabled": is_mfa_enabled(request.user),
        "has_password": request.user.has_usable_password(),
        "pin_set": bool(person and person.pin_set_at),
    })

@membership_required()
@transaction.atomic
def my_contact_edit(request):
    """Self-service contact details, written to the personnel history exactly as a manager edit is."""
    from .forms import SelfContactForm
    person = request.organization.people.filter(user=request.user).first()
    if not person:
        messages.error(request, "This sign-in is not linked to a personnel record. Ask HR to link it.")
        return redirect("my_account")
    before = person_snapshot(person)
    form = SelfContactForm(request.POST or None, instance=person)
    if request.method == "POST" and form.is_valid():
        person = form.save()
        changes = record_person_history(person, before, request.user)
        messages.success(request, "Contact details updated." if changes else "Nothing changed.")
        return redirect("my_account")
    return render(request, "core/form.html", {
        "form": form, "title": "Update my contact details", "eyebrow": "My account",
        "note": "Name, employee ID, licensing, pay and branch are kept by HR — ask them to correct those. Every change here is recorded in your personnel history.",
        "cancel_url": reverse("my_account"),
    })

@membership_required()
@transaction.atomic
def text_alerts(request):
    """The officer's text-message consent, shown with the wording it was given.

    DD §Email, text messaging makes consent mandatory; RB §Email and SMS providers lists opt-out
    processing as part of the SMS interface. The cheapest honest place for both is a page the person
    already has a reason to open, prefilled with the number the company holds — because asking somebody
    to retype a number they never gave you is how consent pages get skipped.
    """
    organization = request.organization
    person = organization.people.filter(user=request.user).first()
    if not person:
        messages.error(request, "This sign-in is not linked to a personnel record, so there is no number to text.")
        return redirect("dashboard")
    current = current_consent(organization, person.mobile_phone) if person.mobile_phone else None
    form = TextAlertsForm(request.POST or None, organization=organization,
        initial={"mobile_phone": person.mobile_phone, "opt_in": False})
    if request.method == "POST" and form.is_valid():
        mobile = form.cleaned_data["mobile_phone"]
        opted_in = form.cleaned_data["opt_in"] and request.POST.get("choice") not in ("off", "no")
        if mobile and person.mobile_phone != mobile:
            person.mobile_phone = mobile
            person.save(update_fields=["mobile_phone"])
        if opted_in:
            record_consent(organization=organization, person=person, destination=mobile,
                state=MessageConsent.State.GRANTED, source=MessageConsent.Source.PROFILE,
                wording=sms_consent_wording(organization),
                evidence={"surface": "text_alerts", "ip": client_ip(request)}, actor=request.user)
            messages.success(request, "Text alerts are on for that number. An enrollment confirmation is"
                                       " queued when consent is newly granted. Reply STOP to opt out.")
            return redirect("text_alerts")
        if current and current.state == MessageConsent.State.GRANTED and request.POST.get("choice") in ("off", "no"):
            record_consent(organization=organization, person=person, destination=current.destination,
                state=MessageConsent.State.REVOKED, source=MessageConsent.Source.PROFILE,
                wording="turned off from the Text alerts page",
                evidence={"surface": "text_alerts", "ip": client_ip(request)}, actor=request.user)
            messages.success(request, "Text alerts are off. Nothing goes to that number until you turn"
                                       " them back on.")
            return redirect("text_alerts")
        if mobile and request.POST.get("choice") == "no":
            # A declined prompt is an answer, not silence, and it is recorded as one: the ledger then
            # says this person was asked on this date and said no, which is what stops the prompt
            # returning next month and is the record a "why am I not getting my schedule?" question
            # ends with.
            record_consent(organization=organization, person=person, destination=mobile,
                state=MessageConsent.State.REVOKED, source=MessageConsent.Source.FIRST_LOGIN,
                wording="declined from the first-sign-in prompt",
                evidence={"surface": "dashboard_prompt", "ip": client_ip(request)}, actor=request.user)
            messages.success(request, "Noted — no texts. Change your mind any time on this page.")
            return redirect("text_alerts")
        if mobile:
            messages.info(request, "Nothing changed — that number already has no decision on file.")
            return redirect("text_alerts")
    # The ledger is keyed on the normalized number, so the page has to look it up the same way the
    # writer stored it — otherwise an officer who opted in sees "no decisions recorded about this
    # number" on the very page whose whole job is that number's decision.
    key = normalize_destination(person.mobile_phone) if person.mobile_phone else ""
    history = (MessageConsent.objects.filter(organization=organization, destination=key)
               .order_by("-decided_at")[:20] if key else MessageConsent.objects.none())
    return render(request, "core/text_alerts.html", {
        "person": person, "form": form, "current": current, "history": history,
        "wording": sms_consent_wording(organization),
        "block": active_suppression(organization, person.mobile_phone) if person.mobile_phone else None})

# NTF-1's surface imports its own three names here rather than joining the block at the top of the
# file, so the channel-rule views read as one section: what they touch is exactly what they import.
from .forms import ChannelRuleForm, SmsProgramForm
from .models import ChannelRule
from .services import audience_reach, confirm_sns_subscription

@membership_required(*PRIVILEGED)
@never_cache
def messaging_settings(request):
    """The callback address, the suppression list, and what the providers last told us.

    An owner who has never seen the webhook address cannot configure it, and an owner who cannot see the
    suppression list cannot tell why a guard stopped getting notices. Both halves belong on one page
    because they are one question: is this company's messaging reaching people, and if not, why.

    NTF-1's channel rules live here too rather than on a page of their own, for the same reason: the
    consent ledger directly beneath the rule editor is what tells an operator whether the rule they are
    about to save can reach anybody.
    """
    organization = request.organization
    program_form = SmsProgramForm(request.POST or None, instance=organization)
    if request.method == "POST" and program_form.is_valid():
        program_form.save()
        AuditEvent.objects.create(organization=organization, actor=request.user,
            action="message.sms_program_updated", target_type="organization",
            target_id=str(organization.pk), metadata={"fields": program_form.changed_data})
        messages.success(request, "SMS program disclosures saved. New text-alert opt-ins are available.")
        return redirect("messaging_settings")
    callback_definitions = (
        ("twilio", "Twilio SMS", "Set both the messaging status callback and the incoming-message webhook to this URL, using HTTP POST."),
        ("mailjet", "Mailjet email", "In Mailjet Event Tracking, configure this URL for the email events you want to receive, including bounces, complaints, and unsubscribes."),
        ("postmark", "Postmark email", "In your Postmark server / message stream webhook settings, use this URL for delivery, bounce, spam complaint, and subscription-change events."),
        ("sns", "Amazon SES via SNS", "Subscribe this HTTPS endpoint to the SNS topics configured for SES delivery, bounce, and complaint notifications. Confirm the pending subscription below. This endpoint does not process SNS SMS delivery logs."),
    )
    callbacks = [{
        "provider": provider, "label": label, "instructions": instructions,
        "url": request.build_absolute_uri(reverse("provider_callback", args=[provider, organization.webhook_token])),
    } for provider, label, instructions in callback_definitions] if organization.webhook_token else []
    rules = list(organization.channel_rules.select_related("created_by"))
    for rule in rules:
        rule.text_reach = (audience_reach(organization, rule.audience, Notification.Channel.SMS)
                           if Notification.Channel.SMS in (rule.channels or []) else None)
    # `?edit=<id>` prefills the same form with one existing rule. Scoped through
    # `organization.channel_rules`, so an id belonging to another company simply is not found and the
    # page renders its own blank form rather than somebody else's rule — and a hand-typed value is
    # treated as absent instead of raising, because a query string is not a trusted uuid.
    editing = None
    wanted = request.GET.get("edit")
    if wanted:
        try:
            editing = organization.channel_rules.filter(pk=uuid.UUID(str(wanted))).first()
        except (ValueError, TypeError, AttributeError):
            editing = None
    return render(request, "core/messaging_settings.html", {
        "organization": organization,
        "program_form": program_form,
        "webhook_token": organization.webhook_token or "",
        "callbacks": callbacks,
        "rules": rules,
        "editing": editing,
        "rule_form": ChannelRuleForm(instance=editing) if editing else ChannelRuleForm(),
        "audience_choices": ChannelRule.Audience.choices,
        # Subscriptions AWS is still waiting to confirm. Bounded in Python rather than with a
        # `raw__has_key` lookup because the JSON path differs across the two databases this suite runs
        # on, and twenty rows is more than one company can plausibly have pending.
        "pending_subscriptions": [row for row in organization.delivery_events.filter(
            provider="sns", applied=False, kind=DeliveryEvent.Kind.STATUS).order_by("-created_at")[:20]
            if (row.raw or {}).get("subscribe_url")],
        "suppressions": organization.suppressions.filter(cleared_at__isnull=True).order_by("-since")[:100],
        "events": organization.delivery_events.select_related("notification").order_by("-created_at")[:100],
        "consents": organization.message_consents.select_related("person").order_by("-decided_at")[:100],
        "blocked": organization.notifications.filter(status=Notification.Status.BLOCKED)
                     .order_by("-created_at")[:50]})

@require_POST
@membership_required(*PRIVILEGED)
@transaction.atomic
def messaging_rule_save(request, rule_id=None):
    """Say which audience hears which family on which channel, and print what that will do.

    The confirmation names the reach in people, not in abstractions: "text officers about credential
    reminders — 9 of the 41 have given consent" is a sentence an owner can act on, and it is the same
    count the send path applies a moment later. Saving a rule silently changes what forty people get
    on their phones, so the screen says so instead of "Saved."
    """
    organization = request.organization
    editing = organization.channel_rules.filter(pk=rule_id).first() if rule_id else None
    if rule_id and not editing:
        raise Http404
    form = ChannelRuleForm(request.POST or None, instance=editing or ChannelRule(organization=organization))
    if form.is_valid():
        # The actor is stamped here rather than on the form: the page that writes a rule is the only
        # thing that knows who pressed the button, and "who decided officers get texted" is a question
        # the row has to keep answering after the audit chain has moved on — the same reason
        # `TimePolicyOverride.authorized_by` exists.
        form.instance.created_by = request.user
        rule = form.save()
        AuditEvent.objects.create(organization=organization, actor=request.user,
            action="message.rule_updated", target_type="channel_rule", target_id=str(rule.pk),
            metadata={"audience": rule.audience, "scope": rule.scope_label, "channels": rule.channels,
                      "created": editing is None})
        reach = (audience_reach(organization, rule.audience, Notification.Channel.SMS)
                 if Notification.Channel.SMS in rule.channels else None)
        messages.success(request, "Notices for {} about {} now go to {}.".format(
            rule.get_audience_display().lower(), rule.scope_label,
            ", ".join(dict(Notification.Channel.choices)[channel] for channel in rule.channels) or "the app only")
            + (f" {reach} of that audience have given text consent." if reach is not None else ""))
        return redirect("messaging_settings")
    for error in form.errors.get("__all__", []):
        messages.error(request, error)
    if form.errors.get("channels") or form.errors.get("event_type") or form.errors.get("audience"):
        messages.error(request, "Check the rule: " + "; ".join(
            item for field in ("audience", "event_type", "channels") for item in form.errors.get(field, [])))
    return redirect("messaging_settings")

@require_POST
@membership_required(*PRIVILEGED)
@transaction.atomic
def messaging_rule_remove(request, rule_id):
    """Take one rule back down, which returns that audience to whatever the code asks for."""
    organization = request.organization
    rule = organization.channel_rules.filter(pk=rule_id).first()
    if not rule:
        raise Http404
    label, scope = rule.get_audience_display(), rule.scope_label
    rule.delete()
    AuditEvent.objects.create(organization=organization, actor=request.user,
        action="message.rule_removed", target_type="channel_rule", target_id=str(rule_id),
        metadata={"audience": label, "scope": scope})
    messages.success(request, f"{label} notices about {scope} go back to the default channels.")
    return redirect("messaging_settings")


def _sms_rows(organization, *, base_url=None):
    from .models import SmsTemplate
    from . import sms as sms_text
    custom = dict(SmsTemplate.objects.filter(organization=organization).values_list("notice_key", "body"))
    groups = {}
    for notice in sms_text.SMS_NOTICES.values():
        wording = custom.get(notice.key) or notice.default
        preview = sms_text.preview_sms(organization, notice, wording, base_url=base_url)
        groups.setdefault(sms_text.FAMILY_LABELS.get(notice.family, "Other"), []).append({
            "notice": notice, "wording": wording, "custom": notice.key in custom,
            "preview": preview, "size": sms_text.segment_info(preview)})
    return groups


def _wording_catalog(request):
    from . import email_wording, sms as sms_text
    from .models import EmailTemplate
    organization = request.organization
    base_url = sms_text.public_base_url(organization)
    emails = {row.notice_key: row for row in EmailTemplate.objects.filter(organization=organization)}
    texts = {row["notice"].key: row for rows in _sms_rows(organization, base_url=base_url).values() for row in rows}
    rows = []
    for notice in email_wording.EMAIL_NOTICES.values():
        custom = emails.get(notice.key)
        subject, body = email_wording.preview(organization, notice,
            custom.subject if custom else email_wording.DEFAULT_SUBJECT,
            custom.body if custom else email_wording.DEFAULT_BODY, base_url=base_url)
        rows.append({"notice": notice, "email_custom": custom is not None,
                     "email_subject": subject, "email_preview": body, "sms": texts.get(notice.key)})
    query = request.GET.get("q", "").strip()
    channel = request.GET.get("channel", "")
    status = request.GET.get("status", "")
    family = request.GET.get("family", "")
    filtered = []
    for row in rows:
        notice = row["notice"]
        if query and query.casefold() not in f"{notice.key} {notice.label} {notice.audience}".casefold():
            continue
        if channel == "sms" and row["sms"] is None:
            continue
        if family and family != notice.family:
            continue
        custom = (row["sms"]["custom"] if row["sms"] else False) if channel == "sms" else row["email_custom"] if channel == "email" else (
            row["email_custom"] or bool(row["sms"] and row["sms"]["custom"]))
        if status == "custom" and not custom or status == "default" and custom:
            continue
        filtered.append(row)
    return {"catalog_rows": filtered, "catalog_total": len(rows), "query": query,
            "channel_filter": channel, "status_filter": status, "family_filter": family,
            "families": sorted({(row["notice"].family,
                sms_text.FAMILY_LABELS.get(row["notice"].family, "Accounts")) for row in rows}),
            "prefix": sms_text.company_prefix(organization),
            "base_url": base_url}


@membership_required(*PRIVILEGED)
def sms_templates(request):
    """One catalog for company email and SMS wording; legacy route names remain valid."""
    return render(request, "core/sms_templates.html", _wording_catalog(request))


@membership_required(*PRIVILEGED)
@transaction.atomic
def email_template_edit(request, notice_key):
    from . import email_wording
    from .models import EmailTemplate
    notice = email_wording.notice_for(notice_key)
    if notice is None:
        raise Http404
    organization = request.organization
    row = organization.email_templates.filter(notice_key=notice.key).first()
    subject = row.subject if row else email_wording.DEFAULT_SUBJECT
    body = row.body if row else email_wording.DEFAULT_BODY
    errors = []
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "reset":
            if row:
                previous = {"subject": row.subject, "body": row.body}
                row.delete()
                AuditEvent.objects.create(organization=organization, actor=request.user,
                    action="message.email_template_reset", target_type="email_template",
                    target_id=notice.key, metadata={"notice": notice.key, "previous": previous})
            messages.success(request, f"{notice.label} emails use the original workflow wording again.")
            return redirect("sms_templates")
        subject = request.POST.get("subject", "").strip()
        body = request.POST.get("body", "").replace("\r\n", "\n").strip()
        errors = email_wording.validate_wording(notice, subject, body)
        if action not in ("save", "preview"):
            errors.append("Choose Save wording or Preview.")
        if not errors and action == "save":
            previous = {"subject": row.subject, "body": row.body} if row else None
            if (subject, body) == (email_wording.DEFAULT_SUBJECT, email_wording.DEFAULT_BODY):
                if row:
                    row.delete()
            else:
                EmailTemplate.objects.update_or_create(organization=organization, notice_key=notice.key,
                    defaults={"subject": subject, "body": body, "updated_by": request.user})
            AuditEvent.objects.create(organization=organization, actor=request.user,
                action="message.email_template_updated", target_type="email_template", target_id=notice.key,
                metadata={"notice": notice.key, "previous": previous, "subject": subject, "body": body})
            messages.success(request, f"Saved {notice.label} email wording. Newly queued emails will use it.")
            return redirect("sms_templates")
    samples = email_wording.sample_values(organization, notice)
    preview_subject, preview_body = email_wording.preview(organization, notice, subject, body)
    return render(request, "core/sms_templates.html", {
        **_wording_catalog(request), "editor_channel": "email", "notice": notice,
        "subject_wording": subject, "wording": body, "errors": errors, "custom": row is not None,
        "preview_subject": preview_subject, "preview": preview_body, "samples": samples,
        "placeholders": [(name, email_wording.PLACEHOLDERS[name], samples.get(name, ""))
                         for name in notice.allowed],
        "default_subject": email_wording.DEFAULT_SUBJECT, "default": email_wording.DEFAULT_BODY,
        "max_subject_length": email_wording.MAX_SUBJECT_LENGTH, "max_length": email_wording.MAX_BODY_LENGTH})


@membership_required(*PRIVILEGED)
@transaction.atomic
def sms_template_edit(request, notice_key):
    """Reword one text, with the placeholders it may use and a preview of what a phone shows."""
    from .models import SmsTemplate
    from . import sms as sms_text
    organization = request.organization
    notice = sms_text.notice_for(notice_key)
    if notice is None:
        raise Http404
    row = SmsTemplate.objects.filter(organization=organization, notice_key=notice.key).first()
    wording = row.body if row else notice.default
    errors = []
    if request.method == "POST":
        if request.POST.get("action") == "reset":
            if row:
                row.delete()
                AuditEvent.objects.create(organization=organization, actor=request.user,
                    action="message.sms_template_reset", target_type="sms_template", target_id=notice.key,
                    metadata={"notice": notice.key, "previous": row.body})
            messages.success(request, f"“{notice.label}” texts use the built-in wording again.")
            return redirect("sms_templates")
        wording = (request.POST.get("body") or "").replace("\r\n", "\n").strip()
        errors = sms_text.validate_template(notice, wording)
        action = request.POST.get("action", "save")
        if action not in ("save", "preview"):
            errors.append("Choose Save wording or Preview.")
        if not errors and action == "save":
            previous = row.body if row else None
            if wording == notice.default:
                if row:
                    row.delete()
            else:
                SmsTemplate.objects.update_or_create(organization=organization, notice_key=notice.key,
                    defaults={"body": wording, "updated_by": request.user})
            AuditEvent.objects.create(organization=organization, actor=request.user,
                action="message.sms_template_updated", target_type="sms_template", target_id=notice.key,
                metadata={"notice": notice.key, "previous": previous, "body": wording})
            preview = sms_text.preview_sms(organization, notice, wording)
            size = sms_text.segment_info(preview)
            messages.success(request, f"Saved. A typical “{notice.label}” text is now {size['characters']} "
                f"characters ({size['segments']} segment{'s' if size['segments'] != 1 else ''}).")
            return redirect("sms_templates")
    preview = sms_text.preview_sms(organization, notice, wording)
    return render(request, "core/sms_templates.html", {
        **_wording_catalog(request), "editor_channel": "sms",
        "notice": notice, "wording": wording, "default": notice.default, "custom": row is not None,
        "errors": errors, "preview": preview, "size": sms_text.segment_info(preview),
        "prefix": sms_text.company_prefix(organization), "base_url": sms_text.public_base_url(organization),
        "placeholders": [(name, sms_text.PLACEHOLDERS[name], sms_text.SAMPLE_VALUES.get(name, ""))
                         for name in notice.allowed],
        "link_sample": sms_text.sample_link(organization, notice),
        "samples": {**{name: sms_text.SAMPLE_VALUES.get(name, "") for name in notice.allowed},
                    "link": sms_text.sample_link(organization, notice)},
        "max_length": sms_text.MAX_TEMPLATE_LENGTH})

@require_POST
@membership_required(*PRIVILEGED)
@transaction.atomic
def messaging_rotate_token(request):
    organization = request.organization
    rotate_webhook_token(organization, request.user)
    messages.success(request, "New webhook URLs issued for all providers. Every old URL is now invalid; "
                              "update Twilio, Mailjet, Postmark, and SNS wherever configured.")
    return redirect("messaging_settings")

@require_POST
@membership_required(*PRIVILEGED)
def messaging_confirm_subscription(request, event_id):
    """Confirm one pending SNS subscription that arrived on this company's own callback address.

    The row is resolved through `organization.delivery_events`, so an id belonging to another tenant is
    a 404 rather than somebody else's confirmation click, and the allow-list check happens inside the
    service before any socket is opened. Nothing here prints the address: it carries a token.

    No `transaction.atomic` on this view, on purpose. The refusal path in the service *wants* its
    record of the attempt to stand, and a view-level transaction would erase it the moment the
    `ValidationError` was caught below — a failed click that leaves no trace is the one outcome an
    operator chasing a missing bounce must not be left with.
    """
    organization = request.organization
    event = organization.delivery_events.filter(pk=event_id).first()
    if event is None:
        raise Http404
    try:
        confirm_sns_subscription(organization, event, request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("messaging_settings")
    messages.success(request, "Amazon confirmed the subscription. Email events for that topic will now "
                              "arrive on the callback address.")
    return redirect("messaging_settings")

# The callback endpoint is device-token-free by design: a provider posts a form or a JSON body with no
# session and no CSRF token, so the address itself carries the tenant identity and the provider's own
# signature, where it has one, carries the proof. Both are checked; neither is faked.
CALLBACK_PROVIDERS = {"twilio": Organization.SmsProvider.TWILIO, "mailjet": Organization.EmailProvider.MAILJET,
                      "postmark": Organization.EmailProvider.POSTMARK, "sns": "sns"}

@csrf_exempt
@require_POST
def provider_callback(request, provider, token):
    """Take one provider's callback, retain it, and act on what it changes.

    A wrong or missing token is a 404 with no body: this endpoint's existence is not information for
    somebody guessing addresses, and a "that company has no webhook configured" answer tells them the
    rest. Anything it does post is refused before it can touch state.
    """
    organization = Organization.objects.filter(webhook_token=token).first()
    mapped = CALLBACK_PROVIDERS.get(provider)
    if organization is None or mapped is None:
        return HttpResponse(status=404)
    # A body may be read once. Twilio, Mailjet and Postmark post forms; SNS posts JSON whose envelope
    # carries a second JSON document inside it — so the content type picks the reader and the two are
    # never both touched on one request. Reading `request.POST` first and `request.body` afterwards
    # raises RawPostDataException, which arrives as a 500 on a provider's retry and looks like a bug
    # in whichever adapter happened to fire first.
    params = {}
    payload = {}
    if "json" in (request.content_type or "").lower():
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            # A body that is neither valid JSON nor a form is retained as text rather than discarded,
            # because "we lost the bounce" and "we could not read the bounce" are different answers to
            # give an owner six months later.
            payload = {"unparsed": request.body[:2000].decode("utf-8", "replace")}
    else:
        params = {key: request.POST.getlist(key)[0] for key in request.POST}
    signed = callback_is_signed(mapped, request, params)
    if callback_requires_signature(mapped) and not signed:
        # Configured to verify and it did not verify: refuse, and say nothing about why.
        AuditEvent.objects.create(organization=organization, actor=None, action="message.callback_refused",
            target_type="organization", target_id=str(organization.pk), metadata={"provider": provider})
        return HttpResponse(status=403)
    if str(payload.get("Type") or "") == "SubscriptionConfirmation":
        # AWS will not deliver notifications until the subscription is confirmed, and confirming means
        # making a request to a URL that arrived inside the payload — an SSRF primitive if followed
        # blindly. The endpoint therefore stores it for a human, and the confirmation button on the
        # messaging page re-checks the host against the SNS allow-list before anything is fetched.
        event = DeliveryEvent.objects.create(organization=organization, provider=provider,
            channel=MessageConsent.Channel.EMAIL,
            destination=str((payload.get("TopicArn") or "").split(":")[-1] or "sns"),
            kind=DeliveryEvent.Kind.STATUS, detail="Subscription confirmation pending an owner's click",
            message_reference=str(payload.get("MessageId") or ""), applied=False, verified=signed,
            raw={"subscribe_url_host": str(payload.get("SubscribeURL") or "").split("/")[2]
                 if "://" in str(payload.get("SubscribeURL") or "") else "",
                 "subscribe_url": str(payload.get("SubscribeURL") or "")[:300], "topic": payload.get("TopicArn")})
        return JsonResponse({"stored": True, "event": str(event.pk), "confirm": "messaging_settings"}, status=202)
    events = normalize_provider_callback(provider, params=params, payload=payload)
    reply = ""
    for event in events:
        if event["kind"] == DeliveryEvent.Kind.INBOUND:
            reply = handle_inbound_message(organization, event["destination"], event["detail"],
                provider=provider, verified=signed)
            event["inbound_handled"] = True
    stored = ingest_provider_events(organization, provider,
        [event for event in events if event["kind"] != DeliveryEvent.Kind.INBOUND],
        verified=signed, raw={"params": params, "payload": payload} if len(
            json.dumps({"params": params, "payload": payload}, default=str)) < 6000 else {})
    if reply:
        return HttpResponse(twiml_reply(reply), content_type="application/xml")
    return JsonResponse({"stored": len(stored), "applied": sum(1 for row in stored if row.applied)})

def _time_review_run(request):
    run_id = request.GET.get("run")
    if not run_id:
        return None
    if request.membership.role not in PAYROLL:
        raise Http404
    try:
        run_id = uuid.UUID(run_id)
    except (TypeError, ValueError, AttributeError):
        raise Http404
    run = request.organization.payroll_runs.filter(pk=run_id).first()
    if run is None:
        raise Http404
    return run


def _time_review_query(request, run=None):
    from urllib.parse import urlencode
    params = {
        name: request.GET.get(name) if request.GET.get(name) in ("pending", "history", "all") else "pending"
        for name in ("punches", "adjustments")
    }
    for name in ("punch_page", "adjustment_page"):
        value = request.GET.get(name, "")
        if value.isdigit():
            params[name] = value
    for name in ("person", "site", "start", "end", "view", "week"):
        if request.GET.get(name):
            params[name] = request.GET[name]
    if run is not None:
        params["run"] = str(run.pk)
    return urlencode(params)


@membership_required(*TIME_REVIEWERS)
@never_cache
def time_review(request):
    scope=scope_for(request)
    from .time_workflow import pending_time_review_counts
    run = _time_review_run(request)

    filter_choices = {"pending", "history", "all"}
    punch_filter = request.GET.get("punches", "pending")
    adjustment_filter = request.GET.get("adjustments", "pending")
    if punch_filter not in filter_choices:
        punch_filter = "pending"
        messages.error(request, "Unknown punch filter; showing pending punches.")
    if adjustment_filter not in filter_choices:
        adjustment_filter = "pending"
        messages.error(request, "Unknown correction filter; showing pending requests.")

    punch_queryset = scope.filter_punches(
        request.organization.punches.select_related(
            "person", "shift__site", "selfie__document_type"
        )
    ).order_by("-occurred_at")
    from .timesheet_views import filtered_punches, timesheet_context
    sheet_queryset = filtered_punches(request, punch_queryset, run)
    sheet_context = timesheet_context(request, sheet_queryset, run)
    review_window_filtered = any(request.GET.get(name) for name in ("person", "site", "start", "end", "week"))
    if review_window_filtered:
        punch_queryset = sheet_queryset
    if run is not None:
        punch_queryset = punch_queryset.filter(
            occurred_at__gte=run.period_start, occurred_at__lt=run.period_end,
        )
    if punch_filter == "pending":
        punch_queryset = punch_queryset.filter(review_status=Punch.Review.PENDING)
    elif punch_filter == "history":
        punch_queryset = punch_queryset.exclude(review_status=Punch.Review.PENDING)
    punches = Paginator(punch_queryset, PAGE_SIZE).get_page(request.GET.get("punch_page", request.GET.get("page")))
    # CLK-2. A reviewer deciding whether to accept a punch needs to know it came off a shared pad, and
    # needs the station's *name* rather than a uuid they cannot interpret. One query for the page (a
    # device_id may equally be an offline device, which simply is not in this map), attached to the
    # evaluated rows — `object_list` is replaced with the list so the template iterates the very
    # objects annotated here instead of re-running the query for fresh instances.
    rows=list(punches.object_list)
    stations={str(kiosk.pk):kiosk.name for kiosk in ClockKiosk.objects.filter(organization=request.organization,
            pk__in=[row.device_id for row in rows if row.device_id])}
    for row in rows:
        row.station_name=stations.get(str(row.device_id)) if row.device_id else None
        # CLK-1. A frame is a personal record with its own disclosure rung, so the review list asks the
        # same question the open route will: can *this* reader see *that* photo. Linking a biometric the
        # route would refuse is the defect the personnel export already rules out — announcing that a
        # face exists for this officer is itself a disclosure, and an auditor reading punch rows must
        # learn nothing from its absence or presence. So the cell says "photo held" only to readers who
        # can open it, and says nothing at all to the rest.
        row.selfie_visible=bool(row.selfie_id) and _record_open(request, row.selfie)
    punches.object_list=rows
    adjustment_queryset = scope.filter_adjustments(
        request.organization.punch_adjustments.select_related(
            "punch__person", "requested_by", "reviewed_by"
        )
    ).order_by("-created_at")
    if review_window_filtered:
        adjustment_queryset = adjustment_queryset.filter(punch_id__in=sheet_queryset.values("pk"))
    if run is not None:
        adjustment_queryset = adjustment_queryset.filter(
            punch__occurred_at__gte=run.period_start, punch__occurred_at__lt=run.period_end,
        )
    if adjustment_filter == "pending":
        adjustment_queryset = adjustment_queryset.filter(status=PunchAdjustment.Status.REQUESTED)
    elif adjustment_filter == "history":
        adjustment_queryset = adjustment_queryset.exclude(status=PunchAdjustment.Status.REQUESTED)
    adjustments = Paginator(adjustment_queryset, PAGE_SIZE).get_page(
        request.GET.get("adjustment_page")
    )
    pending_counts = pending_time_review_counts(request.organization, scope, run=run)
    if review_window_filtered:
        pending_counts["punches"] = sheet_queryset.filter(review_status=Punch.Review.PENDING).count()
        pending_counts["corrections"] = scope.filter_adjustments(request.organization.punch_adjustments.filter(
            status=PunchAdjustment.Status.REQUESTED, punch_id__in=sheet_queryset.values("pk"))).count()
    return render(request, "core/timesheets.html", {
        **sheet_context,
        "punches": punches,
        "adjustments": adjustments,
        "punch_filter": punch_filter,
        "adjustment_filter": adjustment_filter,
        "pending_count": pending_counts["punches"],
        "pending_corrections_count": pending_counts["corrections"],
        "selected_run": run, "review_query": _time_review_query(request, run),
        "authority_scope": scope if scope.restricted else None,
        "review_window_filtered": review_window_filtered,
    })

@require_POST
@membership_required(*TIME_REVIEWERS)
@transaction.atomic
def punch_review(request,punch_id):
    run = _time_review_run(request)
    return_url = f"{reverse('time_review')}?{_time_review_query(request, run)}"
    punch=request.organization.punches.select_related("person","shift__site").filter(pk=punch_id).first()
    if not punch: raise Http404
    if not scope_for(request).permits_punch(punch):
        # Reviewing a punch is the one place where an out-of-scope approval would quietly
        # accept time evidence the actor's own company does not answer for.
        raise Http404
    state = payroll_lock_state(request.organization, punch.occurred_at, *lock_subject(punch))
    if state["locked"]:
        messages.error(request, f"This payroll period is locked — {state['by']}.")
        return redirect(return_url)
    action=request.POST.get("action"); reason=request.POST.get("reason","").strip()
    if action not in (Punch.Review.ACCEPTED,Punch.Review.REJECTED) or (action==Punch.Review.REJECTED and len(reason)<5):
        messages.error(request,"Choose approve/reject and provide a rejection reason.")
        return redirect(return_url)
    before=punch.review_status;punch.review_status=action;punch.exception_reason=reason;punch.save(update_fields=["review_status","exception_reason"])
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="punch.reviewed",target_type="punch",target_id=str(punch.pk),metadata={"before":before,"after":action,"reason":reason})
    from .timekeeping import invalidate_drafts
    invalidate_drafts(punch)
    messages.success(request,"Punch review saved.")
    return redirect(return_url)

@membership_required()
@transaction.atomic
def adjustment_request(request,punch_id):
    person=request.organization.people.filter(user=request.user).first(); punch=request.organization.punches.filter(pk=punch_id,person=person).first()
    if not punch: raise Http404
    # PAY-4: asked of the slice this punch sits in, not of the period as a whole — a correction inside
    # a branch the firm deliberately left open is exactly what partial locking is for.
    state = payroll_lock_state(request.organization, punch.occurred_at, *lock_subject(punch))
    if state["locked"]: messages.error(request,f"This payroll period is locked — {state['by']}."); return redirect("clock")
    form=PunchAdjustmentForm(request.POST or None,initial={"proposed_at":punch.occurred_at})
    if request.method=="POST" and form.is_valid():
        item=PunchAdjustment.objects.create(organization=request.organization,punch=punch,requested_by=request.user,proposed_at=form.cleaned_data["proposed_at"],reason=form.cleaned_data["reason"])
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="punch_adjustment.requested",target_type="punch_adjustment",target_id=str(item.pk),metadata={"punch":str(punch.pk)})
        # NTF-3: the correction sits in /time/review/ until a reviewer opens it, and a guard whose
        # hours are wrong has no way to know the request is not being ignored. The reviewer list is
        # the dispatcher whose authority reaches the post plus the payroll approver, who is the only
        # role that can settle a timecard before it is billed.
        reviewers = set(role_recipients(request.organization, TIME_REVIEWERS))
        if punch.shift_id:
            reviewers |= dispatch_recipients_for_shift(punch.shift, person)
        queue_notice(organization=request.organization,recipients=reviewers-{request.user.pk},
            event_type="punch.correction_requested",
            subject=f"Time correction from {person.full_name}",
            body=(f"{person} asked to move a {punch.get_kind_display().lower()} on "
                  f"{moment_label(request.organization, punch.occurred_at)} to {moment_label(request.organization, item.proposed_at)}."
                  f" Reason: {item.reason}"),
            dedup_key=f"punch.correction:{item.pk}",
            sms={"officer": person, "kind": punch.get_kind_display().lower(), "at": item.proposed_at,
                 "note": item.reason})
        messages.success(request,"Correction requested."); return redirect("clock")
    return render(request,"core/form.html",{"form":form,"title":"Request time correction","eyebrow":"Timekeeping"})

@require_POST
@membership_required(*TIME_REVIEWERS)
@transaction.atomic
def adjustment_review(request,adjustment_id):
    run = _time_review_run(request)
    return_url = f"{reverse('time_review')}?{_time_review_query(request, run)}"
    item=request.organization.punch_adjustments.select_related("punch__person","punch__shift__site").filter(pk=adjustment_id,status=PunchAdjustment.Status.REQUESTED).first()
    if not item: raise Http404
    if not scope_for(request).permits_punch(item.punch): raise Http404
    status=request.POST.get("action"); note=request.POST.get("note","").strip()
    if status not in (PunchAdjustment.Status.APPROVED,PunchAdjustment.Status.REJECTED):
        messages.error(request,"Invalid review action.")
        return redirect(return_url)
    state = payroll_lock_state(request.organization, item.punch.occurred_at, *lock_subject(item.punch))
    if state["locked"]:
        messages.error(request,f"This payroll period is locked — {state['by']}.")
        return redirect(return_url)
    if status == PunchAdjustment.Status.APPROVED and payroll_lock_state(
        request.organization, item.proposed_at, *lock_subject(item.punch))["locked"]:
        messages.error(request, "The corrected time falls in a locked payroll period.")
        return redirect(return_url)
    item.status=status;item.reviewed_by=request.user;item.reviewed_at=timezone.now();item.review_note=note;item.save(update_fields=["status","reviewed_by","reviewed_at","review_note"])
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="punch_adjustment.reviewed",target_type="punch_adjustment",target_id=str(item.pk),metadata={"status":status,"note":note})
    if status == PunchAdjustment.Status.APPROVED:
        from .timekeeping import invalidate_drafts
        invalidate_drafts(item.punch)
    # The officer is the only party who does not have a screen to check, so the outcome is pushed.
    queue_notice(organization=request.organization,recipients={item.requested_by_id}-{request.user.pk},
        event_type=f"punch.correction_{status}",
        subject=f"Time correction {'approved' if status==PunchAdjustment.Status.APPROVED else 'declined'}",
        body=(f"Your correction for the {item.punch.get_kind_display().lower()} on "
              f"{moment_label(request.organization, item.punch.occurred_at)} was "
              f"{'approved at ' + moment_label(request.organization, item.proposed_at) if status==PunchAdjustment.Status.APPROVED else 'declined'}."
              + (f" Note: {note}" if note else "")),
        dedup_key=f"punch.correction:{item.pk}:{status}",
        sms={"kind": item.punch.get_kind_display().lower(), "at": item.proposed_at, "note": note})
    messages.success(request,"Correction review saved.")
    return redirect(return_url)

@membership_required(*PAYROLL)
def payroll(request):
    organization = request.organization
    requested_run_id = request.GET.get("run")
    run_queryset = organization.payroll_runs.select_related(
        "approved_by", "reopened_by"
    ).prefetch_related(
        "lock_segments__branch", "lock_segments__client", "lock_segments__decided_by"
    )
    if requested_run_id:
        try:
            requested_run_id = uuid.UUID(requested_run_id)
        except (TypeError, ValueError, AttributeError):
            raise Http404
        selected_run = run_queryset.filter(pk=requested_run_id).first()
        if selected_run is None:
            raise Http404
    else:
        selected_run = run_queryset.first()

    initial = {}
    if selected_run and selected_run.status == PayrollRun.Status.DRAFT:
        initial = {
            "period_start": timezone.localtime(selected_run.period_start),
            "period_end": timezone.localtime(selected_run.period_end),
        }
    form=PayrollPeriodForm(request.POST or None, initial=initial)
    if request.method=="POST" and form.is_valid():
        run=create_payroll_run(organization=organization,start=form.cleaned_data["period_start"],end=form.cleaned_data["period_end"],actor=request.user)
        if run.status == PayrollRun.Status.DRAFT:
            messages.success(request,"Payroll draft refreshed from the selected period.")
        else:
            messages.warning(request,"This period is already locked; its approved snapshot was not changed.")
        return redirect(f"{reverse('payroll')}?run={run.pk}")
    policy=TimePolicy.objects.filter(organization=organization).first()
    runs = list(run_queryset[:30])
    if selected_run is None:
        selected_run = runs[0] if runs else None
    elif all(run.pk != selected_run.pk for run in runs):
        runs.insert(0, selected_run)
    snapshot = selected_run.snapshot if selected_run else []
    pending_punch_count = pending_correction_count = 0
    exception_rows = []
    open_segments = []
    if selected_run:
        period_punches = organization.punches.filter(
            occurred_at__gte=selected_run.period_start,
            occurred_at__lt=selected_run.period_end,
        )
        pending_punch_count = period_punches.filter(
            review_status=Punch.Review.PENDING
        ).count()
        pending_correction_count = organization.punch_adjustments.filter(
            punch__occurred_at__gte=selected_run.period_start,
            punch__occurred_at__lt=selected_run.period_end,
            status=PunchAdjustment.Status.REQUESTED,
        ).count()
        open_segments = open_lock_segments(selected_run)
        for exception in selected_run.exceptions or []:
            reason = str(exception.get("reason", ""))
            lower_reason = reason.lower()
            if "await review" in lower_reason:
                destination = f"{reverse('time_review')}?punches=pending&adjustments=pending&run={selected_run.pk}"
                destination_label = "Review pending time"
            elif "reopen" in lower_reason or "regenerate" in lower_reason:
                destination = "#generate-draft"
                destination_label = "Regenerate this period"
            elif any(word in lower_reason for word in ("category", "leave", "designated")):
                destination = reverse("settings_pay_categories")
                destination_label = "Review hour categories"
            elif "hold" in lower_reason:
                destination = reverse("schedule")
                destination_label = "Review the schedule"
            else:
                destination = f"{reverse('time_review')}?punches=all&adjustments=all&run={selected_run.pk}"
                destination_label = "Review time evidence"
            exception_rows.append({
                "employee": exception.get("employee", ""),
                "reason": reason,
                "destination": destination,
                "destination_label": destination_label,
            })
    approval_blocked = bool(
        selected_run
        and (selected_run.exceptions or pending_punch_count or pending_correction_count)
    )
    stage = (
        selected_run.status if selected_run else "generate"
    )
    # The selected run's generated snapshot is the source for both the page and its export.
    return render(request,"core/payroll.html",{
        "form":form,"runs":runs,
        # The picker for a lock slice: whoever decides "which part is agreed" chooses from the same two
        # axes the rest of the product divides work by, rather than typing a name.
        "lock_branches": organization.branches.filter(active=True).order_by("name"),
        "lock_clients": organization.clients.order_by("name"),
        "lock_statuses": PayrollLockSegment.Status.choices,
        "can_reopen":bool(policy and policy.allow_reopen),"privileged":request.membership.role in PRIVILEGED,
        "selected_run":selected_run,"latest":selected_run,"stage":stage,
        "exception_rows":exception_rows,"pending_punch_count":pending_punch_count,
        "pending_correction_count":pending_correction_count,"open_segments":open_segments,
        "approval_blocked":approval_blocked,
        "by_pay_code":payroll_totals(snapshot,by="pay_code"),
        "by_category":payroll_totals(snapshot,by="pay_category"),
    })

@require_POST
@membership_required(*PRIVILEGED)
def payroll_reopen(request,run_id):
    """Unlock a locked period so corrections and late punches can be reviewed again.

    The payroll approver who locked it is deliberately not the role that unlocks it: reopening
    undoes somebody else's approval, so it belongs to the owner or administrator who chose to
    allow reopening in policy at all. The reason is kept on the row and in the audit chain.
    """
    run=request.organization.payroll_runs.filter(pk=run_id).first()
    if not run: raise Http404
    try: reopen_payroll_run(run,request.user,request.POST.get("reason",""))
    except ValidationError as exc: messages.error(request,str(exc))
    else: messages.warning(request,f"{run.period_start:%b %d} – {run.period_end:%b %d} reopened. Generate the draft again to pick up the corrected time, then approve it.")
    return redirect(f"{reverse('payroll')}?run={run.pk}")

@require_POST
@membership_required(*(MANAGERS + PAYROLL))
@transaction.atomic
def payroll_segment_lock(request, run_id):
    """Lock or re-open one branch's or one contract's slice of a period. PAY-4.

    The two directions are not the same act, so they are not the same permission: agreeing a slice is
    the payroll approver's job, while un-fixing part of an approved period undoes somebody's approval
    and stays with owner/admin, which is the line the whole-period reopen already draws.

    Doing it a slice at a time is the point. A firm that pays two branches on Thursday and is still
    chasing a missing punch for the third used to have to unlock the two to correct the one — and a
    period that is universally editable is not a locked period, it is a draft with a stamp on it.
    """
    org = request.organization
    run = org.payroll_runs.filter(pk=run_id).first()
    if run is None:
        raise Http404
    status = request.POST.get("status")
    if status == PayrollLockSegment.Status.OPEN and request.membership.role not in PRIVILEGED:
        messages.error(request, "Only an owner or administrator can open part of an approved period.")
        return redirect(f"{reverse('payroll')}?run={run.pk}")
    # One select, prefixed by axis: an operator picking "which slice" should not have to know that a
    # branch and a contract live in different tables, and empty means the catch-all slice that covers
    # everywhere the named ones do not.
    subject = (request.POST.get("subject") or "").strip()
    branch = client = None
    if subject[:1] in ("b", "c") and len(subject) > 1:
        wanted = subject[1:]
        if subject[0] == "b":
            branch = org.branches.filter(pk=wanted).first()
            if branch is None:
                messages.error(request, "That branch is not in this company.")
                return redirect(f"{reverse('payroll')}?run={run.pk}")
        else:
            client = org.clients.filter(pk=wanted).first()
            if client is None:
                messages.error(request, "That contract is not in this company.")
                return redirect(f"{reverse('payroll')}?run={run.pk}")
    try:
        row = set_payroll_lock_segment(run, request.user, status, request.POST.get("reason", ""),
                                       branch=branch, client=client)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect(f"{reverse('payroll')}?run={run.pk}")
    messages.success(request, f"{row.subject_label} is {row.get_status_display().lower()} for "
                              f"{run.period_start:%b %d} – {run.period_end:%b %d}. "
                              "The rest of the period keeps the run's own status.")
    return redirect(f"{reverse('payroll')}?run={run.pk}")

@require_POST
@membership_required(*PAYROLL)
def payroll_approve(request,run_id):
    run=request.organization.payroll_runs.filter(pk=run_id).first()
    if not run: raise Http404
    pending_punches = request.organization.punches.filter(
        occurred_at__gte=run.period_start,
        occurred_at__lt=run.period_end,
        review_status=Punch.Review.PENDING,
    ).count()
    pending_corrections = request.organization.punch_adjustments.filter(
        punch__occurred_at__gte=run.period_start,
        punch__occurred_at__lt=run.period_end,
        status=PunchAdjustment.Status.REQUESTED,
    ).count()
    if pending_punches or pending_corrections:
        messages.error(
            request,
            f"Resolve {pending_punches} pending punch(es) and {pending_corrections} "
            "correction request(s) for this period before approval.",
        )
    else:
        try: approve_payroll_run(run,request.user)
        except ValidationError as exc: messages.error(request,str(exc))
        else: messages.success(request,"Payroll approved and locked.")
    return redirect(f"{reverse('payroll')}?run={run.pk}")

@membership_required(*PAYROLL)
@transaction.atomic
def payroll_run_export(request,run_id):
    run=request.organization.payroll_runs.filter(pk=run_id,status__in=[PayrollRun.Status.APPROVED,PayrollRun.Status.EXPORTED]).first()
    if not run: raise Http404
    # PAY-4: a partial lock makes one new mistake possible — handing the customer a file while part of
    # the period is still being corrected. So the open slices are named before anything is exported,
    # and "some of it is still moving" is the information the operator needs to go close them first.
    pending = open_lock_segments(run)
    if pending:
        messages.error(request, "This period cannot be exported while a slice of it is open for correction: "
                                + ", ".join(item.subject_label for item in pending) + ".")
        return redirect(f"{reverse('payroll')}?run={run.pk}")
    format=request.GET.get("format","csv").lower()
    exporters={"csv":(payroll_snapshot_csv,"text/csv"),"xlsx":(payroll_snapshot_xlsx,"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),"pdf":(payroll_snapshot_pdf,"application/pdf")}
    if format not in exporters: raise Http404
    exporter,content_type=exporters[format];output=exporter(run.snapshot)
    run.status=PayrollRun.Status.EXPORTED;run.exported_at=timezone.now();run.save(update_fields=["status","exported_at"])
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="payroll.exported",target_type="payroll_run",target_id=str(run.pk),metadata={"rows":len(run.snapshot),"format":format})
    # NTF-3 "payroll-export readiness": the file leaving this system is the fact the client's
    # payroll run was built from, so it is recorded as an event for the people who did not press
    # the button rather than only in the audit chain.
    queue_notice(organization=request.organization,recipients=set(payroll_recipients(request.organization))-{request.user.pk},
        event_type="payroll.exported",
        subject=f"Payroll exported for {run.period_start.date()} – {run.period_end.date()}",
        body=f"{request.user} downloaded the {format.upper()} for {len(run.snapshot)} employee row(s). The period is now marked exported.",
        dedup_key=f"payroll.exported:{run.pk}:{format}",
        sms={"period": (run.period_start, run.period_end), "actor": request.user, "count": len(run.snapshot)})
    response=HttpResponse(output,content_type=content_type);response["Content-Disposition"]=f'attachment; filename="payroll-{run.period_start.date()}-{run.period_end.date()}.{format}"';return response

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN)
def security_settings(request):
    form=OrganizationSecurityForm(request.POST or None,instance=request.organization)
    if request.method=="POST" and form.is_valid():
        before=request.organization.mfa_required_roles;before_domains=request.organization.approved_role_domains;form.save()
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="security.mfa_policy_updated",target_type="organization",target_id=str(request.organization.pk),metadata={"before":before,"after":request.organization.mfa_required_roles,
            "domains_before":before_domains,"domains_after":request.organization.approved_role_domains})
        messages.success(request,"Security policy updated.");return redirect("security_settings")
    return render(request,"core/security_settings.html",{"form":form})

@membership_required()
def tenant_select(request):
    memberships=request.user.organization_memberships.filter(active=True).select_related("organization")
    if request.method=="POST":
        membership=memberships.filter(organization_id=request.POST.get("organization_id")).first()
        if not membership: raise Http404
        request.session.cycle_key();request.session["active_organization_id"]=str(membership.organization_id)
        AuditEvent.objects.create(organization=membership.organization,actor=request.user,action="tenant.selected",target_type="organization",target_id=str(membership.organization_id))
        return redirect("dashboard")
    return render(request,"core/tenant_select.html",{"memberships":memberships})

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN)
def domains(request):
    import secrets
    form=DomainForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        item,created=OrganizationDomain.objects.get_or_create(hostname=form.cleaned_data["hostname"],defaults={"organization":request.organization,"verification_token":secrets.token_hex(24)})
        if not created and item.organization_id!=request.organization.id: form.add_error("hostname","This hostname is already claimed.")
        else:
            AuditEvent.objects.create(organization=request.organization,actor=request.user,action="domain.requested",target_type="organization_domain",target_id=str(item.pk),metadata={"hostname":item.hostname})
            messages.success(request,"Domain added. Publish the displayed DNS TXT record, then verify.");return redirect("domains")
    return render(request,"core/domains.html",{"form":form,"domains":request.organization.domains.all()})

@require_POST
@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN)
def domain_verify(request,domain_id):
    import dns.resolver
    item=request.organization.domains.filter(pk=domain_id).first()
    if not item: raise Http404
    try:
        answers=dns.resolver.resolve(f"_tscm-verification.{item.hostname}","TXT")
        values={part.decode() for answer in answers for part in answer.strings}
        if item.verification_token not in values: raise ValueError("Verification token was not found.")
    except Exception:
        item.status=OrganizationDomain.Status.FAILED;item.save(update_fields=["status"]);messages.error(request,"DNS verification failed. Confirm the TXT record and try again.")
    else:
        item.verified=True;item.status=OrganizationDomain.Status.VERIFIED;item.verified_at=timezone.now();item.save(update_fields=["verified","status","verified_at"])
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="domain.verified",target_type="organization_domain",target_id=str(item.pk),metadata={"hostname":item.hostname});messages.success(request,"Domain verified. Configure it in the deployment platform to provision TLS.")
    return redirect("domains")

@login_required
def platform_admin(request):
    if not request.user.is_superuser: raise Http404
    organizations=Organization.objects.annotate(member_count=__import__("django.db.models",fromlist=["Count"]).Count("memberships"),people_count=__import__("django.db.models",fromlist=["Count"]).Count("people",distinct=True)).order_by("display_name")
    return render(request,"core/platform_admin.html",{"organizations":organizations})

@membership_required(*AUDIT_READERS)
def audit_log(request):
    events=_page(request,request.organization.audit_events.select_related("actor").prefetch_related("redactions").order_by("-occurred_at","-id"))
    redactions=request.organization.audit_redactions.select_related("event","requested_by","approved_by")[:100]
    return render(request,"core/audit_log.html",{"events":events,"redactions":redactions,"paginator":events.paginator,
                                                 "is_paginated":events.has_other_pages(),"chain_errors":verify_audit_chain(request.organization),
                                                 "retention":audit_retention_state(request.organization),
                                                 "can_seal":request.membership.role in PRIVILEGED})

@membership_required(*AUDIT_READERS)
def audit_export(request):
    errors=verify_audit_chain(request.organization)
    response=HttpResponse(content_type="application/x-ndjson");response["Content-Disposition"]='attachment; filename="audit-export.ndjson"'
    for event in request.organization.audit_events.order_by("occurred_at","id").prefetch_related("redactions"):
        metadata=dict(event.metadata);redaction=event.redactions.filter(status=AuditRedaction.Status.APPLIED).first()
        if redaction:
            for field in redaction.fields:
                if field in metadata:metadata[field]="[REDACTED]"
        row={"id":str(event.pk),"occurred_at":event.occurred_at.isoformat(),"actor_id":event.actor_id,"action":event.action,"target_type":event.target_type,"target_id":event.target_id,"metadata":metadata,"previous_hash":event.previous_hash,"event_hash":event.event_hash,"redacted":bool(redaction),"chain_verified":str(event.pk) not in errors}
        response.write(json.dumps(row,sort_keys=True,default=str)+"\n")
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="audit.exported",target_type="organization",target_id=str(request.organization.pk),metadata={"event_count":request.organization.audit_events.count(),"chain_errors":len(errors)})
    response["Cache-Control"]="private, no-store";return response

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN)
@transaction.atomic
def audit_redact(request,event_id):
    event=request.organization.audit_events.filter(pk=event_id).first()
    if not event:raise Http404
    if request.organization.audit_redactions.filter(event=event).exists():
        messages.error(request,"This audit event already has a redaction request.");return redirect("audit_log")
    form=AuditRedactionForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        redaction=AuditRedaction.objects.create(organization=request.organization,event=event,fields=form.cleaned_data["fields"],reason=form.cleaned_data["reason"],legal_basis=form.cleaned_data["legal_basis"],requested_by=request.user)
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="audit.redaction_requested",target_type="audit_redaction",target_id=str(redaction.pk),metadata={"event_id":str(event.pk),"fields":redaction.fields,"reason":redaction.reason,"legal_basis":redaction.legal_basis})
        messages.success(request,"Redaction requested. A second owner or administrator must approve it before exports change.");return redirect("audit_log")
    return render(request,"core/form.html",{"form":form,"title":"Request audit export redaction","eyebrow":"Audit governance"})

@require_POST
@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN)
@transaction.atomic
def audit_redaction_decide(request,redaction_id):
    redaction=request.organization.audit_redactions.select_for_update().filter(pk=redaction_id,status=AuditRedaction.Status.PENDING).first()
    if not redaction:raise Http404
    if redaction.requested_by_id==request.user.pk:
        messages.error(request,"The requester cannot approve their own redaction.");return redirect("audit_log")
    action=request.POST.get("action")
    if action not in (AuditRedaction.Status.APPLIED,AuditRedaction.Status.REJECTED):
        messages.error(request,"Choose approve or reject.");return redirect("audit_log")
    redaction.status=action;redaction.approved_by=request.user;redaction.decided_at=timezone.now()
    redaction.save(update_fields=["status","approved_by","decided_at"])
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action=f"audit.redaction_{action}",target_type="audit_redaction",target_id=str(redaction.pk),metadata={"event_id":str(redaction.event_id),"fields":redaction.fields})
    messages.success(request,"Redaction decision recorded.");return redirect("audit_log")

# ── REC-4: the retention of the chain itself ─────────────────────────────────────
#
# Three routes, and the order between them is the design. Sealing writes an archive and records the head
# the chain continues from; purging is refused until that archive reads back exactly as it was written;
# the download exists so somebody can prove the history without the database holding it. All three are
# audited, and none of them is reachable by an auditor: the archive is the chain *as written*, so the
# redaction feature — which hides keys in exports and never touches a row — does not apply to it. Owners
# and administrators only, for exactly that reason.

@require_POST
@membership_required(*PRIVILEGED)
def audit_seal_run(request):
    """Close and archive every period past the retention window, then trim it from the live chain."""
    state = audit_retention_state(request.organization)
    if state["floor_note"]:
        messages.error(request, state["floor_note"]); return redirect("audit_log")
    if state["chain_errors"]:
        messages.error(request, f"The chain does not verify for {len(state['chain_errors'])} event(s). "
                                "Nothing is archived while a break is unexplained.")
        return redirect("audit_log")
    if not state["due_periods"] and not state["pending_purges"]:
        messages.info(request, state["message"]); return redirect("audit_log")
    sealed = purged = events = 0
    # An archived period that was never trimmed is still retention unenforced, and the due-date walk
    # will not offer it again — the purge pass runs on its own so the page can finish what a
    # `--no-purge` run or a blocked delete left half done.
    for pending in state["pending_purges"]:
        if request.POST.get("action") == "seal_only":
            messages.info(request, f"{pending.period_start:%B %Y} stays archived-and-live while you are in archive-only mode.")
            continue
        try:
            purge_sealed_audit(pending, request.user)
        except ValidationError as exc:
            messages.error(request, f"{pending.period_start:%B %Y} was not purged: {' '.join(exc.messages)}")
            continue
        purged += 1; events += pending.event_count
    for start, end in state["due_periods"]:
        try:
            seal = seal_audit_period(request.organization, start, end, actor=request.user)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages)); break
        sealed += 1; events += seal.event_count
        if request.POST.get("action") == "seal_only":
            messages.success(request, f"{start:%B %Y} archived ({seal.event_count} events). The rows stay "
                                      "live until you purge them.")
            continue
        try:
            purge_sealed_audit(seal, request.user)
        except ValidationError as exc:
            messages.error(request, f"{start:%B %Y} is archived but was not purged: {' '.join(exc.messages)}")
            break
        purged += 1
    if purged:
        messages.success(request, f"Sealed and purged {purged} period(s), {events} event(s). The chain now "
                                  "continues from the seal head — verify says it still reads back.")
    return redirect("audit_log")

@require_POST
@membership_required(*PRIVILEGED)
def audit_seal_verify(request, seal_id):
    """Re-derive the period's chain from the archive alone."""
    seal = request.organization.audit_seals.filter(pk=seal_id).first()
    if not seal: raise Http404
    checked = verify_seal_archive(seal)
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action="audit.seal_verified",
        target_type="audit_seal", target_id=str(seal.pk),
        metadata={"events": checked["events"], "problems": checked["problems"], "sha256": checked["sha256"]})
    if checked["problems"]:
        messages.error(request, "This archive does not read back as sealed: " + "; ".join(checked["problems"]))
    else:
        messages.success(request, f"{checked['events']} events re-derived from the archive, hashes and links intact.")
    return redirect("audit_log")

@require_GET
@membership_required(*PRIVILEGED)
def audit_seal_download(request, seal_id):
    seal = request.organization.audit_seals.filter(pk=seal_id).first()
    if not seal: raise Http404
    if not seal.archive:
        messages.error(request, "This seal has no archive on file."); return redirect("audit_log")
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action="audit.seal_downloaded",
        target_type="audit_seal", target_id=str(seal.pk),
        metadata={"events": seal.event_count, "sha256": seal.archive_sha256, "status": seal.status})
    response = FileResponse(seal.archive.open("rb"), as_attachment=True,
        filename=f"audit-seal-{seal.period_start:%Y%m}-{seal.period_end:%Y%m}.ndjson",
        content_type="application/x-ndjson")
    # A company's chain of custody is not something a shared browser keeps.
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

@never_cache
def manifest(request):
    membership = request.user.organization_memberships.filter(active=True).select_related("organization").first() if request.user.is_authenticated else None
    name = membership.organization.display_name if membership else "Texas Security Company Manager"
    return JsonResponse({"name": name, "short_name": name[:24], "start_url": "/", "display": "standalone", "background_color": "#F4F7FB", "theme_color": membership.organization.primary_color if membership else "#16324F", "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}]})

def service_worker(request):
    # CLK-5. The shell used to be the only thing cached, so a guard whose tab had closed could
    # install the app and still get a browser error page at the gate — "installable" meant the
    # icon worked. /clock/ is now cached as a document and refreshed on every online load, and the
    # page is told through a message when what it is rendering came from cache rather than the
    # server, because a stale page that looks live is worse than one that admits it is stale.
    #
    # Disclosed trade-off: the cached document is this officer's own schedule and patrol points, so
    # on a shared kiosk an offline reopen can show the last person's roster. It is served only as a
    # fallback when the network is unreachable and the sign-in cookie still applies to the reload,
    # and the punch queue is encrypted per device — the page being visible never lets a punch be
    # attributed to anyone but the device token that recorded it.
    js = '''const SHELL="tscm-shell-v2",DOCS="tscm-docs-v2";
const SHELL_ASSETS=["/static/css/app.css","/static/js/app.js","/static/icon.svg","/theme.css","/manifest.webmanifest"];
const PAGES=["/clock/"];
const put=(cache,key,response)=>response.ok?cache.put(key,response.clone()):null;
self.addEventListener("install",event=>event.waitUntil((async()=>{
  const shell=await caches.open(SHELL);
  await Promise.all(SHELL_ASSETS.map(path=>fetch(path,{redirect:"error"}).then(response=>put(shell,path,response)).catch(()=>null)));
  const docs=await caches.open(DOCS);
  await Promise.all(PAGES.map(path=>fetch(path,{credentials:"same-origin",redirect:"error"}).then(response=>put(docs,path,response)).catch(()=>null)));
})()));
self.addEventListener("activate",event=>event.waitUntil((async()=>{
  const names=await caches.keys();
  await Promise.all(names.filter(name=>name!==SHELL&&name!==DOCS).map(name=>caches.delete(name)));
  await self.clients.claim();
})()));
const announceStale=()=>self.clients.matchAll({type:"window"}).then(list=>list.forEach(client=>client.postMessage({type:"clock-served-from-cache"})));
const offlineNotice=()=>new Response('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Time clock - offline</title><link rel="stylesheet" href="/static/css/app.css"></head><body><main class="page narrow"><h1>No saved clock page</h1><p>The clock could not be reached and this device has not opened it while online yet. Reopen it once you have signal; punches taken before that are not lost, they wait encrypted on this device.</p></main></body></html>',{status:200,headers:{"Content-Type":"text/html; charset=utf-8"}});
self.addEventListener("fetch",event=>{
  const request=event.request;
  if(request.method!=="GET")return;
  const url=new URL(request.url);
  const path=url.pathname.endsWith("/")?url.pathname:url.pathname+"/";
  if(PAGES.includes(path)){
    event.respondWith(fetch(request).then(response=>{
      if(response.ok)caches.open(DOCS).then(cache=>cache.put(request,response.clone()));
      return response;
    }).catch(()=>caches.open(DOCS).then(cache=>cache.match(request)).then(cached=>{
      if(cached){announceStale();return cached;}
      return offlineNotice();
    })));
    return;
  }
  if(url.origin!==self.location.origin)return;
  event.respondWith(fetch(request).catch(()=>caches.match(request)));
});'''
    return HttpResponse(js, content_type="application/javascript", headers={"Service-Worker-Allowed": "/"})


# ── Onboarding steps (roadmap §13, ONB-1) ────────────────────────────────────────

@membership_required(*RECORD_WRITERS)
def onboarding_settings(request):
    """The checklist this company issues to a new hire, and who is standing in it right now.

    Definitions and progress share a screen because they are asked together — "does every armed
    officer owe the commission card?" and "who is stuck on that today". A settings page that shows
    only the form and never the consequence is a form, not a control.
    """
    org = request.organization
    scope = scope_for(request)
    progress = onboarding_progress(org, scope)
    roster = {person.pk: person for person in scope.filter_people(
        org.people.exclude(status__in=Person.NON_WORKING_STATUSES)).select_related("branch")}
    rows = sorted(({"person": roster[pid], "open": state["open"], "overdue": state["overdue"]}
                   for pid, state in progress.items() if pid in roster),
                  key=lambda row: (row["person"].last_name, row["person"].first_name))
    return render(request, "core/onboarding_settings.html", {
        "items": org.onboarding_items.all(),
        "rows": rows,
        "onboarding_count": scope.filter_people(org.people.filter(status=Person.Status.ONBOARDING)).count(),
        "can_edit": request.membership.role in PRIVILEGED or request.membership.role == Membership.Role.HR,
    })


@membership_required(*RECORD_WRITERS)
def onboarding_item_edit(request, item_id=None):
    """Add or edit one step. The two are the same screen because the fields are the same question."""
    org = request.organization
    item = None
    if item_id is not None:
        item = org.onboarding_items.filter(pk=item_id).first()
        if item is None:
            raise Http404
    form = OnboardingItemForm(request.POST or None, instance=item, organization=org)
    if form.signing_error:
        messages.warning(request, form.signing_error)
    if request.POST:
        code = (request.POST.get("code") or "").strip()
        # The uniqueness rule spans (company, code) and the company is not a field on the form, so
        # the model form cannot raise it — and without this check a reused code reaches the database
        # constraint and answers the operator with a 500 instead of a sentence.
        if code and org.onboarding_items.filter(code=code).exclude(pk=item.pk if item else None).exists():
            form.add_error("code", f"Another step already uses “{code}”.")
        if form.is_valid() and not form.errors:
            saved = form.save(commit=False)
            saved.organization = org
            saved.save()
            AuditEvent.objects.create(organization=org, actor=request.user,
                                      action="onboarding.item_saved", target_type="onboarding_item",
                                      target_id=str(saved.pk),
                                      metadata={"code": saved.code, "kind": saved.kind,
                                                "new": item is None,
                                                "applies_to": saved.applies_to or []})
            messages.success(request, f"Saved {saved.name}. New steps reach officers the next time the checklist is issued.")
            return redirect("onboarding_settings")
    return render(request, "core/form.html", {
        "form": form,
        "title": f"Edit {item.name}" if item else "New onboarding step",
        "eyebrow": "Onboarding",
        "cancel_url": reverse("onboarding_settings"),
        "note": "A step that names a record or a credential is checked against the personnel file, not against a tick box: it cannot be marked complete while the evidence is missing. A step that names neither is a task somebody marks done.",
    })


@membership_required(*MANAGERS)
@require_POST
@transaction.atomic
def onboarding_issue(request, person_id=None):
    """Hand the checklist to one officer, or to everybody currently onboarding.

    Safe to press twice: ``(person, item)`` is unique, so re-issuing after a step is added creates
    only the new rows. That property is what lets the CSV import and the worker pass call the same
    function as this button without a lock or a cleanup job.
    """
    org = request.organization
    if person_id:
        people = [_profile_person(request, person_id)]
    else:
        people = list(scope_for(request).filter_people(
            org.people.filter(status=Person.Status.ONBOARDING)).select_related("user"))
    created = 0
    notified = 0
    for person in people:
        rows = provision_onboarding_tasks(person)
        if rows:
            created += len(rows)
            notified += queue_onboarding_assignment(person, rows)
    AuditEvent.objects.create(organization=org, actor=request.user, action="onboarding.issued",
                              target_type="organization", target_id=str(org.pk),
                              metadata={"people": len(people), "tasks": created, "notices": notified})
    messages.success(request, (f"Issued {created} step{'' if created == 1 else 's'} to {len(people)} officer{'' if len(people) == 1 else 's'}."
                               if created else "Every applicable step is already issued to these officers — nothing was duplicated."))
    return redirect(reverse("person_detail", args=[person_id]) + "?tab=onboarding") if person_id else redirect("onboarding_settings")


@membership_required()
@require_POST
@transaction.atomic
def onboarding_task_decide(request, task_id):
    """Complete, waive, or reopen one step.

    The gate is not uniform, and the difference is the point. An officer decides their **own** steps
    that are addressed to them; a waiver is never theirs to grant, because "this step does not apply
    to me" is a ruling about an insurance condition or a state requirement, and the person it is
    about is the last party who should be able to remove it. Reopening is the office's too — a tick
    that turns out to be wrong is corrected by the role that files records, not quietly by whoever
    clicked.
    """
    org = request.organization
    task = org.onboarding_tasks.select_related("item", "person", "person__user").filter(pk=task_id).first()
    if task is None:
        raise Http404
    if not scope_for(request).permits_person(task.person):
        raise Http404
    role = request.membership.role
    is_self = bool(task.person.user_id) and task.person.user_id == request.user.id
    action = request.POST.get("action")
    note = (request.POST.get("note") or "").strip()
    staff = role in MANAGERS
    may_waive = role in RECORD_WRITERS
    checklist_url = (reverse("person_detail", args=[task.person_id]) + "?tab=onboarding"
                     if staff else reverse("my_onboarding"))
    if action == "waive" and not may_waive:
        messages.error(request, "Only the office that files records can waive a step, and the reason has to be recorded.")
        return redirect(checklist_url)
    if action == "reopen" and not staff:
        messages.error(request, "Only the office can reopen a step somebody else decided.")
        return redirect(checklist_url)
    if not staff and not (is_self and task.item.owner == OnboardingItem.Owner.PERSON and action == "complete"):
        raise Http404
    target = {"complete": OnboardingTask.Status.DONE, "waive": OnboardingTask.Status.WAIVED,
              "reopen": OnboardingTask.Status.OPEN}.get(action)
    if target is None:
        raise Http404
    try:
        if target == OnboardingTask.Status.OPEN:
            task.status = OnboardingTask.Status.OPEN
            task.decided_by = request.user
            task.decided_at = timezone.now()
            task.note = note
            task.save(update_fields=["status", "decided_by", "decided_at", "note", "updated_at"])
        else:
            decide_onboarding_task(task, request.user, target, note)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect(checklist_url)
    AuditEvent.objects.create(organization=org, actor=request.user, action="onboarding.decided",
                              target_type="onboarding_task", target_id=str(task.pk),
                              metadata={"person": str(task.person_id), "item": str(task.item_id),
                                        "status": task.status, "kind": task.item.kind})
    if target != OnboardingTask.Status.OPEN and task.person.user_id and task.person.user_id != request.user.id:
        queue_notice(organization=org, recipients={task.person.user_id},
                     event_type=f"onboarding.{task.status}",
                     subject=f"{task.item.name} — {task.get_status_display()}",
                     body=(f"{'It is marked complete. ' if task.status == OnboardingTask.Status.DONE else 'It has been waived for you: '}"
                           + f"{task.item.name} for {org.display_name or org.legal_name}."
                           + (f" Reason recorded: {task.note}" if task.note else "")),
                     dedup_key=f"onboarding-decision:{task.pk}:{task.status}",
                     sms={"item": task.item.name, "note": task.note})
    messages.success(request, {"done": f"{task.item.name} is complete.",
                               "waived": f"{task.item.name} is waived for {task.person.full_name}.",
                               "open": f"{task.item.name} is outstanding again."}[task.status])
    return redirect(checklist_url)


@membership_required()
def my_onboarding(request):
    """The officer's own steps — what is owed, by when, and what is still missing."""
    org = request.organization
    person = org.people.select_related("user", "branch").filter(user=request.user).first()
    if person is None:
        messages.error(request, "Your login is not linked to a personnel record, so there is no checklist to show.")
        return redirect("dashboard")
    board = onboarding_board(org, person, reader=_record_reader(request))
    return render(request, "core/my_onboarding.html", {
        "person": person, "board": board, "can_waive": False,
        "staff_can_decide": request.membership.role in MANAGERS,
    })


# ── Registry verification (roadmap §8, CMP-3) ────────────────────────────────────

@membership_required(*RECORD_WRITERS)
@require_POST
@transaction.atomic
def credential_registry_check(request, credential_id):
    """Record that somebody looked this credential up in the state registry.

    The DPS licence search has no public API, so this is the honest feature: a dated, attributed
    human check that the compliance queue can age, rather than an automated call this product cannot
    make. A clean look also finishes the verification the row was already carrying a column for —
    ``verified_at`` had never been written by anything since the table was created.
    """
    org = request.organization
    credential = org.credentials.select_related("credential_type", "person").filter(pk=credential_id).first()
    if credential is None:
        raise Http404
    if not scope_for(request).permits_person(credential.person):
        raise Http404
    result = (request.POST.get("result") or "").strip()
    if result not in dict(CredentialRegistryCheck.Result.choices):
        messages.error(request, "Choose what the registry actually showed.")
        return redirect("person_detail", person_id=credential.person_id)
    raw = (request.POST.get("checked_on") or "").strip() or timezone.localdate().isoformat()
    try:
        checked_on = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        messages.error(request, "The check date has to be a real date.")
        return redirect("person_detail", person_id=credential.person_id)
    before = credential.status
    check = record_registry_check(credential, request.user, checked_on, result,
                                 registry_reference=request.POST.get("registry_reference") or "",
                                 note=request.POST.get("note") or "")
    AuditEvent.objects.create(organization=org, actor=request.user, action="credential.registry_checked",
                              target_type="credential", target_id=str(credential.pk),
                              metadata={"result": result, "checked_on": checked_on.isoformat(),
                                        "person": str(credential.person_id),
                                        "status_before": before, "status_after": credential.status})
    if result in ("expired", "not_found", "attention"):
        # The office and the officer both hear about this one: a registry page that contradicts the
        # credential on file is the fact that ends a posting, and only one of the two parties can
        # fix the record.
        recipients = set(role_recipients(org, PRIVILEGED + (Membership.Role.HR,))) | (
            {credential.person.user_id} if credential.person.user_id else set())
        queue_notice(organization=org, recipients=recipients - {request.user.pk},
                     event_type="credential.registry_adverse",
                     subject=f"{credential.person.full_name}: registry says {check.get_result_display().lower()}",
                     body=(f"{credential.credential_type.name} ({credential.number or 'no number recorded'}) for "
                           f"{credential.person.full_name} was checked against the registry on {checked_on:%d %b %Y} "
                           f"and the result was “{check.get_result_display()}”. The credential has not been changed — "
                           "somebody has to decide what the file should now say."),
                     dedup_key=f"credential-registry:{check.pk}",
                     sms={"officer": credential.person, "item": credential.credential_type.name,
                          "status": check.get_result_display().lower()})
        messages.warning(request, f"Recorded: the registry says {check.get_result_display().lower()}. The credential status was left as it was for a person to decide.")
    else:
        messages.success(request, ("Registry check recorded." + (" The credential is now marked verified."
                          if before != credential.status else "")))
    return redirect("person_detail", person_id=credential.person_id)


# ── Pay categories and designated hours (roadmap §2, PAY-2 / PAY-5) ────────────────

@membership_required(*PAYROLL)
def settings_pay_categories(request):
    """What each kind of hour means for pay here, and the reading behind it.

    Five kinds, three switches each. The page shows the effect rather than the fields, because
    "unpaid" and "excluded from overtime" mean different money to a clerk: an unpaid meal takes the
    hours out of the tour and can therefore un-charge overtime later in the same week, while a
    holiday premium leaves the hours where they are and adds the excess over straight time.
    """
    org = request.organization
    ensure_pay_categories(org)
    policy, _ = TimePolicy.objects.get_or_create(organization=org)
    return render(request, "core/settings_pay_categories.html", {
        "categories": org.pay_categories.select_related("organization").order_by("kind"),
        "policy": policy,
        "designations": ShiftHourDesignation.objects.filter(organization=org,
                                                            shift__starts_at__gte=timezone.now() - timedelta(days=45))
                       .select_related("shift", "shift__site", "category", "recorded_by").order_by("-shift__starts_at")[:25],
        "counts": {kind: org.hour_designations.filter(category__kind=kind).count()
                   for kind, _label in PayCategory.Kind.choices},
    })


@membership_required(*PAYROLL)
@require_POST
def pay_category_edit(request, category_id):
    """Edit one kind's paidness, multiple, and overtime basis — and bump its version if that changed."""
    org = request.organization
    category = org.pay_categories.filter(pk=category_id).first()
    if category is None:
        raise Http404
    before = {name: getattr(category, name) for name in ("paid", "multiplier", "counts_toward_overtime", "active")}
    # Taken before this POST is written onto the object, so the version being replaced can be
    # recorded under its own number rather than lost to the one that supersedes it.
    prior_revision = category.revision
    prior_values = rule_snapshot(RuleRevision.Kind.PAY_CATEGORY, category)
    category.paid = request.POST.get("paid") == "on"
    category.counts_toward_overtime = request.POST.get("counts_toward_overtime") == "on"
    category.active = request.POST.get("active") != "off"
    try:
        category.multiplier = Decimal((request.POST.get("multiplier") or "1").strip())
    except Exception:
        messages.error(request, "The multiplier has to be a number, such as 1.5.")
        return redirect("settings_pay_categories")
    if category.multiplier < Decimal("0") or category.multiplier > Decimal("5"):
        messages.error(request, "Keep the multiplier between 0 and 5.")
        return redirect("settings_pay_categories")
    category.name = (request.POST.get("name") or category.name).strip()[:140]
    category.interpretation = (request.POST.get("interpretation") or "").strip()
    # No version bump here: ``PayCategory.save`` owns it, so every door tells the same truth.
    category.save()
    # ...but recording the version each number belongs to is this door's job, because the snapshot of
    # what is being replaced only exists here. Without it ``revision`` climbs and the "rule v2" a
    # payroll line prints names a version that cannot be read back.
    revise_pay_category(category, request.user, previous=(prior_revision, prior_values))
    AuditEvent.objects.create(organization=org, actor=request.user, action="pay_category.saved",
                              target_type="pay_category", target_id=str(category.pk),
                              metadata={"kind": category.kind, "before": {key: str(value) for key, value in before.items()},
                                        "paid": category.paid, "multiplier": str(category.multiplier),
                                        "counts_toward_overtime": category.counts_toward_overtime,
                                        "revision": category.revision})
    # The second half says the thing the first half could be misread as promising: a designation is
    # priced by the version in force when payroll is generated, not by the one that was in force when
    # the hours were marked. That is the deliberate rule here (see ``PayCategoryTest``), and an
    # operator who reads "rule version 2" as "nothing already paid has changed" would be wrong.
    messages.success(request, f"Saved {category.name} — rule version {category.revision}. Earlier versions stay readable in the rule history; "
                              f"hours already marked on posts are priced by the version now in force, so this changes what unpaid time costs.")
    return redirect("settings_pay_categories")


@membership_required(*PAYROLL)
@require_POST
def time_policy_premium(request):
    """The overtime multiple, which was arithmetic and is now policy (PAY-5)."""
    org = request.organization
    policy, _ = TimePolicy.objects.get_or_create(organization=org)
    # Snapshot every field the revision watcher inspects, not just the two this form edits:
    # ``bump_policy_revision`` builds its own watched list and reads ``before.get(name)``, so a
    # partial snapshot compares None against a real value and bumps the version on every save —
    # which is precisely the "a version that moves for nothing" failure the stamp exists to avoid.
    before = {field.name: getattr(policy, field.name) for field in policy._meta.fields}
    try:
        premium = Decimal((request.POST.get("overtime_premium") or "").strip())
        threshold = Decimal((request.POST.get("overtime_after_hours") or "").strip())
    except Exception:
        messages.error(request, "Both the threshold and the premium have to be numbers.")
        return redirect("settings_pay_categories")
    if premium < Decimal("1") or premium > Decimal("5"):
        messages.error(request, "A premium below 1.0× is not a premium; keep it between 1 and 5.")
        return redirect("settings_pay_categories")
    if threshold < Decimal("1") or threshold > Decimal("168"):
        messages.error(request, "The weekly threshold has to sit between 1 and 168 hours.")
        return redirect("settings_pay_categories")
    policy.overtime_premium = premium
    policy.overtime_after_hours = threshold
    bump_policy_revision(policy, before)
    policy.save()
    AuditEvent.objects.create(organization=org, actor=request.user, action="time_policy.premium_changed",
                              target_type="time_policy", target_id=str(policy.pk),
                              metadata={"before": {key: str(value) for key, value in before.items()},
                                        "overtime_premium": str(premium),
                                        "overtime_after_hours": str(threshold), "revision": policy.revision})
    messages.success(request, f"Overtime is now {premium}× above a {threshold}-hour week (rule version {policy.revision}).")
    return redirect("settings_pay_categories")


@membership_required(*(MANAGERS + PAYROLL))
def shift_hours(request, shift_id):
    """One post, its recorded time, and the hours a human had to say something about."""
    org = request.organization
    shift = org.shifts.select_related("site__client", "officer", "pay_code").filter(pk=shift_id).first()
    if shift is None:
        raise Http404
    scope = scope_for(request)
    if not scope.permits_shift(shift):
        raise Http404
    ensure_pay_categories(org)
    return render(request, "core/shift_hours.html", {
        "shift": shift,
        "categories": org.pay_categories.filter(active=True).exclude(kind=PayCategory.Kind.LEAVE).order_by("kind"),
        "designations": shift.hour_designations.select_related("category", "recorded_by").order_by("category__kind"),
        "scheduled_hours": (Decimal((shift.ends_at - shift.starts_at).total_seconds()) / Decimal(3600)).quantize(Decimal("0.01"))
                           if shift.starts_at and shift.ends_at else None,
        # SCH-3, on the same page for the same reason: this is the screen a person opens when the
        # clock and the schedule disagree, and the hold-over is the other half of that disagreement.
        "hold_overs": shift.hold_overs.select_related("relief", "recorded_by").order_by("created_at"),
        "prompt": overrun_prompt(shift),
        "relief_reasons": HoldOver.Reason.choices,
        "relief_halves": shift.relief_halves.select_related("officer", "site").order_by("starts_at"),
        "relieved_post": shift.relief_for,
        # The officer who stayed cannot be the relief who failed to arrive, so they are out of the
        # list rather than left in it for the form to refuse after the dispatcher has typed.
        # Ordered on the name columns: ``full_name`` is a property, so asking the database to sort by
        # it raises FieldError on the whole page — which is why this is not written the readable way.
        "relief_candidates": scope.filter_people(org.people.filter(status=Person.Status.ACTIVE))
                                  .exclude(pk=shift.officer_id).order_by("last_name", "first_name"),
    })


def _form_datetime(value):
    """Parse a datetime-local input, accepting both the browser's and the hand-typed shape.

    ``datetime-local`` submits ``2026-10-03T22:00``, which is not in Django's default input formats,
    and a dispatcher correcting a time by hand types the space-separated one. Both are accepted
    here so the same field does not fail differently depending on whether it was picked or typed —
    the failure mode is a form that looks valid and refuses on save.
    """
    from django.utils.dateparse import parse_datetime
    raw = (value or "").strip().replace(" ", "T")
    if not raw:
        return None
    parsed = parse_datetime(raw) or parse_datetime(raw + ":00")
    if parsed is None:
        raise ValidationError("That is not a date and time I can read.")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


@membership_required(*(MANAGERS + PAYROLL))
@require_POST
@transaction.atomic
def shift_hold_over(request, shift_id):
    """Record why a tour ran past its end — the fact the punches cannot show.

    Kept on the post rather than on the officer, because a hold-over is a coverage event: the site
    stayed staffed and the next tour did not get its officer, and both of those are statements about
    the post. The officer's own overrun is already in the clock.
    """
    org = request.organization
    shift = org.shifts.filter(pk=shift_id).first()
    if shift is None or not scope_for(request).permits_shift(shift):
        raise Http404
    relief = None
    relief_id = request.POST.get("relief")
    if relief_id:
        relief = org.people.filter(pk=relief_id).first()
        if relief is None:
            messages.error(request, "That relief officer is not in this company.")
            return redirect("shift_hours", shift_id=shift.pk)
    try:
        scheduled = _form_datetime(request.POST.get("scheduled_ends_at")) or shift.ends_at
        held_until = _form_datetime(request.POST.get("held_until"))
        expected_release_at = _form_datetime(request.POST.get("expected_release_at"))
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("shift_hours", shift_id=shift.pk)
    try:
        row = record_hold_over(shift, request.POST.get("reason"), actor=request.user,
                               held_until=held_until, relief=relief,
                               note=request.POST.get("note"), scheduled_ends_at=scheduled,
                               expected_release_at=expected_release_at)
    except ValidationError as exc:
        # Field-level messages, not just the first one: a form that reports only that the relief was
        # wrong, while the real refusal was the time, sends the dispatcher back to the wrong field.
        parts = []
        for field, messages_list in (exc.message_dict.items() if hasattr(exc, "message_dict") else [("", exc.messages)]):
            parts.extend([f"{field}: {text}" if field and field != "__all__" else text for text in messages_list])
        messages.error(request, " ".join(parts))
        return redirect("shift_hours", shift_id=shift.pk)
    messages.success(request, f"Recorded: {describe_hold_over(row)}. The reason now shows on the timecard row.")
    return redirect("shift_hours", shift_id=shift.pk)


@membership_required(*(MANAGERS + PAYROLL))
@require_POST
@transaction.atomic
def shift_hours_save(request, shift_id):
    org = request.organization
    shift = org.shifts.filter(pk=shift_id).first()
    if shift is None or not scope_for(request).permits_shift(shift):
        raise Http404
    action = request.POST.get("action")
    if action == "remove":
        row = shift.hour_designations.filter(pk=request.POST.get("designation_id")).select_related("category").first()
        if row is None:
            raise Http404
        label, category = f"{row.hours}h {row.category.name}", row.category
        row.delete()
        AuditEvent.objects.create(organization=org, actor=request.user, action="shift_hours.removed",
                                  target_type="shift", target_id=str(shift.pk),
                                  metadata={"category": category.kind, "removed": str(category.pk)})
        messages.success(request, f"Removed {label} from this post. Recorded time is untouched.")
        return redirect("shift_hours", shift_id=shift.pk)
    category = org.pay_categories.filter(pk=request.POST.get("category_id"), active=True).first()
    if category is None:
        messages.error(request, "Choose one of the pay categories.")
        return redirect("shift_hours", shift_id=shift.pk)
    try:
        set_shift_designation(shift, category, request.POST.get("hours"), request.POST.get("reason"), request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("shift_hours", shift_id=shift.pk)
    AuditEvent.objects.create(organization=org, actor=request.user, action="shift_hours.designated",
                              target_type="shift", target_id=str(shift.pk),
                              metadata={"category": category.kind, "hours": request.POST.get("hours"),
                                        "paid": category.paid, "multiplier": str(category.multiplier),
                                        "revision": category.revision})
    messages.success(request, f"Designated {request.POST.get('hours')}h as {category.name} on this post.")
    return redirect("shift_hours", shift_id=shift.pk)
