from datetime import timedelta
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    AuthorityScope, Branch, Membership, Organization, PayrollRun, Person, Punch,
    PunchAdjustment,
)
from .time_workflow import pending_time_review_counts


class TimeWorkflowTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(legal_name="Time queues", slug="time-queues")
        cls.branch = Branch.objects.create(organization=cls.org, name="Covered")
        cls.other_branch = Branch.objects.create(organization=cls.org, name="Other")
        cls.users = {}
        for role in (Membership.Role.OWNER, Membership.Role.PAYROLL, Membership.Role.SUPERVISOR,
                     Membership.Role.OFFICER):
            user = get_user_model().objects.create_user(username=f"time-{role}")
            member = Membership.objects.create(organization=cls.org, user=user, role=role)
            if role == Membership.Role.SUPERVISOR:
                AuthorityScope.objects.create(organization=cls.org, membership=member, branch=cls.branch)
            cls.users[role] = user
        cls.person = Person.objects.create(
            organization=cls.org, branch=cls.branch, user=cls.users[Membership.Role.OFFICER],
            first_name="Visible", last_name="Worker",
        )
        cls.hidden_person = Person.objects.create(
            organization=cls.org, branch=cls.other_branch, first_name="Hidden", last_name="Worker",
        )
        cls.start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        cls.payroll_run = PayrollRun.objects.create(
            organization=cls.org, period_start=cls.start, period_end=cls.start + timedelta(days=7),
            created_by=cls.users[Membership.Role.PAYROLL], snapshot=[{"employee": "Selected worker"}],
        )

    def login(self, role=Membership.Role.PAYROLL):
        self.client.force_login(self.users[role])

    def punch(self, *, person=None, status=Punch.Review.PENDING, at=None):
        return Punch.objects.create(
            organization=self.org, person=person or self.person, client_event_id=uuid.uuid4(),
            kind=Punch.Kind.IN, occurred_at=at or self.start + timedelta(hours=8), review_status=status,
        )

    def correction(self, punch):
        return PunchAdjustment.objects.create(
            organization=self.org, punch=punch, requested_by=self.users[Membership.Role.OFFICER],
            proposed_at=punch.occurred_at + timedelta(minutes=10), reason="Briefing time correction",
        )

    def test_today_workspace_and_review_share_both_queue_counts_for_payroll(self):
        self.punch()
        self.correction(self.punch(status=Punch.Review.ACCEPTED))
        self.login()
        today = self.client.get(reverse("workspace_today"))
        summary = next(row for row in today.context["work_priorities"] if row["label"] == "Time review")
        self.assertEqual(summary["count"], 2)
        self.assertIsNotNone(summary["oldest"])
        workspace = self.client.get(reverse("workspace_payroll"))
        self.assertEqual(workspace.context["pending_count"], summary["count"])
        self.assertTrue(workspace.context["can_approve"])
        review = self.client.get(reverse("time_review"))
        self.assertEqual(review.context["pending_count"], 1)
        self.assertEqual(review.context["pending_corrections_count"], 1)
        self.assertContains(workspace, "1 punch and 1 correction request")
        self.punch()
        self.assertContains(self.client.get(reverse("workspace_payroll")), "2 punches and 1 correction request")

    def test_supervisor_counts_and_history_remain_scoped_without_payroll_access(self):
        self.punch()
        self.correction(self.punch(status=Punch.Review.ACCEPTED))
        self.correction(self.punch(person=self.hidden_person))
        self.login(Membership.Role.SUPERVISOR)
        workspace = self.client.get(reverse("workspace_payroll"))
        self.assertEqual(workspace.context["pending_count"], 2)
        self.assertFalse(workspace.context["can_payroll"])
        self.assertFalse(workspace.context["can_approve"])
        history = self.client.get(reverse("time_review"), {"punches": "all", "adjustments": "all"})
        self.assertNotContains(history, "Hidden Worker")
        self.assertEqual(self.client.get(reverse("time_review"), {"run": self.payroll_run.pk}).status_code, 404)

    def test_corrections_paginate_independently_beyond_previous_hundred_row_limit(self):
        punch = self.punch(status=Punch.Review.ACCEPTED)
        PunchAdjustment.objects.bulk_create([
            PunchAdjustment(
                organization=self.org, punch=punch, requested_by=self.users[Membership.Role.OFFICER],
                proposed_at=punch.occurred_at, reason=f"Correction {number}",
            ) for number in range(105)
        ])
        self.login()
        page = self.client.get(reverse("time_review"), {"adjustment_page": 3})
        self.assertEqual(page.context["adjustments"].paginator.count, 105)
        self.assertEqual(len(page.context["adjustments"]), 5)
        self.assertEqual(page.context["punches"].paginator.count, 0)
        self.assertEqual(page.context["pending_corrections_count"], 105)

    def test_selected_period_limits_both_queues_and_survives_decisions(self):
        punch = self.punch()
        correction = self.correction(self.punch(status=Punch.Review.ACCEPTED))
        self.correction(self.punch(at=self.start - timedelta(days=1)))
        self.login()
        page = self.client.get(reverse("time_review"), {"run": self.payroll_run.pk})
        self.assertEqual(page.context["pending_count"], 1)
        self.assertEqual(page.context["pending_corrections_count"], 1)
        self.assertEqual(page.context["adjustments"].paginator.count, 1)
        self.assertContains(page, f"?run={self.payroll_run.pk}")
        query = f"?punches=all&adjustments=pending&run={self.payroll_run.pk}"
        answer = self.client.post(reverse("punch_review", args=[punch.pk]) + query, {"action": "accepted"})
        self.assertRedirects(answer, reverse("time_review") + query)
        answer = self.client.post(reverse("adjustment_review", args=[correction.pk]) + query,
                                  {"action": "approved", "note": "Verified"})
        self.assertRedirects(answer, reverse("time_review") + query)

    def test_selected_run_uses_its_snapshot_not_latest_and_rejects_foreign_ids(self):
        newer = PayrollRun.objects.create(
            organization=self.org, period_start=self.start + timedelta(days=7),
            period_end=self.start + timedelta(days=14), created_by=self.users[Membership.Role.PAYROLL],
            snapshot=[{"employee": "Newer worker"}],
        )
        self.login()
        page = self.client.get(reverse("payroll"), {"run": self.payroll_run.pk})
        self.assertEqual(page.context["selected_run"].pk, self.payroll_run.pk)
        self.assertNotEqual(page.context["selected_run"].pk, newer.pk)
        self.assertEqual(page.context["form"].initial["period_start"], timezone.localtime(self.payroll_run.period_start))
        foreign = Organization.objects.create(legal_name="Foreign", slug="foreign-time")
        foreign_run = PayrollRun.objects.create(
            organization=foreign, period_start=self.start, period_end=self.start + timedelta(days=7),
            created_by=self.users[Membership.Role.OWNER],
        )
        for run_id in ("invalid", uuid.uuid4(), foreign_run.pk):
            for route in ("payroll", "time_review"):
                self.assertEqual(self.client.get(reverse(route), {"run": run_id}).status_code, 404)

    def test_live_pending_correction_blocks_approval_even_after_snapshot_generation(self):
        self.correction(self.punch(status=Punch.Review.ACCEPTED))
        self.login()
        page = self.client.get(reverse("payroll"), {"run": self.payroll_run.pk})
        self.assertTrue(page.context["approval_blocked"])
        self.assertContains(page, f"adjustments=pending&amp;run={self.payroll_run.pk}")
        result = self.client.post(reverse("payroll_approve", args=[self.payroll_run.pk]))
        self.assertRedirects(result, reverse("payroll") + f"?run={self.payroll_run.pk}")
        self.payroll_run.refresh_from_db()
        self.assertEqual(self.payroll_run.status, PayrollRun.Status.DRAFT)
        self.assertEqual(pending_time_review_counts(self.org, run=self.payroll_run)["corrections"], 1)

    def test_employee_cannot_open_time_or_payroll_management_surfaces(self):
        self.login(Membership.Role.OFFICER)
        for route in ("time_review", "payroll", "workspace_payroll"):
            self.assertEqual(self.client.get(reverse(route)).status_code, 403)

    def test_open_punch_blocker_links_to_all_evidence_not_empty_pending_queue(self):
        self.payroll_run.exceptions = [{"employee": "Visible Worker", "reason": "Open punch"}]
        self.payroll_run.save()
        self.login()
        page = self.client.get(reverse("payroll"), {"run": self.payroll_run.pk})
        self.assertEqual(
            page.context["exception_rows"][0]["destination"],
            reverse("time_review") + f"?punches=all&adjustments=all&run={self.payroll_run.pk}",
        )

    def test_draft_queue_keeps_older_periods_discoverable_beyond_recent_preview(self):
        PayrollRun.objects.bulk_create([
            PayrollRun(
                organization=self.org, period_start=self.start + timedelta(days=7 * number),
                period_end=self.start + timedelta(days=7 * (number + 1)),
                created_by=self.users[Membership.Role.PAYROLL],
            ) for number in range(1, 13)
        ])
        self.login()
        page = self.client.get(reverse("workspace_payroll"), {"draft_page": 2})
        self.assertEqual(page.context["draft_queue"].paginator.count, 13)
        self.assertEqual(len(page.context["draft_queue"]), 3)
        first = self.client.get(reverse("workspace_payroll"))
        self.assertEqual(first.context["draft_queue"][0].pk, self.payroll_run.pk)
        today = self.client.get(reverse("workspace_today"))
        priority = next(row for row in today.context["work_priorities"] if row["label"] == "Payroll drafts")
        self.assertEqual(priority["count"], 13)
        self.assertTrue(priority["url"].endswith("#payroll-drafts"))
