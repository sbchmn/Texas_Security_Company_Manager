import inspect
from pathlib import Path
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from django import forms
from django.contrib.auth import get_user_model
from django.db import models
from django.conf import settings
from django.template import Variable, VariableDoesNotExist
from django.template.loader import get_template
from django.test import TestCase
from django.urls import resolve, reverse
from django.utils import timezone
from unittest.mock import patch

from . import forms as app_forms
from .models import (
    AuditEvent, AuthorityScope, Branch, Checkpoint, Client, Credential, CredentialType, DocumentType,
    CustomFieldDefinition, Membership, Organization, PayrollRun, Person, PersonCustomValue,
    PersonDocument, Punch, PunchAdjustment, Shift,
    ShiftTemplate, Site, TimePolicy, TrainingRecord,
)


class FormControlParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.labels = set()
        self.controls = []
        self.label_depth = 0
        self.nested_labels = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.append(attrs["id"])
        if tag == "label":
            if self.label_depth:
                self.nested_labels.append(attrs)
            self.label_depth += 1
            if attrs.get("for"):
                self.labels.add(attrs["for"])
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag in ("input", "select", "textarea") and attrs.get("type") != "hidden":
            self.controls.append((attrs, self.label_depth > 0))

    def handle_endtag(self, tag):
        if tag == "label":
            self.label_depth = max(0, self.label_depth - 1)


class WorkflowFormUITest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(legal_name="UI Security", slug="ui-security")
        cls.user = get_user_model().objects.create_user(username="ui-owner@example.com")
        Membership.objects.create(organization=cls.org, user=cls.user, role=Membership.Role.OWNER)
        cls.branch = Branch.objects.create(organization=cls.org, name="Dallas")
        cls.person = Person.objects.create(
            organization=cls.org, branch=cls.branch, user=cls.user,
            first_name="Taylor", last_name="Reed", employee_id="TX-104",
        )
        cls.client_record = Client.objects.create(organization=cls.org, name="Harbor")
        cls.site = Site.objects.create(organization=cls.org, client=cls.client_record, name="North gate")
        cls.shift = Shift.objects.create(
            organization=cls.org, site=cls.site, officer=cls.person,
            starts_at=timezone.now() + timedelta(days=2),
            ends_at=timezone.now() + timedelta(days=2, hours=8), post_name="Night patrol",
        )
        cls.document_type = DocumentType.objects.create(organization=cls.org, name="Handbook")
        cls.credential_type = CredentialType.objects.create(
            organization=cls.org, name="Level III", code="level-iii", registry_check_within_days=30,
        )
        cls.credential = Credential.objects.create(
            organization=cls.org, person=cls.person, credential_type=cls.credential_type,
            number="LIC-104",
        )
        cls.training = TrainingRecord.objects.create(
            organization=cls.org, person=cls.person, course_name="First aid",
            completed_on=timezone.localdate(), hours=4,
        )
        cls.checkpoint = Checkpoint.objects.create(
            organization=cls.org, site=cls.site, name="Gate scan", latitude=32, longitude=-96,
        )
        cls.template = ShiftTemplate.objects.create(
            organization=cls.org, site=cls.site, name="Weekday patrol",
            start_time=datetime(2026, 10, 9, 8).time(), end_time=datetime(2026, 10, 9, 16).time(),
            series_start=timezone.localdate(), weekdays=[0, 1, 2, 3, 4],
        )
        cls.document = PersonDocument.objects.create(
            organization=cls.org, person=cls.person, document_type=cls.document_type,
            original_name="handbook.pdf", file="audit-only/handbook.pdf", size=100, sha256="0" * 64,
            content_type="application/pdf", scan_status=PersonDocument.ScanStatus.CLEAN,
        )
        cls.archived_document = PersonDocument.objects.create(
            organization=cls.org, person=cls.person, document_type=cls.document_type,
            original_name="archived-handbook.pdf", file="audit-only/archived-handbook.pdf",
            size=100, sha256="1" * 64, content_type="application/pdf",
            scan_status=PersonDocument.ScanStatus.CLEAN, archived_at=timezone.now(),
        )
        cls.open_shift = Shift.objects.create(
            organization=cls.org, site=cls.site, status=Shift.Status.PUBLISHED,
            starts_at=timezone.now() + timedelta(days=1), ends_at=timezone.now() + timedelta(days=1, hours=8),
        )
        cls.punch = Punch.objects.create(
            organization=cls.org, person=cls.person, shift=cls.shift, kind=Punch.Kind.IN,
            occurred_at=timezone.now(), client_event_id=uuid.uuid4(), review_status=Punch.Review.PENDING,
        )
        cls.adjustment = PunchAdjustment.objects.create(
            organization=cls.org, punch=cls.punch, requested_by=cls.user,
            proposed_at=timezone.now(), reason="Correct the recorded time",
        )
        cls.payroll_run = PayrollRun.objects.create(
            organization=cls.org, created_by=cls.user,
            period_start=timezone.now() - timedelta(days=14), period_end=timezone.now(),
        )

    def test_every_related_model_used_by_a_form_has_a_readable_label(self):
        for name, form_class in inspect.getmembers(app_forms, inspect.isclass):
            if not issubclass(form_class, forms.BaseForm) or form_class.__module__ != app_forms.__name__:
                continue
            for field_name, field in getattr(form_class, "base_fields").items():
                if isinstance(field, forms.ModelChoiceField) and field.queryset is not None:
                    model = field.queryset.model
                    with self.subTest(form=name, field=field_name, model=model.__name__):
                        self.assertIsNot(model.__str__, models.Model.__str__)

    def test_edit_shift_choices_name_officer_and_tour_and_do_not_offer_self_relief(self):
        self.client.force_login(self.user)
        page = self.client.get(reverse("shift_edit", args=[self.shift.pk]))
        self.assertContains(page, "Taylor Reed")
        self.assertContains(page, "TX-104")
        self.assertNotContains(page, "Person object (")
        self.assertNotContains(page, "Shift object (")
        self.assertNotIn(self.shift, page.context["form"].fields["relief_for"].queryset)

    def test_site_picker_groups_by_client_and_selected_label_is_only_site_name(self):
        self.client.force_login(self.user)
        page = self.client.get(reverse("shift_edit", args=[self.shift.pk]))
        self.assertContains(page, '<optgroup label="Harbor">')
        self.assertContains(page, f'<option value="{self.site.pk}" selected>North gate</option>')
        self.assertNotContains(page, "Harbor — North gate</option>")

    def test_grouped_site_picker_preserves_scope_and_rejects_outside_selection(self):
        other_client = Client.objects.create(organization=self.org, name="Outside client")
        outside = Site.objects.create(organization=self.org, client=other_client, name="Outside site")
        dispatcher = get_user_model().objects.create_user(username="ui-dispatcher@example.com")
        membership = Membership.objects.create(
            organization=self.org, user=dispatcher, role=Membership.Role.SCHEDULER,
        )
        AuthorityScope.objects.create(organization=self.org, membership=membership, site=self.site)
        self.client.force_login(dispatcher)
        page = self.client.get(reverse("shift_create"))
        self.assertContains(page, '<optgroup label="Harbor">')
        self.assertNotContains(page, "Outside client")
        self.assertNotIn(outside, page.context["form"].fields["site"].queryset)
        page = self.client.post(reverse("shift_create"), {
            "site": outside.pk, "starts_at": self.shift.starts_at.isoformat(),
            "ends_at": self.shift.ends_at.isoformat(), "status": Shift.Status.DRAFT,
        })
        self.assertEqual(page.status_code, 200)
        self.assertIn("site", page.context["form"].errors)
        self.assertFalse(Shift.objects.filter(site=outside).exists())

    def test_recurring_template_edit_saves_and_audits_model_not_helper_fields(self):
        self.client.force_login(self.user)
        page = self.client.post(reverse("shift_template_edit", args=[self.template.pk]), {
            "name": "Updated weekday patrol", "site": self.site.pk,
            "start_time": "09:00", "end_time": "17:00", "pattern": ShiftTemplate.Pattern.WEEKLY,
            "weekdays": ["0", "1"], "series_start": self.template.series_start.isoformat(),
            "required_credentials": [self.credential_type.pk], "active": "on",
        })
        self.assertEqual(page.status_code, 302)
        self.template.refresh_from_db()
        self.assertEqual(self.template.name, "Updated weekday patrol")
        event = AuditEvent.objects.get(target_id=str(self.template.pk))
        changes = event.metadata["changes"]
        self.assertEqual(changes["name"]["after"], "Updated weekday patrol")
        self.assertEqual(changes["required_credentials"], {
            "before": [], "after": [str(self.credential_type.pk)],
        })
        self.assertNotIn("preset", changes)

    def test_role_specific_actions_do_not_link_to_forbidden_settings(self):
        for role, page_name in ((Membership.Role.PAYROLL, "payroll"),
                                (Membership.Role.SCHEDULER, "clock_kiosks"),
                                (Membership.Role.SUPERVISOR, "clock_kiosks")):
            with self.subTest(role=role):
                Membership.objects.filter(user=self.user).update(role=role)
                self.client.force_login(self.user)
                page = self.client.get(reverse(page_name))
                self.assertEqual(page.status_code, 200)
                self.assertNotContains(page, f'href="{reverse("time_policy")}"')
                if role == Membership.Role.PAYROLL:
                    self.assertNotContains(page, f'href="{reverse("pay_codes")}"')
                    self.assertContains(page, f'href="{reverse("settings_pay_categories")}"')

    def test_locked_payroll_reopen_controls_have_labels(self):
        self.payroll_run.status = PayrollRun.Status.APPROVED
        self.payroll_run.save()
        TimePolicy.objects.create(organization=self.org, allow_reopen=True)
        self.client.force_login(self.user)
        page = self.client.get(reverse("payroll"))
        self.assertContains(page, "Reason to reopen")
        self.assert_labeled_controls(page)

    def test_invalid_clock_pin_is_a_field_error_not_a_server_error(self):
        self.client.force_login(self.user)
        page = self.client.post(reverse("clock_pin"), {"pin": "0000", "confirm": "0000"})
        self.assertEqual(page.status_code, 200)
        self.assertIn("pin", page.context["form"].errors)
        self.assertContains(page, 'aria-invalid="true"')
        self.assertNotContains(page, 'value="0000"')

    def test_self_profile_hides_history_and_uses_personal_workflow_links(self):
        Membership.objects.filter(user=self.user).update(role=Membership.Role.OFFICER)
        self.client.force_login(self.user)
        url = reverse("person_detail", args=[self.person.pk])
        profile = self.client.get(url)
        self.assertNotContains(profile, f'href="{url}?tab=history"')
        time = self.client.get(url + "?tab=time")
        self.assertContains(time, f'href="{reverse("availability")}"')
        self.assertContains(time, f'href="{reverse("my_time_off")}"')
        self.assertNotContains(time, f'href="{reverse("person_availability", args=[self.person.pk])}"')

    def test_profile_displays_second_address_line_and_valid_zero_and_false_values(self):
        self.person.address_line2 = "Suite 104"
        self.person.hourly_rate = Decimal("0")
        self.person.save()
        for key, kind, value in (("counter", CustomFieldDefinition.Kind.NUMBER, 0),
                                 ("flag", CustomFieldDefinition.Kind.BOOLEAN, False)):
            definition = CustomFieldDefinition.objects.create(
                organization=self.org, name=key, key=key, kind=kind,
            )
            PersonCustomValue.objects.create(
                organization=self.org, person=self.person, definition=definition, value=value,
            )
        self.client.force_login(self.user)
        page = self.client.get(reverse("person_detail", args=[self.person.pk]))
        self.assertContains(page, "Suite 104")
        self.assertContains(page, "0.00/hr")
        self.assertContains(page, "<dd>0</dd>")
        self.assertContains(page, "<dd>No</dd>")

    def test_native_dates_and_times_use_valid_html_initial_values_in_all_forms(self):
        for name, form_class in inspect.getmembers(app_forms, inspect.isclass):
            if not issubclass(form_class, forms.BaseForm) or form_class.__module__ != app_forms.__name__:
                continue
            if name == "OnboardingItemForm":
                form = app_forms.OnboardingItemForm(organization=self.org)
            else:
                form = form_class()
            for field_name, field in form.fields.items():
                input_type = field.widget.attrs.get("type")
                if input_type not in ("date", "time", "datetime-local"):
                    continue
                value = datetime(2026, 10, 9, 18, 30)
                form.initial[field_name] = value.date() if input_type == "date" else value.time() if input_type == "time" else value
                expected = {"date": "2026-10-09", "time": "18:30", "datetime-local": "2026-10-09T18:30"}[input_type]
                with self.subTest(form=name, field=field_name):
                    self.assertIn(f'value="{expected}"', str(form[field_name]))

    def test_generic_form_renders_labels_help_and_workflow_sections(self):
        self.client.force_login(self.user)
        for name, sections in (("person_create", ("Identity", "Contact", "Employment")),
                               ("shift_create", ("Assignment", "Requirements", "Pay"))):
            with self.subTest(page=name):
                page = self.client.get(reverse(name))
                self.assertEqual(page.status_code, 200)
                for section in sections:
                    self.assertContains(page, section)
                parser = FormControlParser()
                parser.feed(page.content.decode())
                self.assertFalse(parser.nested_labels)
                for attrs, wrapped in parser.controls:
                    self.assertTrue(wrapped or attrs.get("id") in parser.labels, attrs)
                self.assertEqual(len(parser.ids), len(set(parser.ids)))

    def test_invalid_form_shows_field_linked_summary_and_keeps_entered_values(self):
        self.client.force_login(self.user)
        page = self.client.post(reverse("shift_create"), {"site": self.site.pk, "post_name": "Keep my post"})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'class="form-error-summary"')
        self.assertContains(page, 'href="#id_starts_at"')
        self.assertContains(page, 'value="Keep my post"')
        self.assertContains(page, 'aria-invalid="true"')

    def test_all_project_templates_compile(self):
        root = Path(settings.BASE_DIR) / "templates"
        for path in root.rglob("*.html"):
            with self.subTest(template=str(path.relative_to(root))):
                get_template(path.relative_to(root).as_posix())

    def page_targets(self):
        from .urls import urlpatterns

        excluded = {"ready", "brand_logo", "platform_admin", "messaging_rule_add"}
        targets = [
            (route.name, reverse(route.name))
            for route in urlpatterns
            if not route.pattern.converters and route.name not in excluded
        ]
        records = {
            "person_id": self.person.pk, "shift_id": self.shift.pk, "branch_id": self.branch.pk,
            "client_id": self.client_record.pk, "site_id": self.site.pk, "checkpoint_id": self.checkpoint.pk,
            "credential_id": self.credential.pk, "type_id": self.credential_type.pk,
            "record_id": self.training.pk, "template_id": self.template.pk,
            "membership_id": Membership.objects.get(organization=self.org, user=self.user).pk,
        }
        names = (
            "person_edit", "person_access_invite", "person_access_link", "membership_access", "person_availability", "person_credential_create",
            "person_training_create", "person_document_upload", "person_pin_issue",
            "shift_edit", "shift_requests", "shift_hours", "offer_post", "branch_edit",
            "client_edit", "site_edit", "checkpoint_create", "checkpoint_edit",
            "credential_edit", "credential_type_edit", "training_edit",
            "shift_template_edit", "shift_template_generate",
        )
        for route in urlpatterns:
            if route.name in names:
                kwargs = {key: records[key] for key in route.pattern.converters}
                targets.append((route.name, reverse(route.name, kwargs=kwargs)))
        for tab in ("profile", "onboarding", "credentials", "training", "documents", "time", "history"):
            targets.append((f"person_detail:{tab}", reverse("person_detail", args=[self.person.pk]) + f"?tab={tab}"))
        targets.append(("shift_hours:open", reverse("shift_hours", args=[self.open_shift.pk])))
        targets.append(("email_template_edit", reverse("email_template_edit", args=["shift.published"])))
        targets.append(("sms_template_edit", reverse("sms_template_edit", args=["shift.published"])))
        return targets

    def test_workflow_pages_render_without_internal_labels_or_broken_model_fields(self):
        self.client.force_login(self.user)
        original = getattr(Variable, "_resolve_lookup")
        failures = set()

        def track_lookup(variable, context):
            try:
                return original(variable, context)
            except VariableDoesNotExist as error:
                params = getattr(error, "params", None)
                if isinstance(params, (tuple, list)) and len(params) == 2:
                    key, value = params
                    if isinstance(value, models.Model) and not hasattr(value, key):
                        failures.add(f"{value.__class__.__name__}.{key}")
                raise

        for name, url in self.page_targets():
            with self.subTest(page=name), patch.object(Variable, "_resolve_lookup", track_lookup):
                failures.clear()
                page = self.client.get(url)
                self.assertIn(page.status_code, (200, 302, 403, 405))
                if page.status_code == 200 and page.headers.get("Content-Type", "").startswith("text/html"):
                    text = page.content.decode()
                    self.assertNotRegex(text, r"\b[A-Z][A-Za-z]+ object \([0-9a-f-]+\)")
                    self.assertNotIn("{#", text)
                    self.assertFalse(failures, sorted(failures))

    def assert_labeled_controls(self, page):
        parser = FormControlParser()
        parser.feed(page.content.decode())
        problems = [
            attrs.get("name", attrs.get("id", "unnamed"))
            for attrs, wrapped in parser.controls
            if attrs.get("type") not in ("submit", "button")
            and not (wrapped or attrs.get("id") in parser.labels
                     or attrs.get("aria-label") or attrs.get("aria-labelledby"))
        ]
        self.assertFalse(problems, problems)
        self.assertFalse(parser.nested_labels, parser.nested_labels)
        self.assertEqual(len(parser.ids), len(set(parser.ids)), "Duplicate IDs make label associations ambiguous.")

    def test_page_controls_are_labeled_and_do_not_nest_labels(self):
        self.client.force_login(self.user)
        for name, url in self.page_targets():
            with self.subTest(page=name):
                page = self.client.get(url)
                if page.status_code != 200 or not page.headers.get("Content-Type", "").startswith("text/html"):
                    continue
                self.assert_labeled_controls(page)

    def test_populated_workflow_pages_render_for_every_membership_role(self):
        membership = Membership.objects.get(user=self.user)
        for role in Membership.Role.values:
            membership.role = role
            membership.save()
            self.client.force_login(self.user)
            for name, url in self.page_targets():
                with self.subTest(role=role, page=name):
                    page = self.client.get(url)
                    allowed = (200, 302, 403, 405)
                    if name in ("person_availability", "person_detail:history") and role not in (
                            Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR,
                            Membership.Role.SUPERVISOR, Membership.Role.SCHEDULER):
                        allowed += (404,)
                    self.assertIn(page.status_code, allowed)
                    if page.status_code == 200 and page.headers.get("Content-Type", "").startswith("text/html"):
                        self.assert_labeled_controls(page)
                        self.assertNotRegex(page.content.decode(), r"\b[A-Z][A-Za-z]+ object \([0-9a-f-]+\)")

    def test_visible_workflow_links_are_accessible_to_the_role_shown(self):
        membership = Membership.objects.get(user=self.user)
        for role in Membership.Role.values:
            membership.role = role
            membership.save()
            self.client.force_login(self.user)
            seen = set()
            for name, url in self.page_targets():
                page = self.client.get(url)
                if page.status_code != 200 or not page.headers.get("Content-Type", "").startswith("text/html"):
                    continue
                parser = FormControlParser()
                parser.feed(page.content.decode())
                for href in parser.links:
                    target = urlsplit(urljoin("http://testserver" + url, href))
                    if target.netloc != "testserver" or href.startswith("#"):
                        continue
                    destination = target.path + ("?" + target.query if target.query else "")
                    if destination in seen:
                        continue
                    seen.add(destination)
                    match = resolve(target.path)
                    if match.url_name in ("document_preview", "document_download", "person_export",
                                          "payroll_export", "payroll_run_export", "audit_export",
                                          "report_snapshot_download", "import_template", "import_errors"):
                        continue
                    with self.subTest(role=role, page=name, link=destination):
                        linked = self.client.get(destination)
                        self.assertNotIn(linked.status_code, (403, 404, 405))
