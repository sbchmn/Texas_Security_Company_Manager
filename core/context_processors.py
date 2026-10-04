from django.conf import settings
from django.urls import reverse


def organization_brand(request):
    membership = getattr(request, "membership", None)
    if not membership and getattr(request, "user", None) and request.user.is_authenticated:
        membership = request.user.organization_memberships.filter(active=True).select_related("organization").first()
    organization = membership.organization if membership else None
    return {"current_organization": organization, "current_membership": membership, "google_sso_enabled": bool(settings.SOCIALACCOUNT_PROVIDERS["google"]["APP"]["client_id"]), "microsoft_sso_enabled": bool(settings.SOCIALACCOUNT_PROVIDERS["microsoft"]["APP"]["client_id"])}


# (label, url name, roles allowed, url names that also light this link)
NAVIGATION = (
    ("Company", (
        ("Overview", "dashboard", None, ("dashboard",)),
        ("People", "people", "MANAGERS", ("people", "person_detail", "person_edit", "person_create", "person_access_invite", "person_availability", "availability_remove")),
        ("Team access", "team", "PRIVILEGED", ("team", "invitation_accept", "authority", "authority_revoke")),
    )),
    ("Operations", (
        ("Clients & sites", "locations", "MANAGERS", ("locations", "client_create", "client_edit", "site_create", "site_edit", "checkpoint_create", "checkpoint_edit")),
        ("Branches", "branches", "MANAGERS", ("branches", "branch_create", "branch_edit")),
        ("Scheduling", "schedule", "MANAGERS", ("schedule", "shift_create", "shift_edit", "shift_cancel")),
        ("Recurring posts", "shift_templates", "MANAGERS", ("shift_templates", "shift_template_create", "shift_template_edit", "shift_template_generate")),
        ("Shift moves", "swaps", "MANAGERS", ("swaps", "swap_decide", "exchange_decide")),
        ("Time off", "time_off", "MANAGERS", ("time_off", "time_off_decide")),
        ("Time clock", "clock", None, ("clock", "adjustment_request")),
        ("Clock stations", "clock_kiosks", "MANAGERS", ("clock_kiosks", "clock_kiosk_close", "clock_kiosk_clear", "person_pin_issue")),
        ("Time review", "time_review", "TIME_REVIEWERS", ("time_review", "punch_review", "adjustment_review")),
        ("Payroll", "payroll", "PAYROLL", ("payroll", "payroll_approve", "payroll_run_export", "payroll_export")),
        ("Pay codes", "pay_codes", "MANAGERS", ("pay_codes", "pay_code_create", "pay_code_edit", "pay_code_remove")),
    )),
    ("Compliance", (
        ("Compliance queue", "compliance", "MANAGERS", ("compliance", "credential_create", "person_credential_create", "credential_edit", "credential_registry_check")),
        ("Onboarding steps", "onboarding_settings", "RECORD_WRITERS", ("onboarding_settings", "onboarding_item_create", "onboarding_item_edit", "onboarding_issue")),
        ("Reports", "reports", "MANAGERS", ("reports", "report_capture")),
        ("Saved reports", "saved_reports", "MANAGERS", ("saved_reports", "report_snapshot_download")),
        ("Personnel documents", "documents", "RECORD_READERS", ("documents", "document_upload", "person_document_upload", "document_download", "document_acknowledgments", "document_remind")),
        ("Training records", "training", "MANAGERS", ("training", "training_create", "person_training_create", "training_edit")),
        ("Retention review", "retention_review", "PRIVILEGED", ("retention_review", "disposition_request", "disposition_execute", "legal_hold_toggle")),
        ("Audit log", "audit_log", "AUDIT_READERS", ("audit_log", "audit_export", "audit_redact", "audit_redaction_decide")),
    )),
    ("My work", (
        ("My shifts", "my_shifts", None, ("my_shifts", "offer_post", "swap_respond", "swap_withdraw", "exchange_respond", "exchange_withdraw")),
        ("Open posts", "open_posts", None, ("open_posts", "shift_requests", "shift_claim")),
        ("My availability", "availability", None, ("availability", "availability_remove")),
        ("My time off", "my_time_off", None, ("my_time_off", "my_time_off_cancel")),
        ("My clock PIN", "clock_pin", None, ("clock_pin",)),
        # Every member sees the consent page, because every member has an answer to give: a notice
        # nobody opted into is a notice that never arrives, and the officer is the only person who
        # can create that record.
        ("Text alerts", "text_alerts", None, ("text_alerts",)),
        ("My documents", "my_documents", None, ("my_documents", "document_acknowledge")),
        # Every member sees it, because every member may be the new hire: the link is cheap and the
        # alternative is a checklist an officer can only see by asking somebody in the office.
        ("My onboarding", "my_onboarding", None, ("my_onboarding",)),
        ("Notifications", "notifications", None, ("notifications", "notification_read")),
    )),
    ("Setup", (
        ("Bulk imports", "imports", "RECORD_WRITERS", ("imports", "import_apply", "import_template", "import_errors")),
        ("Messaging delivery", "messaging_settings", "PRIVILEGED", ("messaging_settings", "messaging_rotate_token")),
        ("Settings", "settings", "SETTINGS_VIEWERS", ("settings", "settings_compliance", "onboarding_settings", "onboarding_item_create", "onboarding_item_edit", "branding", "brand_rollback", "domains", "domain_verify", "security_settings", "time_policy", "credential_type_create", "credential_type_edit", "document_type_create", "document_type_edit", "custom_field_create", "custom_field_edit")),
    )),
)


def navigation(request):
    """Grouped sidebar links, filtered to the roles that would not answer 403.

    The sidebar used to be a flat list of every surface in the product, so an officer was
    offered Team access, Payroll, and Audit and met PermissionDenied on each click. The
    same role tuples the view decorators use decide what is rendered here.
    """
    membership = getattr(request, "membership", None)
    if membership is None:
        return {"navigation": [], "can_manage_people": False, "authority_scope": None}
    from . import views
    from .scope import scope_for
    groups = {"MANAGERS": views.MANAGERS, "PRIVILEGED": views.PRIVILEGED, "RECORD_READERS": views.RECORD_READERS,
              "RECORD_WRITERS": views.RECORD_WRITERS, "PAYROLL": views.PAYROLL, "TIME_REVIEWERS": views.TIME_REVIEWERS,
              "AUDIT_READERS": views.AUDIT_READERS, "SETTINGS_VIEWERS": views.MANAGERS + views.AUDIT_READERS}
    current = request.resolver_match.url_name if request.resolver_match else None
    sections = []
    for title, items in NAVIGATION:
        rendered = []
        for label, name, roles, active_names in items:
            if roles is not None and membership.role not in groups[roles]:
                continue
            rendered.append({"label": label, "url": reverse(name), "active": current in active_names})
        if rendered:
            sections.append({"title": title, "items": rendered})
    scope = scope_for(request)
    return {"navigation": sections, "can_manage_people": membership.role in views.MANAGERS,
            "authority_scope": scope if scope.restricted else None}
