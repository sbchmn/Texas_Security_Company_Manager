import uuid
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse

from .leave import adjust, balance, cancel_approved, enroll, request_days, reserve, suggestion, sync_account
from .models import (
    AuditEvent, AuthorityScope, Branch, Client, LeaveDay, LeaveEntry, LeavePolicy, Membership, Organization,
    PayCategory, PayrollRun, Person, Punch, Shift, Site, TimeOffRequest, TimePolicy,
)
from .services import approve_payroll_run, create_payroll_run, payroll_rows, reopen_payroll_run, set_payroll_lock_segment


class LeaveBankTest(TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 15, 12, tzinfo=dt_timezone.utc)
        self.clock = patch("core.leave.timezone.now", return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.org = Organization.objects.create(legal_name="Leave bank", slug="leave-bank", timezone="UTC")
        self.owner = get_user_model().objects.create_user(username="bank-owner")
        self.worker = get_user_model().objects.create_user(username="bank-worker")
        Membership.objects.create(organization=self.org, user=self.owner, role="owner")
        Membership.objects.create(organization=self.org, user=self.worker, role="officer")
        self.person = Person.objects.create(organization=self.org, user=self.worker,
            first_name="Bank", last_name="Officer", hourly_rate=Decimal("20.00"))
        self.policy = LeavePolicy.objects.create(organization=self.org, enabled=True, annual_hours=100,
                                                grant_method="annual", daily_hours=8)
        self.time_policy = TimePolicy.objects.create(organization=self.org, timezone="UTC",
            pay_period_weeks=1, pay_period_anchor=date(2026, 1, 1), allow_reopen=True)
        self.category = PayCategory.objects.create(organization=self.org, name="Leave", kind="leave", paid=True)
        self.client.force_login(self.owner)

    def account(self, eligible=date(2026, 1, 1)):
        return enroll(self.person, eligible, self.owner)

    def request(self, starts=None, days=2):
        starts = starts or self.now.replace(hour=0)
        return TimeOffRequest.objects.create(organization=self.org, person=self.person,
            starts_at=starts, ends_at=starts + timedelta(days=days), use_leave_bank=True)

    def approve(self, item, hours=Decimal("16.00")):
        response = self.client.post(reverse("time_off_decide", args=[item.pk]),
            {"action": "approved", "confirmed_hours": str(hours), "confirm": "on"})
        item.refresh_from_db()
        self.assertEqual("approved", item.status, getattr(response, "context", None))
        self.assertEqual(302, response.status_code)
        return item

    def generate(self, item, start=None, end=None):
        return create_payroll_run(organization=self.org, start=start or item.starts_at,
                                 end=end or item.ends_at, actor=self.owner)

    def event(self, kind, at, **kwargs):
        return Punch.objects.create(organization=self.org, person=self.person, kind=kind,
                                    occurred_at=at, client_event_id=uuid.uuid4(), **kwargs)

    def shift(self, item, start=8, hours=8, **kwargs):
        customer, _ = Client.objects.get_or_create(organization=self.org, name="Customer")
        site, _ = Site.objects.get_or_create(organization=self.org, client=customer, name="Gate")
        begins = item.starts_at + timedelta(hours=start)
        return Shift.objects.create(organization=self.org, site=site, officer=self.person, status="published",
                                    starts_at=begins, ends_at=begins + timedelta(hours=hours), **kwargs)

    def test_annual_grant_idempotent_and_prorated(self):
        self.now = datetime(2026, 7, 2, 12, tzinfo=dt_timezone.utc)
        with patch("core.leave.timezone.now", return_value=self.now):
            account = self.account(date(2026, 7, 2))
            self.assertEqual(Decimal("50.14"), balance(account)["available"])
            sync_account(account)
            self.assertEqual(1, account.entries.filter(kind="grant").count())

    def test_leap_year_proration(self):
        with patch("core.leave.timezone.now", return_value=datetime(2024, 7, 1, tzinfo=dt_timezone.utc)):
            self.assertEqual(Decimal("50.27"), balance(self.account(date(2024, 7, 1)))["available"])

    def test_accrual_only_completed_periods_and_year_settlement(self):
        self.policy.grant_method = "accrual"
        self.policy.carryover_cap = 100
        self.policy.save()
        account = self.account()
        self.assertEqual(Decimal("3.84"), balance(account)["available"])
        sync_account(account, date(2026, 1, 20))
        self.assertEqual(Decimal("3.84"), balance(account)["available"])
        sync_account(account, date(2027, 1, 1))
        self.assertEqual(Decimal("100.00"), balance(account)["available"])
        count = account.entries.count()
        sync_account(account, date(2027, 1, 1))
        self.assertEqual(count, account.entries.count())

    def test_manual_grants_permissions_reasons_and_nonnegative_balance(self):
        self.policy.grant_method = "manual"
        self.policy.save()
        account = self.account()
        self.assertEqual(0, balance(account)["available"])
        with self.assertRaises(PermissionDenied):
            adjust(account, Decimal("8"), "Manual allowance", self.worker)
        adjust(account, Decimal("20"), "Initial allowance", self.owner)
        adjust(account, Decimal("-4"), "HR correction", self.owner)
        with self.assertRaises(ValidationError):
            adjust(account, Decimal("-17"), "HR correction", self.owner)
        with self.assertRaises(ValidationError):
            adjust(account, Decimal("1"), "bad", self.owner)
        self.assertEqual(16, balance(account)["available"])
        self.assertEqual(2, AuditEvent.objects.filter(action="leave.adjustment").count())

    def test_carryover_cap_preserves_reservations(self):
        self.policy.carryover_cap = 10
        self.policy.save()
        account = self.account()
        item = self.approve(self.request(), Decimal("16"))
        with patch("core.leave.timezone.now", return_value=datetime(2027, 1, 1, tzinfo=dt_timezone.utc)):
            self.assertEqual(Decimal("110.00"), balance(account)["available"])
            self.assertEqual(16, balance(account)["reserved"])
            cancel_approved(item, "Absence withdrawn", self.owner)
            self.assertEqual(126, balance(account)["available"])
        self.assertEqual(1, account.entries.filter(key="expiry:2027").count())

    def test_zero_cap_expires_unused_hours(self):
        account = self.account()
        with patch("core.leave.timezone.now", return_value=datetime(2027, 1, 1, tzinfo=dt_timezone.utc)):
            self.assertEqual(100, balance(account)["available"])
            self.assertEqual(Decimal("-100.00"), account.entries.get(key="expiry:2027").hours)

    def test_enrollment_disabled_duplicate_and_future_eligibility(self):
        self.policy.enabled = False
        self.policy.save()
        with self.assertRaises(ValidationError):
            self.account()
        self.policy.enabled = True
        self.policy.save()
        account = self.account(date(2026, 2, 1))
        self.assertEqual(0, balance(account)["available"])
        with self.assertRaises(ValidationError):
            self.account()
        with self.assertRaises(ValidationError):
            reserve(self.request(), Decimal("8"), self.owner)

    def test_days_touched_midnight_and_dst(self):
        item = self.request()
        self.assertEqual(2, len(list(request_days(item))))
        self.assertEqual(16, suggestion(item))
        item.ends_at += timedelta(hours=1)
        self.assertEqual(24, suggestion(item))
        self.time_policy.timezone = "America/Chicago"
        self.time_policy.save()
        item.starts_at = datetime(2026, 3, 8, 6, tzinfo=dt_timezone.utc)
        item.ends_at = datetime(2026, 3, 9, 5, tzinfo=dt_timezone.utc)
        self.assertEqual(1, len(list(request_days(item))))
        self.assertEqual(8, suggestion(item))

    def test_confirmation_required_preserves_form_and_does_not_reserve(self):
        self.account()
        item = self.request()
        response = self.client.post(reverse("time_off_decide", args=[item.pk]),
            {"action": "approved", "confirmed_hours": "16"})
        self.assertContains(response, "This field is required")
        self.assertEqual("requested", TimeOffRequest.objects.get(pk=item.pk).status)
        self.assertFalse(item.bank_entries.exists())

    def test_insufficient_balance_atomic_and_duplicate_reservation(self):
        account = self.account()
        item = self.request()
        with self.assertRaises(ValidationError):
            reserve(item, Decimal("101"), self.owner)
        self.assertFalse(item.leave_days.exists())
        self.assertEqual(100, balance(account)["available"])
        self.approve(item)
        with self.assertRaises(ValidationError):
            reserve(item, Decimal("16"), self.owner)
        self.assertEqual(84, balance(account)["available"])

    def test_small_amount_rounding_stays_nonnegative_and_exact(self):
        self.account()
        item = self.request(days=5)
        self.approve(item, Decimal("0.03"))
        self.assertEqual(Decimal("0.03"), sum(day.hours for day in item.leave_days.all()))
        self.assertTrue(all(day.hours >= 0 for day in item.leave_days.all()))

    def test_scheduled_hours_replace_fallback_and_follow_shift_days(self):
        self.account()
        item = self.request(days=3)
        self.shift(item, pay_rate=Decimal("25"))
        self.shift(item, start=32, hours=4, pay_rate=Decimal("30"))
        self.assertEqual(12, suggestion(item))
        self.approve(item, Decimal("12"))
        allocations = list(item.leave_days.all())
        self.assertEqual([Decimal("8"), Decimal("4")], [day.hours for day in allocations])
        self.assertEqual([Decimal("25"), Decimal("30")], [day.pay_rate for day in allocations])

    def test_rate_fallback_and_snapshot_immutable(self):
        self.account()
        self.person.hourly_rate = None
        self.person.save()
        item = self.request()
        with self.assertRaises(ValidationError):
            reserve(item, Decimal("16"), self.owner)
        self.policy.fallback_pay_rate = 15
        self.policy.save()
        self.approve(item)
        self.policy.fallback_pay_rate = 99
        self.policy.save()
        self.category.multiplier = 2
        self.category.save()
        run = self.generate(item)
        self.assertEqual([], run.exceptions)
        self.assertEqual(Decimal("240.00"), sum(Decimal(row["estimated_pay"]) for row in run.snapshot))

    def test_missing_paid_category_and_disabled_policy_refuse_approval(self):
        self.account()
        item = self.request()
        self.category.paid = False
        self.category.save()
        with self.assertRaises(ValidationError):
            reserve(item, Decimal("16"), self.owner)
        self.category.paid = True
        self.category.save()
        self.policy.enabled = False
        self.policy.save()
        with self.assertRaises(ValidationError):
            reserve(item, Decimal("16"), self.owner)

    def test_two_days_pay_sixteen_hours_not_fortyeight(self):
        account = self.account()
        item = self.approve(self.request())
        run = self.generate(item)
        self.assertEqual([], run.exceptions)
        self.assertEqual(16, sum(Decimal(row["total_hours"]) for row in run.snapshot))
        self.assertEqual(Decimal("320.00"), sum(Decimal(row["estimated_pay"]) for row in run.snapshot))
        self.assertEqual(16, balance(account)["reserved"])
        approve_payroll_run(run, self.owner)
        self.assertEqual(16, balance(account)["used"])
        self.assertEqual(0, balance(account)["reserved"])

    def test_split_periods_no_duplicate_hours_and_reopen(self):
        self.account()
        item = self.approve(self.request(days=1), Decimal("8"))
        middle = item.starts_at + timedelta(hours=12)
        first = self.generate(item, end=middle)
        approve_payroll_run(first, self.owner)
        second = self.generate(item, start=middle)
        self.assertEqual([], second.exceptions)
        self.assertEqual("4.00", first.snapshot[0]["total_hours"])
        self.assertEqual("4.00", second.snapshot[0]["total_hours"])
        with self.assertRaises(ValidationError):
            cancel_approved(item, "Absence withdrawn", self.owner)
        approve_payroll_run(second, self.owner)
        reopen_payroll_run(first, self.owner, "Reconcile the approved leave")
        self.assertTrue(item.leave_days.get().consumed)
        reopen_payroll_run(second, self.owner, "Reconcile the approved leave")
        self.assertFalse(item.leave_days.get().consumed)
        cancel_approved(item, "Absence withdrawn", self.owner)

    def test_accepted_work_and_unpaid_breaks_not_paid_twice(self):
        self.account()
        item = self.approve(self.request(days=1), Decimal("8"))
        # Clock-in before the absence and clock-out at its exact end still count.
        self.event("in", item.starts_at - timedelta(hours=1))
        self.event("break_start", item.starts_at + timedelta(hours=20), break_paid=False)
        self.event("break_end", item.starts_at + timedelta(hours=21))
        self.event("out", item.ends_at)
        rows = [row for row in payroll_rows(self.org, item.starts_at, item.ends_at) if row["pay_category"] == "leave"]
        self.assertEqual(Decimal("0"), rows[0]["total_hours"])
        self.assertIn("23.00 worked hours", rows[0]["note"])

    def test_partial_work_reduces_bank_pay_only(self):
        self.account()
        item = self.approve(self.request(days=1), Decimal("8"))
        self.event("in", item.starts_at + timedelta(hours=8))
        self.event("out", item.starts_at + timedelta(hours=12))
        rows = [row for row in payroll_rows(self.org, item.starts_at, item.ends_at) if row["pay_category"] == "leave"]
        self.assertEqual(Decimal("4"), rows[0]["total_hours"])
        self.assertEqual(Decimal("80"), rows[0]["estimated_pay"])

    def test_incomplete_work_blocks_bank_pay(self):
        self.account()
        item = self.approve(self.request(days=1), Decimal("8"))
        self.event("in", item.starts_at - timedelta(hours=1))
        self.assertTrue(self.generate(item).exceptions)

    def test_cancellation_restores_once_and_invalidates_draft(self):
        account = self.account()
        item = self.approve(self.request())
        run = self.generate(item)
        cancel_approved(item, "Absence withdrawn", self.owner)
        self.assertEqual(100, balance(account)["available"])
        with self.assertRaises(ValidationError):
            cancel_approved(item, "Absence withdrawn", self.owner)
        self.assertEqual(1, item.bank_entries.filter(kind="release").count())
        run.refresh_from_db()
        self.assertTrue(run.exceptions)

    def test_locked_subrange_refuses_bank_approval(self):
        self.account()
        item = self.request(days=1)
        PayrollRun.objects.create(organization=self.org, created_by=self.owner, status="approved",
            period_start=item.starts_at + timedelta(hours=3), period_end=item.starts_at + timedelta(hours=5))
        with self.assertRaises(ValidationError):
            reserve(item, Decimal("8"), self.owner)

    def test_overlapping_locked_run_blocks_generation(self):
        self.account()
        item = self.approve(self.request())
        first = self.generate(item)
        approve_payroll_run(first, self.owner)
        other = self.generate(item, end=item.ends_at + timedelta(hours=1))
        self.assertTrue(other.exceptions)

    def test_profile_settings_and_employee_surfaces(self):
        self.account()
        self.assertContains(self.client.get(reverse("person_detail", args=[self.person.pk])), "Leave bank")
        self.assertContains(self.client.get(reverse("settings")), reverse("leave_policy"))
        self.assertContains(self.client.get(reverse("leave_policy")), "Annual allowance")
        self.client.force_login(self.worker)
        self.assertContains(self.client.get(reverse("my_time_off")), "100.00 hours available")
        self.assertEqual(403, self.client.get(reverse("leave_policy")).status_code)
        self.assertEqual(403, self.client.post(reverse("employee_leave", args=[self.person.pk]),
                                              {"action": "adjust", "hours": 100, "reason": "Forged grant"}).status_code)

    def test_unenrolled_forged_bank_request_shows_error(self):
        self.client.force_login(self.worker)
        response = self.client.post(reverse("my_time_off"), {"starts_at": "2026-01-20T08:00",
            "ends_at": "2026-01-21T08:00", "use_leave_bank": "on", "reason": "Leave"})
        self.assertContains(response, "HR must enroll you")
        self.assertFalse(self.person.time_off_requests.exists())

    def test_current_year_terms_unchanged_on_company_edit(self):
        account = self.account()
        response = self.client.post(reverse("leave_policy"), {"enabled": "on", "annual_hours": 200,
            "grant_method": "annual", "daily_hours": 6, "carryover_cap": 0, "fallback_pay_rate": ""})
        self.assertEqual(302, response.status_code)
        self.assertEqual(100, balance(account)["available"])
        self.assertEqual(100, account.years.get(year=2026).annual_hours)

    def test_worked_leave_returns_hours_at_lock_and_reverses_at_reopen(self):
        account = self.account()
        item = self.approve(self.request(days=1), Decimal("8"))
        self.event("in", item.starts_at + timedelta(hours=8))
        self.event("out", item.starts_at + timedelta(hours=12))
        run = self.generate(item)
        self.assertEqual(92, balance(account)["available"])
        approve_payroll_run(run, self.owner)
        self.assertEqual(96, balance(account)["available"])
        self.assertEqual(4, balance(account)["used"])
        self.assertEqual(0, balance(account)["reserved"])
        reopen_payroll_run(run, self.owner, "Reconcile worked time with leave")
        self.assertEqual(92, balance(account)["available"])
        self.assertEqual(8, balance(account)["reserved"])
        self.generate(item)
        approve_payroll_run(run, self.owner)
        self.assertEqual(96, balance(account)["available"])

    def test_reopening_spent_refund_requires_hr_adjustment(self):
        account = self.account()
        item = self.approve(self.request(days=1), Decimal("8"))
        self.event("in", item.starts_at + timedelta(hours=8))
        self.event("out", item.starts_at + timedelta(hours=12))
        run = self.generate(item)
        approve_payroll_run(run, self.owner)
        adjust(account, Decimal("-96"), "Remove available allowance", self.owner)
        with self.assertRaisesMessage(ValidationError, "Previously refunded leave has been spent"):
            reopen_payroll_run(run, self.owner, "Reconcile worked time with leave")
        run.refresh_from_db()
        self.assertEqual("approved", run.status)
        self.assertEqual(1, run.leave_usages.count())
        adjust(account, Decimal("4"), "Restore refunded hours", self.owner)
        reopen_payroll_run(run, self.owner, "Reconcile worked time with leave")
        self.assertEqual(0, balance(account)["available"])

    def test_split_period_reserved_and_used_amounts_and_cent_rounding(self):
        account = self.account()
        item = self.approve(self.request(days=1), Decimal("0.03"))
        middle = item.starts_at + timedelta(hours=12)
        first = self.generate(item, end=middle)
        second = self.generate(item, start=middle)
        self.assertEqual(Decimal("0.03"), Decimal(first.snapshot[0]["total_hours"]) + Decimal(second.snapshot[0]["total_hours"]))
        approve_payroll_run(first, self.owner)
        self.assertEqual(Decimal("0.01"), balance(account)["reserved"])
        self.assertEqual(Decimal("0.02"), balance(account)["used"])
        approve_payroll_run(second, self.owner)
        self.assertEqual(Decimal("0.03"), balance(account)["used"])

    def test_bank_work_changes_outside_original_period_require_refresh(self):
        self.account()
        item = self.approve(self.request(days=1), Decimal("8"))
        run = self.generate(item)
        self.event("in", item.starts_at - timedelta(hours=1))
        self.event("out", item.ends_at)
        with self.assertRaisesMessage(ValidationError, "regenerate"):
            approve_payroll_run(run, self.owner)
        self.assertFalse(run.leave_usages.exists())

    def test_bank_leave_company_partial_lock_requires_whole_approval(self):
        self.account()
        item = self.approve(self.request())
        run = self.generate(item)
        with self.assertRaisesMessage(ValidationError, "whole-period approval"):
            set_payroll_lock_segment(run, self.owner, "locked", "Lock the company slice")

    def test_tenant_and_supervisor_bank_access(self):
        other = Organization.objects.create(legal_name="Other", slug="other-bank")
        outsider = get_user_model().objects.create_user(username="bank-outsider")
        Membership.objects.create(organization=other, user=outsider, role="owner")
        account = self.account()
        with self.assertRaises(PermissionDenied):
            adjust(account, Decimal("8"), "Cross-company grant", outsider)
        with self.assertRaises(PermissionDenied):
            reserve(self.request(), Decimal("8"), outsider)
        self.assertEqual(100, balance(account)["available"])

    def test_scoped_supervisor_can_reserve_without_hr_balance_edit_access(self):
        self.account()
        branch = Branch.objects.create(organization=self.org, name="North")
        self.person.branch = branch
        self.person.save()
        supervisor = get_user_model().objects.create_user(username="bank-supervisor")
        membership = Membership.objects.create(organization=self.org, user=supervisor, role="supervisor")
        AuthorityScope.objects.create(organization=self.org, membership=membership, branch=branch)
        self.client.force_login(supervisor)
        item = self.approve(self.request())
        self.assertEqual(16, item.confirmed_leave_hours)
        self.assertEqual(403, self.client.post(reverse("employee_leave", args=[self.person.pk]),
            {"action": "adjust", "hours": "16", "reason": "Supervisor grant"}).status_code)
        self.assertNotContains(self.client.get(reverse("person_detail", args=[self.person.pk])), "Grant or adjust hours")

    def test_decline_needs_no_paid_hour_confirmation(self):
        self.account()
        item = self.request()
        response = self.client.post(reverse("time_off_decide", args=[item.pk]), {"action": "declined"})
        self.assertEqual(302, response.status_code)
        item.refresh_from_db()
        self.assertEqual("declined", item.status)
        self.assertFalse(item.bank_entries.exists())

    def test_pending_correction_on_boundary_tour_blocks_bank_approval(self):
        from .models import PunchAdjustment
        self.account()
        item = self.approve(self.request(days=1), Decimal("8"))
        start = self.event("in", item.starts_at - timedelta(hours=1))
        self.event("out", item.ends_at)
        PunchAdjustment.objects.create(organization=self.org, punch=start, requested_by=self.worker,
            proposed_at=item.starts_at + timedelta(hours=12), reason="Actual time")
        self.assertTrue(self.generate(item).exceptions)
