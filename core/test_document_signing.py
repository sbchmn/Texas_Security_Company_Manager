import copy
from io import StringIO
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, transaction, IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .document_signing import (
    DocuSealClient, allowed_origin, decrypt_api_key, encrypt_api_key, issue_signing_request,
    reconcile_signing_request, signing_evidence, validate_template, SubmissionRejected,
)
from .forms import OnboardingItemForm
from .models import (
    AuditEvent, ChannelAudience, ChannelRule, DocumentType, Membership, Notification, OnboardingItem, OnboardingTask, Organization,
    Person, PersonDocument, SignedArtifact, SigningRequest, SigningSettings,
)
from .services import decide_onboarding_task, onboarding_board, provision_onboarding_tasks


ORIGIN = "https://sign.example.com"
PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF"


@override_settings(
    DOCUSEAL_ALLOWED_ORIGINS=[ORIGIN],
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class DocumentSigningTest(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="signing-owner")
        self.employee = get_user_model().objects.create_user(username="signing-employee", email="employee@example.com")
        self.org = Organization.objects.create(legal_name="Signing LLC", display_name="Signing", slug="signing")
        Membership.objects.create(organization=self.org, user=self.owner, role=Membership.Role.OWNER)
        Membership.objects.create(organization=self.org, user=self.employee, role=Membership.Role.OFFICER)
        self.person = Person.objects.create(
            organization=self.org, user=self.employee, first_name="Alex", last_name="Guard",
            email="employee@example.com", status=Person.Status.ONBOARDING, hire_date=timezone.localdate(),
        )
        self.dtype = DocumentType.objects.create(organization=self.org, name="Signed handbook", code="signed-handbook")
        self.config = SigningSettings.objects.create(
            organization=self.org, base_url=ORIGIN, encrypted_api_key=encrypt_api_key("test-only-api-key"), enabled=True,
        )
        self.item = OnboardingItem.objects.create(
            organization=self.org, name="Sign handbook", code="sign-handbook", kind=OnboardingItem.Kind.SIGNATURE,
            document_type=self.dtype, signing_template_id=10, signing_template_name="Employee handbook",
        )
        self.task = provision_onboarding_tasks(self.person)[0]
        self.template = {
            "id": 10, "name": "Employee handbook", "updated_at": "2026-10-01T12:00:00Z", "archived_at": None,
            "submitters": [{"name": "Employee", "uuid": "employee-role"}],
            "fields": [{"uuid": "signature-field", "type": "signature", "required": True, "submitter_uuid": "employee-role"}],
            "schema": [{"name": "Employee handbook"}],
        }
        self.remote = None
        self.submission = None
        self.posts = 0
        api = patch.object(DocuSealClient, "api", side_effect=self.provider_api)
        self.api = api.start()
        self.addCleanup(api.stop)
        files = patch.object(DocuSealClient, "pdf", return_value=PDF)
        self.pdf = files.start()
        self.addCleanup(files.stop)

    def provider_api(self, method, path, *, params=None, payload=None):
        if path == "/templates/10":
            return copy.deepcopy(self.template)
        if path == "/templates":
            return {"data": [copy.deepcopy(self.template)], "pagination": {"count": 1}}
        if method == "POST" and path == "/submissions":
            self.posts += 1
            signer = payload["submitters"][0]
            self.remote = {
                "id": 20 + self.posts, "submission_id": 30 + self.posts, "slug": "signedLink",
                "external_id": signer["external_id"], "email": signer["email"], "role": signer["role"],
                "status": "awaiting", "completed_at": None, "declined_at": None,
            }
            self.submission = {
                "id": self.remote["submission_id"], "template": {"id": 10}, "status": "pending",
                "submitters": [copy.deepcopy(self.remote)], "documents": [], "audit_log_url": None,
            }
            return [copy.deepcopy(self.remote)]
        if path == "/submitters":
            rows = [copy.deepcopy(self.remote)] if self.remote else []
            return {"data": rows, "pagination": {"count": len(rows)}}
        if path.startswith("/submissions/"):
            return copy.deepcopy(self.submission)
        raise AssertionError(f"Unexpected provider call: {method} {path}")

    def issue(self):
        return issue_signing_request(self.task, self.owner)

    def complete_provider(self):
        now = timezone.now().isoformat()
        self.remote.update(status="completed", completed_at=now)
        self.submission.update(
            status="completed", completed_at=now,
            documents=[{"name": "Employee handbook", "url": ORIGIN + "/file/signed.pdf"}],
            audit_log_url=ORIGIN + "/file/audit.pdf",
        )
        signer = copy.deepcopy(self.remote)
        signer["fields"] = [{"uuid": "signature-field", "value": ORIGIN + "/file/signature.png"}]
        self.submission["submitters"] = [signer]

    def test_checklist_creation_never_sends(self):
        provision_onboarding_tasks(self.person)
        self.assertEqual(self.posts, 0)
        self.assertFalse(SigningRequest.objects.exists())

    def test_send_reserves_request_and_queues_invitation_without_completion(self):
        request = self.issue()
        self.assertEqual(request.status, SigningRequest.Status.SENT)
        self.assertEqual(request.attempt, 1)
        self.assertEqual(self.posts, 1)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, OnboardingTask.Status.OPEN)
        self.assertEqual(Notification.objects.filter(event_type="onboarding.signature_requested").count(), 2)
        post = next(call for call in self.api.call_args_list if call.args[0] == "POST")
        self.assertFalse(post.kwargs["payload"]["send_email"])
        self.assertFalse(post.kwargs["payload"]["send_sms"])
        self.assertEqual(post.kwargs["payload"]["submitters"][0]["external_id"], str(request.pk))

    def test_pending_does_not_complete_or_resend(self):
        request = self.issue()
        reconcile_signing_request(request.pk)
        with self.assertRaisesMessage(ValidationError, "already has"):
            self.issue()
        self.assertEqual(self.posts, 1)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, OnboardingTask.Status.OPEN)
        self.assertFalse(PersonDocument.objects.exists())

    def test_signed_pdfs_and_audit_are_filed_before_task_completion(self):
        request = self.issue()
        self.complete_provider()
        result = reconcile_signing_request(request.pk)
        self.assertEqual(result.status, SigningRequest.Status.COMPLETED)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, OnboardingTask.Status.DONE)
        self.assertIsNone(self.task.decided_by)
        self.assertEqual(SignedArtifact.objects.count(), 2)
        for artifact in result.artifacts.select_related("document"):
            self.assertEqual(artifact.document.person, self.person)
            self.assertEqual(artifact.document.document_type, self.dtype)
            self.assertEqual(artifact.document.scan_status, PersonDocument.ScanStatus.CLEAN)
            self.assertEqual(artifact.document.verified_type, "application/pdf")
            with artifact.document.file.open("rb") as stored:
                self.assertEqual(stored.read(), PDF)
        self.assertTrue(signing_evidence(self.task)["satisfied"])
        self.assertEqual(len(onboarding_board(self.org, self.person)["done"]), 1)
        self.assertTrue(AuditEvent.objects.filter(action="signing.completed").exists())

    def test_repeat_completion_is_idempotent(self):
        request = self.issue()
        self.complete_provider()
        reconcile_signing_request(request.pk)
        reconcile_signing_request(request.pk)
        self.assertEqual(SignedArtifact.objects.count(), 2)
        self.assertEqual(self.pdf.call_count, 2)
        self.assertEqual(AuditEvent.objects.filter(action="signing.completed").count(), 1)

    def test_manual_completion_cannot_bypass_signature(self):
        with self.assertRaisesMessage(ValidationError, "automatically"):
            decide_onboarding_task(self.task, self.owner, OnboardingTask.Status.DONE, "")
        self.client.force_login(self.employee)
        self.client.post(reverse("onboarding_task_decide", args=[self.task.pk]), {"action": "complete"})
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, OnboardingTask.Status.OPEN)

    def test_wrong_signer_template_or_external_reference_never_files(self):
        request = self.issue()
        self.complete_provider()
        original_remote = copy.deepcopy(self.remote)
        original_submission = copy.deepcopy(self.submission)
        mutations = ("email", "external_id", "role", "template", "submitter_id", "signature", "invalid_signature")
        for name in mutations:
            with self.subTest(mismatch=name):
                self.remote = copy.deepcopy(original_remote)
                self.submission = copy.deepcopy(original_submission)
                if name in ("email", "external_id", "role"):
                    self.remote[name] = "wrong"
                elif name == "template":
                    self.submission["template"]["id"] = 999
                elif name == "submitter_id":
                    self.submission["submitters"][0]["id"] = 999
                elif name == "invalid_signature":
                    self.submission["submitters"][0]["fields"][0]["value"] = True
                else:
                    self.submission["submitters"][0]["fields"] = []
                with self.assertRaises(ValidationError):
                    reconcile_signing_request(request.pk)
                self.assertFalse(PersonDocument.objects.exists())
                self.task.refresh_from_db()
                self.assertEqual(self.task.status, OnboardingTask.Status.OPEN)

    def test_missing_audit_or_different_documents_never_complete(self):
        request = self.issue()
        self.complete_provider()
        original = copy.deepcopy(self.submission)
        for name in ("audit_log_url", "documents"):
            with self.subTest(missing=name):
                self.submission = copy.deepcopy(original)
                self.submission[name] = None if name == "audit_log_url" else []
                with self.assertRaises(ValidationError):
                    reconcile_signing_request(request.pk)
                self.assertFalse(PersonDocument.objects.exists())

    def test_import_failure_rolls_back_files_and_recovers_on_retry(self):
        request = self.issue()
        self.complete_provider()
        self.pdf.side_effect = [PDF, ValidationError("Audit download failed.")]
        with self.assertRaises(ValidationError):
            reconcile_signing_request(request.pk)
        self.assertFalse(PersonDocument.objects.exists())
        self.assertFalse(SignedArtifact.objects.exists())
        from django.core.files.storage import default_storage
        directories, files = default_storage.listdir(f"private/{self.org.pk}/{self.person.pk}")
        self.assertEqual(files, [])
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, OnboardingTask.Status.OPEN)
        request.refresh_from_db()
        self.assertEqual(request.failures, 1)
        self.assertGreater(request.next_check_at, timezone.now())
        self.pdf.side_effect = None
        reconcile_signing_request(request.pk)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, OnboardingTask.Status.DONE)

    def test_scanner_failure_keeps_step_open(self):
        request = self.issue()
        self.complete_provider()
        with patch("core.services.malware_scan", side_effect=ValidationError("Malware scanner unavailable.")):
            with self.assertRaisesMessage(ValidationError, "scanner"):
                reconcile_signing_request(request.pk)
        self.assertFalse(PersonDocument.objects.exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, OnboardingTask.Status.OPEN)

    def test_failure_inside_record_store_does_not_orphan_pdf(self):
        request = self.issue()
        self.complete_provider()
        with patch("core.services.AuditEvent.objects.create", side_effect=IntegrityError("Injected audit failure")):
            with self.assertRaises(ValidationError):
                reconcile_signing_request(request.pk)
        self.assertFalse(PersonDocument.objects.exists())
        from django.core.files.storage import default_storage
        _, files = default_storage.listdir(f"private/{self.org.pk}/{self.person.pk}")
        self.assertEqual(files, [])

    def test_bad_provider_timestamp_is_retryable_and_does_not_file(self):
        request = self.issue()
        self.complete_provider()
        self.submission["completed_at"] = "2026-99-99T00:00:00Z"
        with self.assertRaisesMessage(ValidationError, "timestamp"):
            reconcile_signing_request(request.pk)
        request.refresh_from_db()
        self.assertTrue(request.last_error)
        self.assertFalse(PersonDocument.objects.exists())

    def test_definitive_post_rejection_allows_corrected_send(self):
        original = self.provider_api
        def reject_creation(method, path, **kwargs):
            if method == "POST":
                raise SubmissionRejected("DocuSeal API returned HTTP 422.")
            return original(method, path, **kwargs)
        self.api.side_effect = reject_creation
        with self.assertRaises(SubmissionRejected):
            self.issue()
        request = SigningRequest.objects.get()
        self.assertEqual(request.status, SigningRequest.Status.REJECTED)
        self.api.side_effect = original
        second = self.issue()
        self.assertEqual(second.attempt, 2)
        self.assertEqual(self.posts, 1)

    def test_ambiguous_creation_recovers_without_second_post(self):
        original = self.provider_api
        def create_then_timeout(method, path, **kwargs):
            result = original(method, path, **kwargs)
            if method == "POST":
                raise ValidationError("The creation response timed out.")
            return result
        self.api.side_effect = create_then_timeout
        with self.assertRaises(ValidationError):
            self.issue()
        request = SigningRequest.objects.get()
        self.assertEqual(request.status, SigningRequest.Status.PREPARING)
        self.api.side_effect = original
        reconcile_signing_request(request.pk)
        request.refresh_from_db()
        self.assertEqual(request.status, SigningRequest.Status.SENT)
        self.assertEqual(self.posts, 1)

    def test_unknown_creation_stays_visible_and_is_not_resent(self):
        self.api.side_effect = lambda method, path, **kwargs: (
            copy.deepcopy(self.template) if path.startswith("/templates/") else {"data": []}
        )
        with self.assertRaises(ValidationError):
            self.issue()
        request = SigningRequest.objects.get()
        with self.assertRaisesMessage(ValidationError, "not confirmed"):
            reconcile_signing_request(request.pk)
        with self.assertRaisesMessage(ValidationError, "already has"):
            self.issue()
        request.refresh_from_db()
        self.assertTrue(request.last_error)

    def test_declined_request_can_be_reissued_as_new_attempt(self):
        request = self.issue()
        self.submission["status"] = "declined"
        reconcile_signing_request(request.pk)
        second = self.issue()
        self.assertEqual(second.attempt, 2)
        self.assertNotEqual(second.pk, request.pk)
        self.assertEqual(SigningRequest.objects.count(), 2)

    def test_waived_step_is_not_overwritten_by_later_signature(self):
        request = self.issue()
        decide_onboarding_task(self.task, self.owner, OnboardingTask.Status.WAIVED, "Does not apply to this employee.")
        self.complete_provider()
        reconcile_signing_request(request.pk)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, OnboardingTask.Status.WAIVED)
        self.assertEqual(SignedArtifact.objects.count(), 2)

    def test_person_without_login_receives_email(self):
        self.person.user = None
        self.person.save(update_fields=["user"])
        self.task = OnboardingTask.objects.select_related("person").get(pk=self.task.pk)
        self.issue()
        notice = Notification.objects.get()
        self.assertIsNone(notice.recipient)
        self.assertEqual(notice.destination, self.person.email)

    def test_invitation_respects_subject_channel_rule_and_signer_email(self):
        ChannelRule.objects.create(
            organization=self.org, family="onboarding", audience=ChannelAudience.SUBJECT,
            channels=["email", "sms"], created_by=self.owner,
        )
        self.person.email = "personnel-signer@example.com"
        self.person.save(update_fields=["email"])
        self.task = OnboardingTask.objects.select_related("person__user", "item").get(pk=self.task.pk)
        self.issue()
        self.assertEqual(set(Notification.objects.values_list("channel", flat=True)), {"in_app", "email", "sms"})
        notice = Notification.objects.get(channel="email")
        self.assertEqual(notice.channel, "email")
        self.assertEqual(notice.destination, self.person.email)

    def test_multiple_template_documents_are_imported_with_one_audit(self):
        self.template["schema"].append({"name": "Additional terms.pdf"})
        request = self.issue()
        self.complete_provider()
        self.submission["documents"].append({"name": "Additional terms", "url": ORIGIN + "/file/terms.pdf"})
        reconcile_signing_request(request.pk)
        self.assertEqual(request.artifacts.count(), 3)
        self.assertTrue(signing_evidence(self.task)["satisfied"])

    def test_duplicate_provider_matches_never_file(self):
        request = self.issue()
        original = self.provider_api
        self.api.side_effect = lambda method, path, **kwargs: (
            {"data": [copy.deepcopy(self.remote), copy.deepcopy(self.remote)]}
            if path == "/submitters" else original(method, path, **kwargs)
        )
        with self.assertRaisesMessage(ValidationError, "Multiple"):
            reconcile_signing_request(request.pk)
        self.assertFalse(PersonDocument.objects.exists())

    def test_signing_details_are_hidden_if_record_read_access_changes(self):
        request = self.issue()
        request.last_error = "Private provider diagnostic"
        request.save(update_fields=["last_error"])
        self.dtype.sensitivity = DocumentType.Sensitivity.SEALED
        self.dtype.save()
        board = onboarding_board(self.org, self.person, reader=(Membership.Role.OFFICER, self.person.pk))
        self.assertEqual(board["rows"][0]["evidence"]["state"], "hidden")
        self.assertIsNone(board["rows"][0]["signing"])
        self.client.force_login(self.employee)
        response = self.client.get(reverse("my_onboarding"))
        self.assertNotContains(response, "Private provider diagnostic")
        self.assertNotContains(response, "Check signing status")

    def test_open_reports_removed_origin_instead_of_raising_server_error(self):
        self.issue()
        self.client.force_login(self.employee)
        with override_settings(DOCUSEAL_ALLOWED_ORIGINS=[]):
            response = self.client.get(reverse("signing_open", args=[self.task.pk]), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "DOCUSEAL_ALLOWED_ORIGINS")

    def test_settings_preserve_key_and_refuse_backend_change_with_open_request(self):
        self.issue()
        self.client.force_login(self.owner)
        self.client.post(reverse("signing_settings"), {"base_url": ORIGIN, "api_key": "", "enabled": "on"})
        self.config.refresh_from_db()
        self.assertEqual(decrypt_api_key(self.config.encrypted_api_key), "test-only-api-key")
        with override_settings(DOCUSEAL_ALLOWED_ORIGINS=[ORIGIN, "https://second.example.com"]):
            response = self.client.post(reverse("signing_settings"), {
                "base_url": "https://second.example.com", "api_key": "", "enabled": "on",
            })
        self.assertContains(response, "Outstanding signing requests")
        self.config.refresh_from_db()
        self.assertEqual(self.config.base_url, ORIGIN)

    def test_worker_surfaces_failure_and_records_retry(self):
        request = self.issue()
        self.remote = None
        output = StringIO()
        with self.assertRaises(CommandError):
            call_command("reconcile_signatures", request=str(request.pk), stdout=output, stderr=StringIO())
        self.assertIn("checked=0 failed=1", output.getvalue())
        request.refresh_from_db()
        self.assertEqual(request.failures, 1)

    def test_config_change_during_preflight_refuses_to_send(self):
        original = self.provider_api
        def disable_during_template_check(method, path, **kwargs):
            if path == "/templates/10":
                SigningSettings.objects.filter(pk=self.config.pk).update(enabled=False)
            return original(method, path, **kwargs)
        self.api.side_effect = disable_during_template_check
        with self.assertRaisesMessage(ValidationError, "configuration changed"):
            self.issue()
        self.assertEqual(self.posts, 0)
        self.assertFalse(SigningRequest.objects.exists())

    def test_encrypted_key_is_not_echoed_in_settings(self):
        self.assertNotIn("test-only-api-key", self.config.encrypted_api_key)
        self.assertEqual(decrypt_api_key(self.config.encrypted_api_key), "test-only-api-key")
        self.client.force_login(self.owner)
        response = self.client.get(reverse("signing_settings"))
        self.assertNotContains(response, "test-only-api-key")
        self.assertNotContains(response, self.config.encrypted_api_key)

    def test_employee_cannot_send_or_manage_backend(self):
        self.client.force_login(self.employee)
        self.assertEqual(self.client.post(reverse("signing_send", args=[self.task.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("signing_settings")).status_code, 403)
        self.assertEqual(self.posts, 0)

    def test_signer_can_open_link_and_check_status(self):
        request = self.issue()
        self.client.force_login(self.employee)
        response = self.client.get(reverse("signing_open", args=[self.task.pk]))
        self.assertRedirects(response, ORIGIN + "/s/signedLink", fetch_redirect_response=False)
        self.complete_provider()
        self.client.post(reverse("signing_refresh", args=[self.task.pk]))
        request.refresh_from_db()
        self.assertEqual(request.status, SigningRequest.Status.COMPLETED)
        self.assertContains(self.client.get(reverse("my_onboarding")), "Signed and filed")

    def test_staff_cannot_impersonate_signer(self):
        self.issue()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("signing_open", args=[self.task.pk])).status_code, 404)

    def test_cross_tenant_task_is_hidden(self):
        other = Organization.objects.create(legal_name="Other", display_name="Other", slug="other-signing")
        Membership.objects.filter(user=self.owner).update(organization=other)
        self.client.force_login(self.owner)
        for route in ("signing_send", "signing_refresh"):
            self.assertEqual(self.client.post(reverse(route, args=[self.task.pk])).status_code, 404)
        self.assertEqual(self.posts, 0)

    def test_signed_record_appears_in_personnel_file_with_existing_read_gates(self):
        request = self.issue()
        self.complete_provider()
        reconcile_signing_request(request.pk)
        self.client.force_login(self.employee)
        for artifact in request.artifacts.all():
            response = self.client.get(reverse("document_download", args=[artifact.document_id]))
            self.assertEqual(response.status_code, 200)
        page = self.client.get(reverse("person_detail", args=[self.person.pk]) + "?tab=documents")
        self.assertContains(page, "signed-")

    def test_sealed_record_cannot_be_sent_to_subject(self):
        self.dtype.sensitivity = DocumentType.Sensitivity.SEALED
        self.dtype.save()
        with self.assertRaisesMessage(ValidationError, "readable"):
            self.issue()
        self.assertFalse(SigningRequest.objects.exists())

    def test_form_selects_template_and_rejects_signature_without_record(self):
        data = {
            "name": "Sign terms", "code": "sign-terms", "kind": "signature", "owner": "person",
            "due_within_days": 7, "signing_template_id": 10, "order": 0, "active": True,
        }
        form = OnboardingItemForm(data, organization=self.org)
        self.assertFalse(form.is_valid())
        self.assertIn("document_type", form.errors)
        data["document_type"] = self.dtype.pk
        form = OnboardingItemForm(data, organization=self.org)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.signing_template_name, "Employee handbook")

    def test_issued_mapping_cannot_be_changed(self):
        self.issue()
        data = {
            "name": self.item.name, "code": self.item.code, "kind": "task", "owner": "person",
            "document_type": self.dtype.pk, "due_within_days": 7, "order": 0, "active": True,
        }
        form = OnboardingItemForm(data, instance=self.item, organization=self.org)
        self.assertFalse(form.is_valid())
        self.assertIn("kind", form.errors)

    def test_issued_step_can_be_deactivated_without_a_live_backend(self):
        self.issue()
        SigningSettings.objects.filter(pk=self.config.pk).update(enabled=False)
        self.dtype.active = False
        self.dtype.save(update_fields=["active"])
        form = OnboardingItemForm({
            "name": self.item.name, "code": self.item.code, "kind": "signature", "owner": "person",
            "document_type": self.dtype.pk, "signing_template_id": 10, "due_within_days": 7,
            "order": 0, "active": False,
        }, instance=self.item, organization=self.org)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.item.refresh_from_db()
        self.assertFalse(self.item.active)

    def test_worker_imports_completed_submission(self):
        request = self.issue()
        self.complete_provider()
        output = StringIO()
        call_command("reconcile_signatures", request=str(request.pk), stdout=output)
        self.assertIn("checked=1 failed=0", output.getvalue())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, OnboardingTask.Status.DONE)

    def test_explicit_worker_request_reports_unknown_id(self):
        from uuid import uuid4
        with self.assertRaisesMessage(CommandError, "not found"):
            call_command("reconcile_signatures", request=str(uuid4()), stdout=StringIO())

    def test_model_and_mysql_reference_guards(self):
        request = self.issue()
        other = Organization.objects.create(legal_name="Other", display_name="Other", slug="other-guard")
        invalid = SigningRequest(
            organization=other, task=self.task, attempt=2, document_type=self.dtype, template_id=10,
            signer_email=self.person.email, signer_name=self.person.full_name, base_url=ORIGIN,
        )
        with self.assertRaises(ValidationError):
            invalid.clean()
        if connection.vendor == "mysql":
            with self.assertRaises(DatabaseError), transaction.atomic():
                invalid.save()
            with self.assertRaises(DatabaseError), transaction.atomic():
                SigningRequest.objects.filter(pk=request.pk).update(organization=other)

    def test_artifact_cannot_reference_a_different_person(self):
        request = self.issue()
        self.complete_provider()
        reconcile_signing_request(request.pk)
        other = Person.objects.create(organization=self.org, first_name="Other", last_name="Employee")
        wrong = PersonDocument.objects.create(
            organization=self.org, person=other, document_type=self.dtype,
            file="private/no-file.pdf", original_name="no-file.pdf", size=len(PDF), sha256="0" * 64,
        )
        artifact = SignedArtifact(request=request, document=wrong, key="wrong", name="Wrong person")
        with self.assertRaises(ValidationError):
            artifact.clean()
        if connection.vendor == "mysql":
            with self.assertRaises(DatabaseError), transaction.atomic():
                artifact.save()
            existing = request.artifacts.first()
            with self.assertRaises(DatabaseError), transaction.atomic():
                SignedArtifact.objects.filter(pk=existing.pk).update(document=wrong)


class Response:
    def __init__(self, payload, status=200, headers=None):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, chunk_size):
        yield self.payload


@override_settings(DOCUSEAL_ALLOWED_ORIGINS=[ORIGIN])
class DocuSealTransportTest(TestCase):
    def setUp(self):
        self.client_api = DocuSealClient(SigningSettings(
            base_url=ORIGIN, encrypted_api_key=encrypt_api_key("transport-test-key"),
        ))

    def test_only_operator_allowed_https_origins_are_accepted(self):
        for value in ("http://sign.example.com", "https://evil.example.com", ORIGIN + "/api", "https://user:pass@sign.example.com"):
            with self.subTest(origin=value), self.assertRaises(ValidationError):
                allowed_origin(value)

    def test_api_authentication_and_redirect_policy(self):
        with patch("core.document_signing.requests.request", return_value=Response(b'{"data":[]}')) as network:
            self.assertEqual(self.client_api.api("GET", "/templates"), {"data": []})
        self.assertEqual(network.call_args.kwargs["headers"], {"X-Auth-Token": "transport-test-key"})
        self.assertEqual(network.call_args.args[1], ORIGIN + "/api/templates")
        self.assertFalse(network.call_args.kwargs["allow_redirects"])

    def test_api_timeout_and_bad_response_are_explicit(self):
        for response in (Response(b"not-json"), Response(b"{}", status=302), Response(b"{}", status=401)):
            with self.subTest(response=response.status_code):
                with patch("core.document_signing.requests.request", return_value=response), self.assertRaises(ValidationError):
                    self.client_api.api("GET", "/templates")
        with patch("core.document_signing.requests.request", side_effect=requests.Timeout("private-token")), self.assertRaises(ValidationError) as failure:
            self.client_api.api("GET", "/templates")
        self.assertNotIn("private-token", str(failure.exception))
        with patch("core.document_signing.requests.request", side_effect=requests.exceptions.SSLError()):
            with self.assertRaisesMessage(ValidationError, "certificate verification failed"):
                self.client_api.api("GET", "/templates")

    def test_file_redirect_cannot_leave_signing_origin(self):
        redirect = Response(b"", status=302, headers={"Location": "https://evil.example.com/file.pdf"})
        with patch("core.document_signing.requests.get", return_value=redirect) as network:
            with self.assertRaisesMessage(ValidationError, "configured"):
                self.client_api.pdf(ORIGIN + "/file/pdf")
        self.assertEqual(network.call_count, 1)
        self.assertNotIn("headers", network.call_args.kwargs)

    def test_pdf_validation_size_limit_and_same_origin_redirect(self):
        responses = [Response(b"", status=302, headers={"Location": "/file/final.pdf"}), Response(PDF)]
        with patch("core.document_signing.requests.get", side_effect=responses):
            self.assertEqual(self.client_api.pdf(ORIGIN + "/file/initial"), PDF)
        with patch("core.document_signing.requests.get", return_value=Response(b"<html>")), self.assertRaises(ValidationError):
            self.client_api.pdf(ORIGIN + "/file/pdf")
        with patch("core.document_signing.DOCUMENT_MAX_BYTES", 5):
            with patch("core.document_signing.requests.get", return_value=Response(PDF)), self.assertRaises(ValidationError):
                self.client_api.pdf(ORIGIN + "/file/pdf")
        with patch("core.document_signing.requests.get", side_effect=requests.exceptions.SSLError()):
            with self.assertRaisesMessage(ValidationError, "certificate verification failed"):
                self.client_api.pdf(ORIGIN + "/file/pdf")

    def test_template_requires_one_signer_and_signature(self):
        invalid = {"id": 1, "submitters": [{"name": "Employee", "uuid": "role"}], "fields": [], "schema": [{"name": "Handbook"}]}
        with self.assertRaises(ValidationError):
            validate_template(invalid)
