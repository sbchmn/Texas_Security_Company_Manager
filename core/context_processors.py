from django.conf import settings
from django.urls import reverse


def organization_brand(request):
    membership = getattr(request, "membership", None)
    if not membership and getattr(request, "user", None) and request.user.is_authenticated:
        membership = request.user.organization_memberships.filter(active=True).select_related("organization").first()
    organization = membership.organization if membership else None
    from django.utils import timezone as _tz
    year = _tz.localdate().year
    since = settings.APP_PUBLISHER["copyright_since"]
    return {"current_organization": organization, "current_membership": membership, "app_version": settings.APP_VERSION,
            "app_name": settings.APP_NAME, "app_publisher": settings.APP_PUBLISHER,
            "copyright_years": str(since) if year <= since else f"{since}–{year}", "google_sso_enabled": bool(settings.SOCIALACCOUNT_PROVIDERS["google"]["APP"]["client_id"]), "microsoft_sso_enabled": bool(settings.SOCIALACCOUNT_PROVIDERS["microsoft"]["APP"]["client_id"])}


# Workspace-first navigation: users choose a job, then explore related functions within that workspace.
# Each workspace has a landing page that surfaces the most urgent work first.
# (label, url name, roles allowed, url names that also light this link)

WORKSPACES = (
    ("Today", "workspace_today", None, (
        "workspace_today", "dashboard", "clock", "adjustment_request", "my_shifts", "offer_post", "swap_respond",
        "swap_withdraw", "exchange_respond", "exchange_withdraw", "my_onboarding", "document_acknowledge",
        "my_documents", "shift_claim", "open_posts", "shift_requests", "my_time_off", "my_time_off_cancel",
        "text_alerts", "notifications", "notification_read", "my_account", "my_contact_edit",
        "attendance_queue", "attendance_detail"
    )),
    ("People", "workspace_people", "MANAGERS", (
        "workspace_people", "people", "person_detail", "person_edit", "person_create", "person_access_invite", "person_access_link",
        "person_availability", "availability_remove", "person_credential_create", "person_training_create",
        "person_document_upload", "document_acknowledge", "availability", "signing_queue", "private_personnel", "employee_leave"
    )),
    ("Schedule", "workspace_schedule", "MANAGERS", (
        "workspace_schedule", "schedule", "shift_create", "shift_edit", "shift_cancel", "shift_templates",
        "shift_template_create", "shift_template_edit", "shift_template_generate", "swaps", "swap_decide",
        "exchange_decide", "time_off", "time_off_decide", "leave_cancel", "open_posts", "shift_requests", "shift_claim"
    )),
    ("Time & Payroll", "workspace_payroll", "TIME_REVIEWERS", (
        "workspace_payroll", "time_review", "punch_detail", "punch_review", "adjustment_review", "payroll", "payroll_approve",
        "payroll_run_export", "payroll_export", "payroll_reopen", "payroll_segment_lock"
    )),
    ("Compliance & Records", "workspace_compliance", "RECORD_READERS", (
        "workspace_compliance", "compliance", "credential_create", "credential_edit", "credential_registry_check",
        "documents", "document_upload", "person_document_upload", "document_download", "document_acknowledgments",
        "document_remind", "training", "training_create", "person_training_create", "training_edit",
        "retention_review", "disposition_request", "disposition_execute", "legal_hold_toggle", "onboarding_settings",
        "onboarding_item_create", "onboarding_item_edit", "onboarding_issue"
    )),
    ("Reports", "workspace_reports", "MANAGERS", (
        "workspace_reports", "reports", "report_capture", "saved_reports", "report_snapshot_download", "audit_log",
        "audit_export", "audit_redact", "audit_redaction_decide"
    )),
)

QUICK_LINKS = (
    ("Timeclock", "clock", ("clock", "adjustment_request")),
    ("My shifts", "my_shifts", ("my_shifts", "offer_post", "swap_respond", "swap_withdraw",
                                "exchange_respond", "exchange_withdraw")),
)

# Settings is available to authorized users as a persistent utility, not within a workspace.
# It appears in the sidebar footer and includes all configuration screens.
SETTINGS_NAVIGATION = (
    ("Company setup", "settings", "SETTINGS_VIEWERS", (
        "settings", "company_post_orders", "locations", "client_create", "client_edit", "site_create", "site_edit", "checkpoint_create",
        "checkpoint_edit", "branches", "branch_create", "branch_edit", "clock_kiosks", "clock_kiosk_close",
        "clock_kiosk_clear", "person_pin_issue"
    )),
    ("Compliance & onboarding", "settings_compliance", "RECORD_WRITERS", (
        "settings_compliance", "onboarding_settings", "onboarding_item_create", "onboarding_item_edit",
        "credential_type_create", "credential_type_edit", "document_type_create", "document_type_edit",
        "document_type_remove", "custom_field_remove", "compliance_rule_remove"
    )),
    ("Pay codes", "pay_codes", "MANAGERS", (
        "pay_codes", "pay_code_create", "pay_code_edit", "pay_code_remove"
    )),
    ("Branding & domains", "branding", "PRIVILEGED", (
        "branding", "brand_rollback", "domains", "domain_verify"
    )),
    ("Security & policies", "security_settings", "PRIVILEGED", (
        "security_settings", "time_policy", "leave_policy", "messaging_settings", "messaging_rotate_token", "signing_settings",
        "sms_templates", "sms_templates_legacy", "sms_template_edit", "email_template_edit"
    )),
    ("Data & integrations", "imports", "RECORD_WRITERS", (
        "imports", "import_apply", "import_template", "import_errors"
    )),
    ("Team & access", "team", "PRIVILEGED", (
        "team", "invitation_accept", "authority", "authority_revoke", "membership_access"
    )),
)

# Legacy flat navigation kept for backwards compatibility and testing; phased out.
NAVIGATION = (
    ("Company", (
        ("Overview", "dashboard", None, ("dashboard",)),
        ("People", "people", "MANAGERS", ("people", "person_detail", "person_edit", "person_create", "person_access_invite", "person_access_link", "person_availability", "availability_remove")),
        ("Team access", "team", "PRIVILEGED", ("team", "invitation_accept", "authority", "authority_revoke", "membership_access")),
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
        ("Timesheets", "time_review", "TIME_REVIEWERS", ("time_review", "punch_detail", "punch_review", "adjustment_review")),
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
        ("Text alerts", "text_alerts", None, ("text_alerts",)),
        ("My documents", "my_documents", None, ("my_documents", "document_acknowledge")),
        ("My onboarding", "my_onboarding", None, ("my_onboarding",)),
        ("Notifications", "notifications", None, ("notifications", "notification_read")),
    )),
    ("Setup", (
        ("Bulk imports", "imports", "RECORD_WRITERS", ("imports", "import_apply", "import_template", "import_errors")),
        ("Messaging delivery", "messaging_settings", "PRIVILEGED", ("messaging_settings", "messaging_rotate_token")),
        ("Notification wording", "sms_templates", "PRIVILEGED", ("sms_templates", "sms_templates_legacy", "sms_template_edit", "email_template_edit")),
        ("Document signing", "signing_settings", "PRIVILEGED", ("signing_settings",)),
        ("Settings", "settings", "SETTINGS_VIEWERS", ("settings", "settings_compliance", "onboarding_settings", "onboarding_item_create", "onboarding_item_edit", "branding", "brand_rollback", "domains", "domain_verify", "security_settings", "time_policy", "credential_type_create", "credential_type_edit", "document_type_create", "document_type_edit", "document_type_remove", "custom_field_create", "custom_field_edit", "custom_field_remove", "compliance_rule_remove")),
    )),
)


def navigation(request):
    """Workspace-first navigation grouped by user responsibility.
    
    Primary workspaces appear as main entries. Settings is a persistent utility accessed
    from the sidebar footer. Role filtering ensures only accessible workspaces are shown.
    """
    membership = getattr(request, "membership", None)
    if membership is None:
        return {"workspaces": [], "quick_links": [], "settings_nav": [], "settings_active": False, "can_manage_people": False, "authority_scope": None, "active_workspace": None, "navigation": []}
    
    from . import views
    from .scope import scope_for
    
    groups = {
        "MANAGERS": views.MANAGERS, "PRIVILEGED": views.PRIVILEGED, "RECORD_READERS": views.RECORD_READERS,
        "RECORD_WRITERS": views.RECORD_WRITERS, "PAYROLL": views.PAYROLL, "TIME_REVIEWERS": views.TIME_REVIEWERS,
        "AUDIT_READERS": views.AUDIT_READERS, "SETTINGS_VIEWERS": views.MANAGERS + views.AUDIT_READERS
    }
    
    current = request.resolver_match.url_name if request.resolver_match else None
    
    # Determine active workspace
    active_workspace = None
    workspaces_rendered = []
    for label, name, roles, active_names in WORKSPACES:
        if roles is not None and membership.role not in groups[roles]:
            continue
        if name == "workspace_payroll" and membership.role not in views.PAYROLL:
            label = "Timesheets"
        workspaces_rendered.append({
            "label": label,
            "url": reverse(name),
            "active": current in active_names,
            "current": current == name,
            "workspace_name": name
        })
    
    # Settings navigation appears in the footer
    settings_rendered = []
    for label, name, roles, active_names in SETTINGS_NAVIGATION:
        if roles is not None and membership.role not in groups[roles]:
            continue
        is_active = current in active_names
        settings_rendered.append({
            "label": label,
            "url": reverse(name),
            "active": is_active,
        })

    settings_active = any(item["active"] for item in settings_rendered)
    # Personal pages belong to Today; shared management routes prefer their specific workspace.
    matches = [item for item in workspaces_rendered if item["active"]]
    selected = next((item for item in matches if item["workspace_name"] != "workspace_today"),
                    matches[0] if matches else None) if not settings_active else None
    for item in workspaces_rendered:
        item["active"] = item is selected
    if selected:
        active_workspace = selected["label"]
    
    # Build legacy navigation structure for backward compatibility
    navigation_rendered = []
    for section_title, items in NAVIGATION:
        section_items = []
        for label, name, roles, active_names in items:
            if roles is not None and membership.role not in groups[roles]:
                continue
            is_active = current in active_names
            section_items.append({
                "label": label,
                "url": reverse(name),
                "active": is_active,
            })
        if section_items:
            navigation_rendered.append({
                "title": section_title,
                "items": section_items,
            })
    
    scope = scope_for(request)
    return {
        "workspaces": workspaces_rendered,
        "quick_links": [{"label": label, "url": reverse(name), "active": current in active_names}
                        for label, name, active_names in QUICK_LINKS],
        "settings_nav": settings_rendered,
        "settings_active": settings_active,
        "can_manage_people": membership.role in views.MANAGERS,
        "can_write_records": membership.role in views.RECORD_WRITERS,
        "can_review_retention": membership.role in views.PRIVILEGED,
        "can_manage_payroll": membership.role in views.PAYROLL,
        "authority_scope": scope if scope.restricted else None,
        "active_workspace": active_workspace,
        "navigation": navigation_rendered,  # Legacy for old sidebar templates
    }
