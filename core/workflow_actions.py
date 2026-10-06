"""Read-only workflow projections; decisions stay on their authoritative routes."""
from collections import Counter
from datetime import date

from django.urls import reverse
from django.db.models import Q

from .models import Membership, OnboardingItem, OnboardingTask, Person, SigningRequest, SigningSettings, record_open_for
from .services import onboarding_board


def onboarding_actions(request):
    from . import views
    from .scope import scope_for

    org = request.organization
    scope = scope_for(request)
    reader = views._record_reader(request)
    config = SigningSettings.objects.filter(organization=org, enabled=True).first()
    tasks_by_person = {}
    for task in scope.filter_by_person(org.onboarding_tasks.select_related(
        "person__user", "item__document_type", "item__credential_type",
    )):
        tasks_by_person.setdefault(task.person_id, []).append(task)
    rows = []
    signing_rows = []
    for person in scope.filter_people(org.people.filter(
        status__in=[Person.Status.ONBOARDING, Person.Status.ACTIVE],
    )).select_related("user", "branch"):
        board = onboarding_board(org, person, reader=reader, tasks=tasks_by_person.get(person.pk, []))
        url = reverse("person_detail", args=[person.pk]) + "?tab=onboarding"
        if board["unissued"]:
            rows.append({
                "person": person, "name": f'{board["unissued"]} steps not issued',
                "owner": "Office", "category": "unissued", "state": "Not issued",
                "next_step": "Issue missing steps from the checklist", "url": url,
                "due_on": None, "overdue": False, "created_at": None,
            })
        for row in board["rows"]:
            task = row["task"]
            hidden = row["evidence"] and row["evidence"]["state"] == "hidden"
            action = {
                **row, "person": person, "name": row["item"].name, "url": url + f"#task-{task.pk}",
                "owner": "Office" if row["item"].owner == OnboardingItem.Owner.STAFF else "Employee",
                "category": "office" if row["item"].owner == OnboardingItem.Owner.STAFF else "employee",
                "state": row["evidence"]["state"] if row["evidence"] else "Outstanding",
                "next_step": "Review checklist evidence and complete the step",
                "created_at": task.created_at,
                "action_label": "Open checklist",
            }
            if hidden:
                action.update(state="Evidence restricted", next_step="Personnel-record staff must review evidence")
            elif row["block"]:
                action.update(category="blocked", next_step="File or correct the required evidence")
                if row["item"].kind == OnboardingItem.Kind.CREDENTIAL:
                    action.update(url=reverse("person_detail", args=[person.pk]) + "?tab=credentials",
                                  action_label="Resolve credential")
                elif row["item"].kind == OnboardingItem.Kind.DOCUMENT and request.membership.role in views.RECORD_WRITERS:
                    action.update(url=reverse("person_document_upload", args=[person.pk]),
                                  action_label="File required document")
            if row["item"].kind == OnboardingItem.Kind.SIGNATURE and not hidden:
                signing = row["signing"]
                item = row["item"]
                action["can_send"] = bool(
                    config and config.encrypted_api_key and config.base_url and item.active and
                    item.signing_template_id and item.document_type_id and item.document_type.active and
                    item.owner == OnboardingItem.Owner.PERSON and
                    record_open_for(item.document_type, person.pk, Membership.Role.OFFICER, person.pk) and
                    (person.email or (person.user.email if person.user_id else ""))
                )
                if signing is None:
                    stage, next_step = "ready", "Send signing request"
                    if not action["can_send"]:
                        stage, next_step = "failed", "Configure signing, an active template, and the personnel record type before sending"
                        if not (person.email or (person.user.email if person.user_id else "")):
                            next_step = "Record the employee's signer email before sending"
                elif signing.status == SigningRequest.Status.COMPLETED and row["evidence"]["satisfied"]:
                    stage, next_step = "completed", "Open the filed documents"
                elif signing.status == SigningRequest.Status.COMPLETED:
                    stage, next_step = "failed", "Signed artifacts no longer satisfy this step; review the personnel file"
                elif signing.last_error or signing.status in (
                    SigningRequest.Status.REJECTED, SigningRequest.Status.DECLINED,
                ):
                    stage, next_step = "failed", "Review the error; check status or correct and resend"
                elif signing.status == SigningRequest.Status.PREPARING:
                    stage, next_step = "processing", "Reconcile creation; do not send a duplicate"
                else:
                    stage, next_step = "awaiting", "Employee signs; worker verifies and files completion"
                action.update(category=stage, next_step=next_step)
                if request.membership.role in views.RECORD_WRITERS:
                    action.update(url=reverse("signing_queue") + f"?stage={stage}&task={task.pk}", action_label="Review signing task")
                if signing is None:
                    action["owner"] = "Office"
                elif stage == "awaiting":
                    action["owner"] = "Employee"
                else:
                    action["owner"] = "Office / signing worker"
                if stage == "completed":
                    action["url"] = reverse("person_detail", args=[person.pk]) + "?tab=documents"
                if task.status == OnboardingTask.Status.OPEN or stage == "completed":
                    signing_rows.append(action)
            if task.status == OnboardingTask.Status.OPEN:
                rows.append(action)
    key = lambda row: (not row["overdue"], row["due_on"] or date.max, str(row["person"].pk), row["name"])
    rows.sort(key=key)
    signing_rows.sort(key=key)
    return {
        "rows": rows, "signing_rows": signing_rows, "total": len(rows),
        "overdue": sum(row["overdue"] for row in rows),
        "signing_counts": Counter(row["category"] for row in signing_rows),
        "office_count": sum(row["owner"] != "Employee" for row in rows),
    }


def compliance_actions(request, attendance):
    from . import views

    role = request.membership.role
    result = []
    for kind, section in attendance.items():
        if kind == "documents" and role not in views.RECORD_READERS:
            continue
        for item in section["rows"]:
            if not item["_needs"]:
                continue
            person = item.get("person")
            if person:
                tab = {"credentials": "credentials", "training": "training"}.get(kind, "documents")
                url = reverse("person_detail", args=[person.pk]) + f"?tab={tab}"
                label = "Resolve in personnel file" if role in views.RECORD_WRITERS else "Review personnel evidence"
                if kind == "credentials" and role in views.MANAGERS:
                    label = "Resolve credential"
                if tab == "documents" and role not in views.RECORD_READERS:
                    url = reverse("compliance") + f"?kind={kind}&person={person.pk}"
                    label = "Review duty; personnel-record staff must file evidence"
            else:
                url = reverse("documents") if role in views.RECORD_READERS else reverse("compliance") + f"?kind={kind}"
                label = "Review company evidence"
            if item.get("acknowledgments"):
                url = reverse("document_acknowledgments", args=[item["acknowledgments"]])
                label = "Review outstanding acknowledgments"
            result.append({**item, "kind": kind, "url": url, "action_label": label,
                           "owner": "Personnel-record staff" if kind in ("documents", "duties") else "Office",
                           "impact": "Review assignment eligibility" if kind == "credentials" else "Evidence needs review"})
    result.sort(key=lambda row: (row.get("date") or date.max, row["subject"]))
    return result


def setup_readiness(request):
    from . import views
    from .models import SigningSettings
    from .scope import scope_for

    org = request.organization
    role = request.membership.role
    scope = scope_for(request)
    checks = []
    def add(label, configured, detail, route, allowed):
        if allowed:
            checks.append({"label": label, "configured": configured, "detail": detail, "url": reverse(route)})
    manager = role in views.MANAGERS
    records = role in views.RECORD_WRITERS
    privileged = role in views.PRIVILEGED
    sites = scope.filter_sites(org.sites.filter(active=True))
    add("Staffing locations", sites.exists(), "Create active sites, then verify coordinates and geofences.", "locations", manager)
    add("Site coordinates", sites.exists() and not sites.filter(
        Q(latitude__isnull=True) | Q(longitude__isnull=True),
    ).exists(), "A geofence cannot be verified without both coordinates.", "locations", manager)
    add("Personnel access", not scope.filter_people(org.people.filter(
        status__in=[Person.Status.ACTIVE, Person.Status.ONBOARDING], user__isnull=True,
    )).exists() and scope.filter_people(org.people.all()).exists(),
        "Link each working employee to a sign-in from their personnel profile.", "people", manager)
    add("Compliance requirements", org.credential_types.filter(active=True).exclude(applies_to=[]).exists() or
        any(rule.unevaluated_reason is None for rule in org.compliance_rules.filter(active=True).select_related("document_type")),
        "Define applicable, measurable requirements and review their approvals; an empty compliance queue is not a readiness certificate.",
        "settings_compliance", manager)
    add("Onboarding steps", org.onboarding_items.filter(active=True).exists(),
        "Define and issue applicable steps. Issuing a checklist does not send documents.", "onboarding_settings", records)
    signing = SigningSettings.objects.filter(organization=org).first()
    add("Document signing", bool(signing and signing.enabled and signing.encrypted_api_key and signing.base_url),
        "Optional integration: connect DocuSeal and validate templates before sending. Configured does not mean live health was checked here.",
        "signing_settings", privileged)
    add("Time policy review", org.audit_events.filter(action="time_policy.updated").exists(),
        "Review rounding, overtime, geofence and approval policy before processing payroll.", "time_policy", privileged)
    add("Outbound messaging", bool(org.email_from),
        "Configure provider delivery and sender identity; this checklist does not test delivery.", "messaging_settings", privileged)
    return checks
