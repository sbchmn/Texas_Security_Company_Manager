from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .attendance import follow_up, reconcile_attendance, reconcile_shift
from .forms import TimePolicyOverrideForm
from .models import (
    AttendanceCase, AttendanceCaseAction, AuditEvent, AuthorityScope, Branch, Client,
    EmailTemplate, HoldOver, Membership, Notification, Organization, Person, Punch, Shift,
    PunchAdjustment, Site, TimePolicy, TimePolicyOverride,
)
from .notification_actions import notification_action
from .services import RULE_WATCHED, effective_clock_policy, record_hold_over, record_punch
from .models import RuleRevision


@override_settings(PUBLIC_BASE_URL="https://attendance.example.com")
class LiveAttendanceTest(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(second=0, microsecond=0)
        self.org = Organization.objects.create(legal_name="Attendance Security", slug="attendance-security")
        self.owner = get_user_model().objects.create_user(username="attendance-owner")
        self.owner_member = Membership.objects.create(organization=self.org, user=self.owner, role=Membership.Role.OWNER)
        self.officer = get_user_model().objects.create_user(username="attendance-officer")
        Membership.objects.create(organization=self.org, user=self.officer, role=Membership.Role.OFFICER)
        self.branch = Branch.objects.create(organization=self.org, name="Local")
        self.person = Person.objects.create(organization=self.org, user=self.officer, branch=self.branch,
                                            first_name="Jordan", last_name="Guard")
        self.customer = Client.objects.create(organization=self.org, name="Customer")
        self.site = Site.objects.create(organization=self.org, client=self.customer, branch=self.branch, name="North gate")
        self.shift = Shift.objects.create(organization=self.org, site=self.site, officer=self.person,
            status=Shift.Status.PUBLISHED, starts_at=self.now - timedelta(minutes=10),
            ends_at=self.now + timedelta(hours=8))
        self.policy = TimePolicy.objects.create(organization=self.org, require_geofence=False,
            arrival_alert_enabled=True, departure_alert_enabled=True)

    def punch(self, kind=Punch.Kind.IN, *, at=None, status=Punch.Review.ACCEPTED, person=None):
        return Punch.objects.create(organization=self.org, shift=self.shift, person=person or self.person,
            client_event_id=uuid.uuid4(), kind=kind, occurred_at=at or self.shift.starts_at, review_status=status)

    def evaluate(self, now=None):
        return reconcile_attendance(now or self.now, organizations=[self.org])

    def case(self):
        self.evaluate()
        return AttendanceCase.objects.get(shift=self.shift)

    def departure(self):
        self.shift.starts_at = self.now - timedelta(hours=8)
        self.shift.ends_at = self.now - timedelta(minutes=10)
        self.shift.save()
        self.punch()

    def supervisor(self, *, covered=True):
        user = get_user_model().objects.create_user(username=f"supervisor-{covered}")
        member = Membership.objects.create(organization=self.org, user=user, role=Membership.Role.SUPERVISOR)
        if covered:
            AuthorityScope.objects.create(organization=self.org, membership=member, site=self.site)
        else:
            other_site = Site.objects.create(organization=self.org, client=self.customer, name="Unrelated site")
            AuthorityScope.objects.create(organization=self.org, membership=member, site=other_site)
        return user, member

    def test_disabled_defaults_and_zero_grace(self):
        self.policy.arrival_alert_enabled = self.policy.departure_alert_enabled = False
        self.policy.save()
        self.assertEqual(self.evaluate()["opened"], 0)
        self.policy.arrival_alert_enabled = True
        self.policy.arrival_grace_minutes = 0
        self.policy.save()
        self.assertEqual(self.evaluate(self.shift.starts_at)["opened"], 0)
        self.assertEqual(self.evaluate(self.shift.starts_at + timedelta(microseconds=1))["opened"], 1)

    def test_exact_arrival_boundary_and_snapshot(self):
        boundary = self.shift.starts_at + timedelta(minutes=5)
        self.assertEqual(self.evaluate(boundary)["opened"], 0)
        self.assertEqual(self.evaluate(boundary + timedelta(microseconds=1))["opened"], 1)
        case = AttendanceCase.objects.get()
        self.assertEqual(case.deadline_at, boundary)
        self.assertEqual(case.policy_snapshot["grace_minutes"], 5)
        self.assertEqual(case.policy_snapshot["grace_source"], "company")
        self.assertEqual(case.actions.get().metadata["policy"], case.policy_snapshot)

    def test_per_field_client_site_inheritance_preserves_false_and_zero(self):
        TimePolicyOverride.objects.create(organization=self.org, client=self.customer,
            arrival_grace_minutes=12, departure_alert_enabled=False)
        site_rule = TimePolicyOverride.objects.create(organization=self.org, site=self.site,
            arrival_grace_minutes=0, departure_alert_enabled=True)
        resolved = effective_clock_policy(self.org, self.site)
        self.assertEqual(resolved.attendance_alert("arrival")["grace_minutes"], 0)
        self.assertEqual(resolved.attendance_alert("arrival")["grace_source"], "site")
        self.assertEqual(resolved.attendance_alert("departure")["enabled_source"], "site")
        site_rule.departure_alert_enabled = None
        site_rule.save()
        self.assertFalse(effective_clock_policy(self.org, self.site).attendance_alert("departure")["enabled"])

    def test_override_form_and_bounds(self):
        form = TimePolicyOverrideForm({"client": str(self.customer.pk), "arrival_alert_enabled": "no", "arrival_grace_minutes": "0"},
                                     instance=TimePolicyOverride(organization=self.org))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIs(form.cleaned_data["arrival_alert_enabled"], False)
        self.assertEqual(form.cleaned_data["arrival_grace_minutes"], 0)
        self.assertIsNone(form.cleaned_data["departure_alert_enabled"])
        form = TimePolicyOverrideForm({"client": str(self.customer.pk), "arrival_grace_minutes": "241", "departure_grace_minutes": "-1"},
                                     instance=TimePolicyOverride(organization=self.org))
        self.assertFalse(form.is_valid())
        self.assertIn("arrival_grace_minutes", form.errors)
        self.assertIn("departure_grace_minutes", form.errors)
        for kind in (RuleRevision.Kind.CLOCK_POLICY, RuleRevision.Kind.CLOCK_RULE):
            self.assertIn("arrival_alert_enabled", RULE_WATCHED[kind])
            self.assertIn("departure_grace_minutes", RULE_WATCHED[kind])

    def test_worker_dedup_and_recipient_scope(self):
        visible, _ = self.supervisor()
        hidden, _ = self.supervisor(covered=False)
        self.assertEqual(self.evaluate()["opened"], 1)
        notices = Notification.objects.filter(event_type="punch.late_arrival")
        recipients = set(notices.values_list("recipient_id", flat=True))
        self.assertEqual(recipients, {self.owner.pk, self.officer.pk, visible.pk})
        self.assertNotIn(hidden.pk, recipients)
        count = notices.count()
        self.assertEqual(self.evaluate()["opened"], 0)
        self.assertEqual(notices.count(), count)
        self.assertEqual(AttendanceCaseAction.objects.count(), 1)

    def test_non_published_unassigned_and_inactive_not_alerted(self):
        for status in (Shift.Status.DRAFT, Shift.Status.CANCELLED):
            self.shift.status = status
            self.shift.save()
            self.assertEqual(self.evaluate()["opened"], 0)
        self.shift.status = Shift.Status.PUBLISHED
        self.shift.officer = None
        self.shift.save()
        self.assertEqual(self.evaluate()["opened"], 0)
        self.shift.officer = self.person
        self.shift.save()
        self.person.status = Person.Status.INACTIVE
        self.person.save()
        self.assertEqual(self.evaluate()["opened"], 0)

    def test_arrival_does_not_create_retroactive_case_after_shift_end(self):
        self.shift.ends_at = self.now
        self.shift.save()
        self.assertEqual(self.evaluate()["opened"], 0)

    def test_pending_evidence_resolves_but_rejected_does_not(self):
        case = self.case()
        rejected = self.punch(status=Punch.Review.REJECTED)
        self.assertEqual(self.evaluate()["resolved"], 0)
        rejected.review_status = Punch.Review.PENDING
        rejected.save()
        self.assertEqual(self.evaluate()["resolved"], 1)
        case.refresh_from_db()
        self.assertEqual(case.resolution, "Clock-in recorded")
        self.assertEqual(rejected.review_status, Punch.Review.PENDING)

    def test_future_and_wrong_officer_punches_do_not_resolve(self):
        self.punch(at=self.now + timedelta(minutes=1))
        other = Person.objects.create(organization=self.org, first_name="Other", last_name="Guard")
        self.punch(person=other)
        self.assertEqual(self.evaluate()["opened"], 1)

    def test_record_punch_immediately_resolves_offline_arrival_without_mutating_it(self):
        case = self.case()
        punch, created = record_punch(organization=self.org, person=self.person, shift=self.shift,
            client_event_id=uuid.uuid4(), kind=Punch.Kind.IN, occurred_at=self.shift.starts_at,
            actor=self.officer, offline=True)
        self.assertTrue(created)
        case.refresh_from_db()
        self.assertEqual(case.status, "resolved")
        self.assertEqual(punch.occurred_at, self.shift.starts_at)
        self.assertTrue(punch.offline)

    def test_checkpoint_not_a_departure_and_exact_departure_boundary(self):
        self.departure()
        self.punch(Punch.Kind.CHECKPOINT, at=self.now)
        boundary = self.shift.ends_at + timedelta(minutes=5)
        self.assertEqual(self.evaluate(boundary)["opened"], 0)
        self.assertEqual(self.evaluate(boundary + timedelta(microseconds=1))["opened"], 1)
        self.assertEqual(AttendanceCase.objects.get().kind, AttendanceCase.Kind.DEPARTURE)

    def test_departure_requires_arrival_and_out_must_follow_last_in(self):
        self.departure()
        self.punch(Punch.Kind.OUT, at=self.shift.starts_at - timedelta(minutes=1))
        self.assertEqual(self.evaluate()["opened"], 1)
        self.punch(Punch.Kind.OUT, at=self.now)
        self.assertEqual(self.evaluate()["resolved"], 1)
        self.punch(Punch.Kind.IN, at=self.now + timedelta(minutes=1))
        self.assertEqual(self.evaluate(self.now + timedelta(minutes=2))["opened"], 1)
        self.assertEqual(AttendanceCase.objects.get().cycle, 2)

    def test_no_departure_case_for_entirely_missing_punches(self):
        self.shift.ends_at = self.now - timedelta(minutes=10)
        self.shift.save()
        self.assertEqual(self.evaluate()["opened"], 0)

    def test_cancel_reassign_disable_and_extended_schedule_resolve_with_history(self):
        case = self.case()
        self.shift.status = Shift.Status.CANCELLED
        self.shift.save()
        self.assertEqual(self.evaluate()["resolved"], 1)
        case.refresh_from_db()
        self.assertIn("no longer active", case.resolution)
        self.shift.status = Shift.Status.PUBLISHED
        self.shift.save()
        self.evaluate()
        self.policy.arrival_alert_enabled = False
        self.policy.save()
        self.evaluate()
        case.refresh_from_db()
        self.assertIn("disabled", case.resolution)
        self.policy.arrival_alert_enabled = True
        self.policy.save()
        self.evaluate()
        self.shift.starts_at = self.now + timedelta(hours=1)
        self.shift.save()
        self.evaluate()
        case.refresh_from_db()
        self.assertIn("deadline", case.resolution)
        self.shift.starts_at = self.now - timedelta(minutes=10)
        self.shift.save()
        self.evaluate()
        replacement = Person.objects.create(organization=self.org, first_name="Alex", last_name="Relief")
        self.shift.officer = replacement
        self.shift.save()
        self.evaluate()
        case.refresh_from_db()
        self.assertIn("previous officer", case.resolution)
        self.assertTrue(AttendanceCase.objects.filter(person=replacement, status="open").exists())
        self.assertGreater(case.actions.count(), 5)

    def test_finite_holdover_moves_deadline_and_realerts_without_changing_pay(self):
        self.departure()
        case = self.case()
        original_punches = list(Punch.objects.values())
        release = self.now + timedelta(minutes=30)
        follow_up(case, self.owner, "hold_over", "Relief delayed; supervisor approves coverage.",
                  expected_release_at=release, reason=HoldOver.Reason.RELIEF_LATE)
        case.refresh_from_db()
        self.assertEqual(case.status, "resolved")
        hold = HoldOver.objects.get()
        self.assertEqual(hold.expected_release_at, release)
        self.assertIsNone(hold.held_until)
        self.assertEqual(list(Punch.objects.values()), original_punches)
        self.assertEqual(self.evaluate(release + timedelta(minutes=5))["opened"], 0)
        self.assertEqual(self.evaluate(release + timedelta(minutes=5, seconds=1))["opened"], 1)
        case.refresh_from_db()
        self.assertEqual(case.cycle, 2)
        self.assertEqual(case.deadline_at, release + timedelta(minutes=5))
        self.assertEqual(case.actions.filter(kind="opened").count(), 2)

    def test_existing_open_ended_holdover_is_not_an_unapproved_late_dismissal(self):
        self.departure()
        record_hold_over(self.shift, HoldOver.Reason.UNFILLED, actor=self.owner)
        self.assertEqual(self.evaluate()["opened"], 0)
        hold = HoldOver.objects.get()
        hold.held_until = self.now - timedelta(minutes=7)
        hold.save()
        self.assertEqual(self.evaluate()["opened"], 1)

    def test_invalid_holdover_deadline_does_not_write(self):
        self.departure()
        case = self.case()
        for release in (None, self.now - timedelta(minutes=1)):
            with self.assertRaises(ValidationError):
                follow_up(case, self.owner, "hold_over", "Need approval for delayed relief",
                          expected_release_at=release, reason=HoldOver.Reason.RELIEF_LATE)
        self.assertEqual(HoldOver.objects.count(), 0)

    def test_contacts_relief_and_manager_closure_remain_audited_and_deduped(self):
        case = self.case()
        follow_up(case, self.owner, "contact", "Called officer; phone unanswered.")
        follow_up(case, self.owner, "relief", "Called dispatch; relief is on the way.")
        follow_up(case, self.owner, "closed", "Confirmed arrival by radio; officer will correct time.")
        self.assertEqual(case.actions.count(), 4)
        self.assertEqual(self.evaluate()["opened"], 0)
        self.assertTrue(AuditEvent.objects.filter(action="attendance.closed").exists())
        self.assertEqual(Punch.objects.count(), 0)

    def test_action_permissions_and_reason_required(self):
        case = self.case()
        hidden, _ = self.supervisor(covered=False)
        for user in (self.officer, hidden):
            with self.assertRaises(PermissionDenied):
                follow_up(case, user, "closed", "Attempt to close someone else's case.")
        for kind, note in (("closed", ""), ("bogus", "A sufficiently detailed note.")):
            with self.assertRaises(ValidationError):
                follow_up(case, self.owner, kind, note)
        self.assertEqual(case.actions.count(), 1)

    def test_history_and_queue_scope_including_cross_tenant(self):
        case = self.case()
        self.client.force_login(self.officer)
        response = self.client.get(reverse("attendance_detail", args=[case.pk]))
        self.assertContains(response, "Contact dispatch or your supervisor")
        self.assertNotContains(response, "Record follow-up</h3>")
        self.assertEqual(self.client.post(reverse("attendance_detail", args=[case.pk]), {}).status_code, 403)
        hidden, _ = self.supervisor(covered=False)
        self.client.force_login(hidden)
        self.assertEqual(self.client.get(reverse("attendance_detail", args=[case.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("attendance_queue")), self.person.full_name)
        foreign = Organization.objects.create(legal_name="Elsewhere", slug="elsewhere")
        stranger = get_user_model().objects.create_user(username="foreign-owner")
        Membership.objects.create(organization=foreign, user=stranger, role="owner")
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(reverse("attendance_detail", args=[case.pk])).status_code, 404)

    def test_manager_ui_and_invalid_post_then_success(self):
        case = self.case()
        self.client.force_login(self.owner)
        url = reverse("attendance_detail", args=[case.pk])
        response = self.client.get(url)
        self.assertContains(response, "Record follow-up")
        self.assertContains(response, "Policy at this alert")
        self.assertNotContains(response, "Approve hold-over until")  # arrival cannot authorize departure
        response = self.client.post(url, {"action": "contact", "note": "short"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "at least 10 characters")
        response = self.client.post(url, {"action": "contact", "note": "Reached officer; on post now."})
        self.assertRedirects(response, url)
        self.assertContains(self.client.get(url), "Reached officer")

    def test_today_schedule_and_notification_actions_link_same_scoped_case(self):
        case = self.case()
        self.client.force_login(self.owner)
        today = self.client.get(reverse("workspace_today"))
        priority = next(item for item in today.context["work_priorities"] if item["label"] == "Live attendance")
        self.assertEqual(priority["count"], 1)
        self.assertContains(self.client.get(reverse("workspace_schedule")), "Live attendance (1)")
        for user, member in ((self.owner, self.owner_member),
                             (self.officer, Membership.objects.get(user=self.officer))):
            notification = Notification.objects.filter(recipient=user, event_type="punch.late_arrival").first()
            request = SimpleNamespace(user=user, organization=self.org, membership=member)
            action = notification_action(request, notification)
            self.assertEqual(action["url"], reverse("attendance_detail", args=[case.pk]))

    def test_departure_email_override_and_default_wording_link(self):
        self.departure()
        EmailTemplate.objects.create(organization=self.org, notice_key="punch.overdue_departure",
            subject="Check {officer}", body="Deadline {time}, {minutes} minutes overdue. {body}\n{link}")
        self.evaluate()
        emails = Notification.objects.filter(event_type="punch.overdue_departure", channel="email")
        self.assertTrue(emails.exists())
        case = AttendanceCase.objects.get()
        for email in emails:
            self.assertEqual(email.subject, "Check Jordan Guard")
            self.assertIn("5 minutes overdue", email.body)
            self.assertIn("Do not assume they left", email.body)
            self.assertIn(reverse("attendance_detail", args=[case.pk]), email.body)

    def test_worker_command_and_lookback(self):
        self.shift.starts_at -= timedelta(days=8)
        self.shift.save()
        self.assertEqual(self.evaluate()["opened"], 0)
        self.shift.starts_at = self.now - timedelta(minutes=10)
        self.shift.save()
        output = StringIO()
        with patch("core.attendance.timezone.now", return_value=self.now):
            call_command("monitor_attendance", stdout=output)
        self.assertIn("opened=1", output.getvalue())

    def test_ui_controls_have_labels_and_safe_notes(self):
        from .test_form_ui import FormControlParser
        self.departure()
        case = self.case()
        follow_up(case, self.owner, "contact", "<script>alert('x')</script> attempted.")
        self.client.force_login(self.owner)
        response = self.client.get(reverse("attendance_detail", args=[case.pk]))
        self.assertNotContains(response, "<script>alert('x')</script>")
        parser = FormControlParser()
        parser.feed(response.content.decode())
        for attrs, wrapped in parser.controls:
            self.assertTrue(wrapped or attrs.get("id") in parser.labels, attrs)

    def test_approved_time_corrections_count_but_requested_corrections_do_not(self):
        self.departure()
        out = self.punch(Punch.Kind.OUT, at=self.shift.starts_at - timedelta(minutes=1))
        adjustment = PunchAdjustment.objects.create(organization=self.org, punch=out,
            requested_by=self.officer, proposed_at=self.now - timedelta(minutes=1),
            reason="Actual departure confirmed by dispatch")
        case = self.case()
        adjustment.status = PunchAdjustment.Status.APPROVED
        adjustment.save()
        self.assertEqual(self.evaluate()["resolved"], 1)
        case.refresh_from_db()
        self.assertEqual(case.resolution, "Clock-out recorded")
        out.refresh_from_db()
        self.assertEqual(out.occurred_at, self.shift.starts_at - timedelta(minutes=1))

    def test_new_holdover_supersedes_open_ended_authorization(self):
        self.departure()
        first = record_hold_over(self.shift, HoldOver.Reason.UNFILLED, actor=self.owner)
        release = self.now + timedelta(minutes=15)
        second = record_hold_over(self.shift, HoldOver.Reason.RELIEF_LATE, actor=self.owner, expected_release_at=release)
        HoldOver.objects.filter(shift=self.shift).update(created_at=self.now)
        self.assertEqual((first.sequence, second.sequence), (1, 2))
        self.assertEqual(self.evaluate()["opened"], 0)
        self.assertEqual(self.evaluate(release + timedelta(minutes=6))["opened"], 1)

    def test_holdover_authorization_does_not_follow_a_reassigned_officer(self):
        self.departure()
        record_hold_over(self.shift, HoldOver.Reason.RELIEF_LATE, actor=self.owner,
                         expected_release_at=self.now + timedelta(hours=1))
        replacement = Person.objects.create(organization=self.org, first_name="Alex", last_name="Replacement")
        self.shift.officer = replacement
        self.shift.save()
        self.punch(person=replacement)
        self.assertEqual(self.evaluate()["opened"], 1)
        self.assertEqual(AttendanceCase.objects.get().person, replacement)

    def test_case_get_has_no_evaluation_side_effect_and_history_paginates(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("attendance_queue")), "No matching attendance cases")
        self.assertEqual(AttendanceCase.objects.count(), 0)
        case = self.case()
        for number in range(28):
            follow_up(case, self.owner, "contact", f"Contact attempt {number}: awaiting officer response.")
        response = self.client.get(reverse("attendance_detail", args=[case.pk]), {"history_page": 2})
        self.assertEqual(response.context["history"].number, 2)
        self.assertEqual(response.context["history"].paginator.count, 29)
        self.assertEqual(len(response.context["history"]), 4)
        self.assertContains(response, "Previous history")

    def test_overdue_departure_holdover_post_uses_company_timezone(self):
        from zoneinfo import ZoneInfo
        self.departure()
        case = self.case()
        self.org.timezone = "America/Los_Angeles"
        self.org.save()
        self.client.force_login(self.owner)
        release = (timezone.now() + timedelta(hours=2)).replace(second=0, microsecond=0)
        local = release.astimezone(ZoneInfo(self.org.timezone)).strftime("%Y-%m-%dT%H:%M")
        url = reverse("attendance_detail", args=[case.pk])
        response = self.client.post(url, {"action": "hold_over", "note": "Officer authorized to stay until relief arrives.",
            "reason": HoldOver.Reason.RELIEF_LATE, "expected_release_at": local})
        self.assertRedirects(response, url)
        self.assertEqual(HoldOver.objects.get().expected_release_at, release)

    def test_editor_preserves_explicit_disable_and_zero_override(self):
        self.client.force_login(self.owner)
        override = TimePolicyOverride.objects.create(organization=self.org, site=self.site,
            arrival_alert_enabled=False, arrival_grace_minutes=0)
        response = self.client.get(reverse("time_policy_override", args=[override.pk]))
        self.assertContains(response, 'value="no" selected')
        self.assertContains(response, 'value="0"')
        self.assertContains(self.client.get(reverse("time_policy")), "Live attendance policies")

    def test_punch_resolution_does_not_upgrade_the_parent_shift_lock(self):
        case = self.case()
        self.punch()
        with patch.object(Shift.objects, "select_for_update", side_effect=AssertionError("Unexpected parent lock upgrade")):
            self.assertEqual(reconcile_shift(self.shift.pk, now=self.now, create=False)["resolved"], 1)
        case.refresh_from_db()
        self.assertEqual(case.status, "resolved")
