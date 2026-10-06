from datetime import timedelta
from types import SimpleNamespace
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    AuthorityScope, Branch, Client, DocumentType, Membership, Notification,
    OnboardingItem, OnboardingTask, Organization, Person, PersonDocument, Shift,
    ShiftExchange, ShiftSwap, SigningRequest, Site,
)
from .notification_actions import notification_action
from .services import queue_notice


class EmployeeWorkflowTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(legal_name="Briefs", slug="briefs")
        self.other_org = Organization.objects.create(legal_name="Other", slug="briefs-other")
        self.user = get_user_model().objects.create_user(username="brief-worker")
        self.peer_user = get_user_model().objects.create_user(username="brief-peer")
        self.manager_user = get_user_model().objects.create_user(username="brief-manager")
        self.membership = Membership.objects.create(organization=self.org, user=self.user, role=Membership.Role.OFFICER)
        Membership.objects.create(organization=self.org, user=self.peer_user, role=Membership.Role.OFFICER)
        self.manager_membership = Membership.objects.create(
            organization=self.org, user=self.manager_user, role=Membership.Role.SCHEDULER)
        self.branch = Branch.objects.create(organization=self.org, name="Covered")
        self.other_branch = Branch.objects.create(organization=self.org, name="Outside")
        self.person = Person.objects.create(organization=self.org, user=self.user, first_name="Our", last_name="Worker")
        self.peer = Person.objects.create(organization=self.org, user=self.peer_user, first_name="Their", last_name="Worker")
        self.customer = Client.objects.create(organization=self.org, name="Brief client",
            contact_name="Authorized site contact", contact_email="site@example.com")
        self.site = Site.objects.create(organization=self.org, client=self.customer, branch=self.branch,
            name="Brief site", address="123 Post Road")
        self.outside = Site.objects.create(organization=self.org, client=self.customer, branch=self.other_branch,
            name="Outside site", address="999 Private Road")
        self.shift = self.make_shift(self.person, self.site, "Actual multiline orders\nCheck gate <script>unsafe</script>")
        self.peer_shift = self.make_shift(self.peer, self.site, "Peer-only orders")
        self.swap = ShiftSwap.objects.create(organization=self.org, shift=self.peer_shift,
            requester=self.peer, replacement=self.person)
        self.exchange = ShiftExchange.objects.create(organization=self.org, initiator=self.peer,
            partner=self.person, initiator_shift=self.peer_shift)
        self.client.force_login(self.user)

    def make_shift(self, officer, site, orders=""):
        start = timezone.now() + timedelta(days=2)
        return Shift.objects.create(organization=site.organization, site=site, officer=officer,
            starts_at=start, ends_at=start + timedelta(hours=8),
            status=Shift.Status.PUBLISHED, post_orders=orders)

    def request_for(self, membership=None):
        membership = membership or self.membership
        return SimpleNamespace(organization=self.org, user=membership.user, membership=membership)

    def notice(self, event="shift.swap_offered", key=None, recipient=None, organization=None):
        return Notification.objects.create(organization=organization or self.org,
            recipient=recipient or self.user, event_type=event, subject="Workflow notice", body="Task remains open",
            deduplication_key=key or f"shift-swap:{self.swap.pk}:in_app")

    def test_incoming_answers_precede_roster_and_actual_brief_is_readable(self):
        response = self.client.get(reverse("my_shifts"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertLess(html.index("Your response required"), html.index("Coming up"))
        self.assertContains(response, f'id="swap-{self.swap.pk}"')
        self.assertContains(response, f'id="exchange-{self.exchange.pk}"')
        self.assertContains(response, '<details class="shift-card-brief">')
        self.assertContains(response, "Actual multiline orders")
        self.assertContains(response, "Check gate &lt;script&gt;unsafe&lt;/script&gt;")
        self.assertContains(response, "123 Post Road")
        self.assertContains(response, "Authorized site contact")
        self.assertContains(response, "site@example.com")
        self.assertNotContains(response, "Peer-only orders")
        self.assertNotContains(response, "999 Private Road")

    def test_other_employee_cannot_open_assignment_or_move(self):
        self.client.force_login(self.peer_user)
        response = self.client.get(reverse("my_shifts"))
        self.assertNotContains(response, "Actual multiline orders")
        self.assertEqual(self.client.get(reverse("offer_post", args=[self.shift.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("swap_respond", args=[self.swap.pk]),
                                         {"action": "agreed"}).status_code, 404)

    def test_received_consent_and_completed_outcomes_remain_visible(self):
        self.swap.status = ShiftSwap.Status.AGREED
        self.swap.save(update_fields=["status"])
        self.exchange.status = ShiftExchange.Status.APPROVED
        self.exchange.save(update_fields=["status"])
        response = self.client.get(reverse("my_shifts"))
        self.assertContains(response, "Waiting for a manager")
        self.assertContains(response, "Approved")
        self.assertContains(response, f'id="swap-{self.swap.pk}"')
        self.assertContains(response, f'id="exchange-{self.exchange.pk}"')
        self.assertNotContains(response, "Accept hand-off — send for review")

    def test_producer_key_resolves_exact_employee_action_and_mark_read_is_only_read(self):
        queue_notice(organization=self.org, recipients={self.user.pk},
            event_type="shift.swap_offered", subject="A hand-off", body="Your answer",
            dedup_key=f"shift-swap:{self.swap.pk}", channels=(Notification.Channel.IN_APP,))
        notice = Notification.objects.get(event_type="shift.swap_offered")
        action = notification_action(self.request_for(), notice)
        self.assertIsNotNone(action)
        self.assertEqual(action["url"], reverse("my_shifts") + f"#swap-{self.swap.pk}")
        response = self.client.get(reverse("notifications"))
        self.assertContains(response, action["url"])
        self.assertContains(response, "does not accept, approve, or resolve")
        self.client.post(reverse("notification_read", args=[notice.pk]))
        notice.refresh_from_db()
        self.swap.refresh_from_db()
        self.assertEqual(notice.status, Notification.Status.READ)
        self.assertEqual(self.swap.status, ShiftSwap.Status.OFFERED)

    def test_recipient_tenant_unknown_and_deleted_targets_fail_closed(self):
        notice = self.notice()
        self.assertIsNone(notification_action(self.request_for(self.manager_membership), notice))
        foreign = self.notice(organization=self.other_org)
        self.assertIsNone(notification_action(self.request_for(), foreign))
        unknown = self.notice(event="arbitrary", key=f"https://example.com:{self.shift.pk}")
        self.assertIsNone(notification_action(self.request_for(), unknown))
        malformed = self.notice(key="shift-swap:https://example.com:in_app")
        self.assertIsNone(notification_action(self.request_for(), malformed))
        missing = self.notice(key=f"shift-swap:{uuid.uuid4()}:in_app")
        self.assertIsNone(notification_action(self.request_for(), missing))
        self.swap.delete()
        self.assertIsNone(notification_action(self.request_for(), notice))

    def test_manager_scopes_and_both_trade_halves_are_rechecked(self):
        AuthorityScope.objects.create(organization=self.org, membership=self.manager_membership, site=self.site)
        notice = self.notice(recipient=self.manager_user)
        action = notification_action(self.request_for(self.manager_membership), notice)
        self.assertEqual(action["url"], reverse("swaps") + f"?swap={self.swap.pk}")
        self.peer_shift.site = self.outside
        self.peer_shift.save(update_fields=["site"])
        self.assertIsNone(notification_action(self.request_for(self.manager_membership), notice))
        self.peer_shift.site = self.site
        self.peer_shift.save(update_fields=["site"])
        outside_person = Person.objects.create(organization=self.org, first_name="Outside", last_name="Officer")
        self.exchange.partner_shift = self.make_shift(outside_person, self.outside)
        self.exchange.save(update_fields=["partner_shift"])
        trade = self.notice(event="shift.exchange_agreed", key=f"shift-exchange-agreed:{self.exchange.pk}:in_app",
                            recipient=self.manager_user)
        self.assertIsNone(notification_action(self.request_for(self.manager_membership), trade))

    def test_notifications_listing_read_and_foreign_target_are_scoped(self):
        own = self.notice()
        others = self.notice(recipient=self.peer_user)
        foreign = self.notice(organization=self.other_org)
        response = self.client.get(reverse("notifications"))
        self.assertEqual({item.pk for item in response.context["notifications"]}, {own.pk})
        for hidden in (others, foreign):
            self.assertEqual(self.client.post(reverse("notification_read", args=[hidden.pk])).status_code, 404)
        foreign_client = Client.objects.create(organization=self.other_org, name="Foreign")
        foreign_site = Site.objects.create(organization=self.other_org, client=foreign_client, name="Foreign", address="Foreign")
        foreign_shift = self.make_shift(None, foreign_site, "Foreign orders")
        forged = self.notice(event="shift.published", key=f"shift-published:{foreign_shift.pk}:in_app")
        self.assertIsNone(notification_action(self.request_for(), forged))

    def test_private_document_never_becomes_a_notification_link(self):
        kind = DocumentType.objects.create(organization=self.org, name="Sealed investigation",
            sensitivity=DocumentType.Sensitivity.SEALED, audience=DocumentType.Audience.PERSON)
        document = PersonDocument.objects.create(organization=self.org, person=self.person,
            document_type=kind, file="not-written.pdf", original_name="private.pdf",
            content_type="application/pdf", size=1, sha256="a" * 64, scan_status=PersonDocument.ScanStatus.CLEAN)
        notice = self.notice(event="document.acknowledgment_requested",
                             key=f"document-remind:{document.pk}:{self.person.pk}:20261005")
        self.assertIsNone(notification_action(self.request_for(), notice))

    def test_no_person_is_not_owner_of_an_unassigned_post(self):
        role = Membership.objects.create(organization=self.org,
            user=get_user_model().objects.create_user(username="unlinked"), role=Membership.Role.OFFICER)
        shift = self.make_shift(None, self.site)
        notice = self.notice(event="shift.published", key=f"shift-published:{shift.pk}:in_app", recipient=role.user)
        self.assertIsNone(notification_action(self.request_for(role), notice))

    def test_employee_cannot_link_to_another_person_even_when_notice_is_addressed_to_self(self):
        notice = self.notice(event="onboarding.assigned", key=f"onboarding-plan:{self.peer.pk}:in_app")
        self.assertIsNone(notification_action(self.request_for(), notice))
        own = self.notice(event="onboarding.assigned", key=f"onboarding-plan:{self.person.pk}:in_app")
        action = notification_action(self.request_for(), own)
        self.assertEqual(action["url"], reverse("person_detail", args=[self.person.pk]) + "?tab=onboarding")
        manager_notice = self.notice(event="onboarding.assigned",
            key=f"onboarding-plan:{self.peer.pk}:in_app", recipient=self.manager_user)
        AuthorityScope.objects.create(organization=self.org, membership=self.manager_membership, site=self.outside)
        self.assertIsNone(notification_action(self.request_for(self.manager_membership), manager_notice))

    def test_email_read_does_not_change_delivery_and_inactive_membership_has_no_action(self):
        notice = self.notice()
        notice.channel = Notification.Channel.EMAIL
        notice.status = Notification.Status.SENT
        notice.save(update_fields=["channel", "status"])
        self.assertEqual(self.client.get(reverse("notification_read", args=[notice.pk])).status_code, 405)
        self.client.post(reverse("notification_read", args=[notice.pk]))
        notice.refresh_from_db()
        self.assertEqual(notice.status, Notification.Status.SENT)
        self.membership.active = False
        self.assertIsNone(notification_action(self.request_for(), notice))

    def test_signing_uses_gated_checklist_or_staff_queue_not_provider_url(self):
        kind = DocumentType.objects.create(organization=self.org, name="Agreement")
        item = OnboardingItem.objects.create(organization=self.org, code="agreement",
            name="Sign agreement", kind=OnboardingItem.Kind.SIGNATURE, document_type=kind)
        task = OnboardingTask.objects.create(organization=self.org, person=self.person, item=item)
        signing = SigningRequest.objects.create(organization=self.org, task=task, attempt=1,
            document_type=kind, template_id=1, signer_email="worker@example.com", signer_name="Worker",
            base_url="https://sign.example.com", signing_slug="never-expose", status=SigningRequest.Status.SENT)
        notice = self.notice(event="onboarding.signature_requested", key=f"onboarding-signing:{signing.pk}:in_app")
        action = notification_action(self.request_for(), notice)
        self.assertEqual(action["url"], reverse("person_detail", args=[self.person.pk]) + f"?tab=onboarding#task-{task.pk}")
        self.manager_membership.role = Membership.Role.HR
        self.manager_membership.save(update_fields=["role"])
        notice.recipient = self.manager_user
        action = notification_action(self.request_for(self.manager_membership), notice)
        self.assertEqual(action["url"], reverse("signing_queue") + f"?task={task.pk}")
        self.client.force_login(self.manager_user)
        self.assertEqual(self.client.get(action["url"]).status_code, 200)
        for task_status, person_status in (
            (OnboardingTask.Status.WAIVED, Person.Status.ACTIVE),
            (OnboardingTask.Status.DONE, Person.Status.ACTIVE),
            (OnboardingTask.Status.OPEN, Person.Status.INACTIVE),
        ):
            task.status = task_status
            task.save(update_fields=["status"])
            self.person.status = person_status
            self.person.save(update_fields=["status"])
            action = notification_action(self.request_for(self.manager_membership), notice)
            self.assertEqual(action["url"], reverse("person_detail", args=[self.person.pk])
                             + f"?tab=onboarding#task-{task.pk}")
            self.assertEqual(self.client.get(action["url"]).status_code, 200)
        task_notice = self.notice(event="onboarding.overdue", key=f"onboarding-overdue:{task.pk}:20261005:in_app")
        self.assertTrue(notification_action(self.request_for(), task_notice)["url"].endswith(f"#task-{task.pk}"))

    def test_decided_manager_move_receipts_use_history_not_open_selector(self):
        self.client.force_login(self.manager_user)
        for target, event, prefix in (
            (self.swap, "shift.swap_approved", "shift-swap-approved"),
            (self.exchange, "shift.exchange_approved", "shift-exchange-approved"),
        ):
            target.status = "approved"
            target.save(update_fields=["status"])
            notice = self.notice(event=event, key=f"{prefix}:{target.pk}:in_app", recipient=self.manager_user)
            action = notification_action(self.request_for(self.manager_membership), notice)
            self.assertEqual(action["url"], reverse("swaps"))
            response = self.client.get(action["url"])
            self.assertEqual(response.status_code, 200)
