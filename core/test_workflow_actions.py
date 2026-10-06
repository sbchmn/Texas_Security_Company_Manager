from datetime import timedelta
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    AuthorityScope, Branch, Client, CredentialType, DocumentType, Membership, OnboardingItem, OnboardingTask,
    Organization, Person, PersonDocument, Shift, ShiftSwap, Site,
    SignedArtifact, SigningRequest, SigningSettings, TimeOffRequest,
)


class WorkflowActionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(legal_name="Action Security", slug="action-security")
        cls.branch = Branch.objects.create(organization=cls.org, name="Covered")
        cls.hidden_branch = Branch.objects.create(organization=cls.org, name="Other")
        cls.users = {}
        for role in (Membership.Role.OWNER, Membership.Role.HR, Membership.Role.SCHEDULER,
                     Membership.Role.SUPERVISOR, Membership.Role.OFFICER, Membership.Role.AUDITOR):
            user = get_user_model().objects.create_user(username=f"actions-{role}", email=f"{role}@example.com")
            member = Membership.objects.create(organization=cls.org, user=user, role=role)
            if role in (Membership.Role.SCHEDULER, Membership.Role.SUPERVISOR):
                AuthorityScope.objects.create(organization=cls.org, membership=member, branch=cls.branch)
            cls.users[role] = user
        cls.person = Person.objects.create(
            organization=cls.org, branch=cls.branch, user=cls.users[Membership.Role.OFFICER],
            first_name="Visible", last_name="Active", email="visible@example.com",
            status=Person.Status.ACTIVE, hire_date=timezone.localdate(),
        )
        cls.hidden_person = Person.objects.create(
            organization=cls.org, branch=cls.hidden_branch, first_name="Hidden", last_name="Worker",
            status=Person.Status.ACTIVE,
        )
        cls.dtype = DocumentType.objects.create(organization=cls.org, name="Agreement")
        cls.item = OnboardingItem.objects.create(organization=cls.org, name="Office task", code="office",
                                                owner=OnboardingItem.Owner.STAFF)
        cls.task = OnboardingTask.objects.create(organization=cls.org, person=cls.person, item=cls.item,
                                                due_on=timezone.localdate() - timedelta(days=2))
        cls.sign_item = OnboardingItem.objects.create(
            organization=cls.org, name="Sign agreement", code="agreement",
            kind=OnboardingItem.Kind.SIGNATURE, document_type=cls.dtype,
            signing_template_id=1, owner=OnboardingItem.Owner.PERSON,
        )
        cls.sign_task = OnboardingTask.objects.create(organization=cls.org, person=cls.person, item=cls.sign_item)
        cls.config = SigningSettings.objects.create(
            organization=cls.org, base_url="https://sign.example.com", enabled=True, encrypted_api_key="configured",
        )
        client = Client.objects.create(organization=cls.org, name="Action client")
        cls.site = Site.objects.create(organization=cls.org, client=client, name="Covered site", branch=cls.branch)
        cls.hidden_site = Site.objects.create(organization=cls.org, client=client, name="Other site", branch=cls.hidden_branch)

    def login(self, role=Membership.Role.OWNER):
        self.client.force_login(self.users[role])

    def signing(self, status, **kwargs):
        return SigningRequest.objects.create(
            organization=self.org, task=self.sign_task, document_type=self.dtype,
            template_id=1, template_snapshot={"documents": ["Agreement"]}, attempt=1,
            status=status, signer_email=self.person.email, signer_name=self.person.full_name,
            base_url=self.config.base_url, **kwargs,
        )

    def post(self, site=None, officer=None, status=Shift.Status.PUBLISHED):
        starts_at = timezone.now() + timedelta(days=1)
        return Shift.objects.create(organization=self.org, site=site or self.site, officer=officer,
                                    status=status, starts_at=starts_at, ends_at=starts_at + timedelta(hours=8))

    def test_active_employee_outstanding_steps_and_filters_share_today_counts(self):
        self.login()
        page = self.client.get(reverse("workspace_people"))
        self.assertContains(page, "Visible Active")
        self.assertContains(page, f"#task-{self.task.pk}")
        self.assertEqual(page.context["onboarding_actions"]["overdue"], 1)
        office = self.client.get(reverse("workspace_people") + "?work=office")
        self.assertTrue(all(row["owner"] != "Employee" for row in office.context["onboarding_queue"]))
        overdue = self.client.get(reverse("workspace_people") + "?work=overdue")
        self.assertEqual(len(overdue.context["onboarding_queue"]), 1)
        today = self.client.get(reverse("workspace_today"))
        summary = next(row for row in today.context["work_priorities"] if row["label"] == "Outstanding onboarding")
        self.assertEqual(summary["count"], page.context["onboarding_actions"]["total"])
        self.task.status = OnboardingTask.Status.DONE
        self.task.save()
        overdue = self.client.get(reverse("workspace_people") + "?work=overdue")
        self.assertEqual(len(overdue.context["onboarding_queue"]), 0)

    def test_onboarding_queue_is_scoped_and_employee_cannot_open_management_queue(self):
        OnboardingTask.objects.create(organization=self.org, person=self.hidden_person, item=self.item)
        self.login(Membership.Role.SCHEDULER)
        page = self.client.get(reverse("workspace_people"))
        self.assertNotContains(page, "Hidden Worker")
        self.assertNotContains(page, reverse("signing_queue"))
        self.assertEqual(self.client.get(reverse("signing_queue")).status_code, 403)
        self.login(Membership.Role.OFFICER)
        self.assertEqual(self.client.get(reverse("workspace_people")).status_code, 403)

    def test_signing_stages_failure_and_safe_return_to_queue(self):
        self.login(Membership.Role.HR)
        page = self.client.get(reverse("signing_queue") + "?stage=ready")
        self.assertEqual(page.context["queue"].paginator.count, 1)
        with patch("core.signing_views.issue_signing_request") as send:
            result = self.client.post(reverse("signing_send", args=[self.sign_task.pk]),
                                      {"return_to": "signing_queue"})
            self.assertRedirects(result, reverse("signing_queue"))
            send.assert_called_once()
        signing = self.signing(SigningRequest.Status.PREPARING)
        page = self.client.get(reverse("signing_queue") + "?stage=processing")
        self.assertEqual(page.context["queue"].paginator.count, 1)
        self.assertNotContains(page, "Send signing request")
        signing.status = SigningRequest.Status.SENT
        signing.save()
        self.assertEqual(self.client.get(reverse("signing_queue") + "?stage=awaiting").context["queue"].paginator.count, 1)
        signing.last_error = "Could not verify signed PDFs"
        signing.save()
        page = self.client.get(reverse("signing_queue") + "?stage=failed")
        self.assertContains(page, "Could not verify signed PDFs")
        self.assertEqual(page.context["queue"].paginator.count, 1)

    def test_completed_signing_links_to_file_and_respects_record_visibility(self):
        signing = self.signing(SigningRequest.Status.COMPLETED)
        document = PersonDocument.objects.create(
            organization=self.org, person=self.person, document_type=self.dtype,
            original_name="signed.pdf", file="test-only/signed.pdf", size=1, sha256="a" * 64,
            scan_status=PersonDocument.ScanStatus.CLEAN,
        )
        SignedArtifact.objects.create(request=signing, document=document, key="document:0", name="signed")
        self.sign_task.status = OnboardingTask.Status.DONE
        self.sign_task.save()
        self.login()
        page = self.client.get(reverse("signing_queue") + "?stage=completed")
        self.assertEqual(page.context["queue"].paginator.count, 1)
        self.assertContains(page, reverse("person_detail", args=[self.person.pk]) + "?tab=documents")
        self.dtype.sensitivity = DocumentType.Sensitivity.SEALED
        self.dtype.save()
        self.person.user = self.users[Membership.Role.HR]
        self.person.save()
        self.login(Membership.Role.HR)
        page = self.client.get(reverse("signing_queue"))
        self.assertEqual(page.context["queue"].paginator.count, 0)

    def test_no_signing_configuration_is_attention_not_ready(self):
        self.config.enabled = False
        self.config.save()
        self.login()
        page = self.client.get(reverse("signing_queue") + "?stage=failed")
        self.assertEqual(page.context["queue"].paginator.count, 1)
        self.assertContains(page, "Configure signing")
        ready = self.client.get(reverse("signing_queue") + "?stage=ready")
        self.assertEqual(ready.context["queue"].paginator.count, 0)

    def test_schedule_filters_published_open_separately_from_drafts_and_risk(self):
        open_post = self.post()
        draft = self.post(status=Shift.Status.DRAFT)
        staffed = self.post(officer=self.person)
        self.login()
        page = self.client.get(reverse("schedule") + "?show=open")
        self.assertEqual([post.pk for post in page.context["shifts"]], [open_post.pk])
        page = self.client.get(reverse("schedule") + "?show=draft")
        self.assertEqual([post.pk for post in page.context["shifts"]], [draft.pk])
        page = self.client.get(reverse("schedule") + "?show=risk")
        self.assertNotIn(staffed, page.context["shifts"])
        self.assertNotIn(open_post, page.context["shifts"])

    def test_selected_requests_are_exact_scoped_and_offer_queue_return(self):
        visible = self.post(officer=self.person)
        hidden = self.post(site=self.hidden_site)
        swap = ShiftSwap.objects.create(organization=self.org, shift=visible, requester=self.person,
                                       replacement=self.hidden_person)
        other = ShiftSwap.objects.create(organization=self.org, shift=hidden, requester=self.hidden_person,
                                        replacement=self.person)
        leave = TimeOffRequest.objects.create(organization=self.org, person=self.person,
                                              starts_at=visible.starts_at, ends_at=visible.ends_at)
        hidden_leave = TimeOffRequest.objects.create(organization=self.org, person=self.hidden_person,
                                                     starts_at=visible.starts_at, ends_at=visible.ends_at)
        self.login(Membership.Role.SCHEDULER)
        page = self.client.get(reverse("swaps") + f"?swap={swap.pk}")
        self.assertEqual([row.pk for row in page.context["pending"]], [swap.pk])
        self.assertContains(page, "Back to scheduling action queue")
        self.assertEqual(self.client.get(reverse("swaps") + f"?swap={other.pk}").status_code, 404)
        page = self.client.get(reverse("time_off") + f"?request={leave.pk}")
        self.assertEqual([row["request"].pk for row in page.context["rows"]], [leave.pk])
        self.assertEqual(self.client.get(reverse("time_off") + f"?request={hidden_leave.pk}").status_code, 404)
        self.assertEqual(self.client.get(reverse("time_off") + "?request=bad").status_code, 404)
        self.assertEqual(self.client.get(reverse("time_off") + f"?request={uuid.uuid4()}").status_code, 404)

    def test_setup_readiness_is_explicit_and_authorized(self):
        self.login()
        page = self.client.get(reverse("settings"))
        self.assertContains(page, "Configuration checks, not operational or legal certification")
        checks = {row["label"]: row for row in page.context["setup_checks"]}
        self.assertFalse(checks["Site coordinates"]["configured"])
        self.assertTrue(checks["Document signing"]["configured"])
        self.login(Membership.Role.SCHEDULER)
        page = self.client.get(reverse("workspace_today"))
        self.assertNotContains(page, reverse("signing_settings"))
        self.assertNotContains(page, reverse("messaging_settings"))

    def test_retention_pending_decisions_precede_history(self):
        self.login()
        page = self.client.get(reverse("retention_review"))
        text = page.content.decode()
        self.assertLess(text.index("Disposition requests"), text.index("Archived records"))
        self.assertLess(text.index("Due for review"), text.index("Deleted records"))

    def test_onboarding_issuance_and_completion_preserve_checklist_context(self):
        self.person.status = Person.Status.ONBOARDING
        self.person.save()
        self.login()
        result = self.client.post(reverse("onboarding_issue"))
        self.assertRedirects(result, reverse("onboarding_settings"))
        result = self.client.post(reverse("person_onboarding_issue", args=[self.person.pk]))
        self.assertRedirects(result, reverse("person_detail", args=[self.person.pk]) + "?tab=onboarding")
        result = self.client.post(reverse("onboarding_task_decide", args=[self.task.pk]), {"action": "complete"})
        self.assertRedirects(result, reverse("person_detail", args=[self.person.pk]) + "?tab=onboarding")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, OnboardingTask.Status.DONE)

    def test_compliance_missing_requirement_has_authorized_resolution_link(self):
        CredentialType.objects.create(organization=self.org, name="Required card", code="required",
                                      applies_to=["unarmed"], blocks_scheduling=True)
        self.person.is_unarmed_officer = True
        self.person.save()
        self.login(Membership.Role.SCHEDULER)
        page = self.client.get(reverse("compliance"))
        self.assertContains(page, reverse("person_detail", args=[self.person.pk]) + "?tab=credentials")
        self.assertContains(page, "Review assignment eligibility")
        self.assertNotContains(page, reverse("document_upload"))

    def test_onboarding_queue_pagination_keeps_totals_and_filters(self):
        for index in range(30):
            item = OnboardingItem.objects.create(organization=self.org, name=f"Work {index}", code=f"work-{index}",
                                                owner=OnboardingItem.Owner.STAFF)
            OnboardingTask.objects.create(organization=self.org, person=self.person, item=item)
        self.login()
        first = self.client.get(reverse("workspace_people") + "?work=office")
        second = self.client.get(reverse("workspace_people") + "?work=office&page=2")
        self.assertEqual(len(first.context["onboarding_queue"]), 25)
        self.assertGreater(len(second.context["onboarding_queue"]), 0)
        self.assertEqual(first.context["onboarding_actions"]["total"], second.context["onboarding_actions"]["total"])
        self.assertContains(first, "work=office&amp;page=2")

    def test_foreign_signing_task_selection_is_not_disclosed(self):
        foreign = Organization.objects.create(legal_name="Elsewhere", slug="elsewhere-workflow")
        person = Person.objects.create(organization=foreign, first_name="Foreign", last_name="Signer")
        dtype = DocumentType.objects.create(organization=foreign, name="Private agreement")
        item = OnboardingItem.objects.create(organization=foreign, name="Private step", code="private",
                                            kind=OnboardingItem.Kind.SIGNATURE, document_type=dtype)
        task = OnboardingTask.objects.create(organization=foreign, person=person, item=item)
        self.login()
        self.assertEqual(self.client.get(reverse("signing_queue") + f"?task={task.pk}").status_code, 404)
        self.assertEqual(self.client.get(reverse("signing_queue") + "?stage=unsupported").status_code, 404)
