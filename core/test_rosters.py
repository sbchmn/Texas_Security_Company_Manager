from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse

from .forms import PersonnelAssignmentsForm, TimePolicyForm
from .models import (
    AuditEvent, AuthorityScope, Branch, Client, Membership, Organization, Person,
    PersonnelAssignment, Shift, ShiftTemplate, Site, TimePolicy,
)
from .rosters import add_roster_assignments, pay_period_bounds, remove_roster_assignment
from .scope import for_membership, manager_recipients_by_person
from .services import recurring_plan, shift_eligibility

ZONE = ZoneInfo("America/Chicago")


def moment(day, hour=0):
    return datetime(2026, 10, day, hour, tzinfo=ZONE)


class PersonnelRosterTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(legal_name="Roster Security", display_name="Roster Security", slug="roster")
        self.owner = get_user_model().objects.create_user(username="roster-owner")
        self.supervisor = get_user_model().objects.create_user(username="roster-supervisor")
        self.worker = get_user_model().objects.create_user(username="roster-worker")
        Membership.objects.create(organization=self.org, user=self.owner, role=Membership.Role.OWNER)
        self.membership = Membership.objects.create(organization=self.org, user=self.supervisor, role=Membership.Role.SUPERVISOR)
        Membership.objects.create(organization=self.org, user=self.worker, role=Membership.Role.OFFICER)
        self.branch = Branch.objects.create(organization=self.org, name="Branch")
        self.client_a = Client.objects.create(organization=self.org, name="Client A")
        self.client_b = Client.objects.create(organization=self.org, name="Client B")
        self.site = Site.objects.create(organization=self.org, client=self.client_a, name="Site A", address="A")
        self.other_site = Site.objects.create(organization=self.org, client=self.client_a, name="Site A2", address="A2")
        self.site_b = Site.objects.create(organization=self.org, client=self.client_b, name="Site B", address="B")
        self.person = Person.objects.create(organization=self.org, user=self.worker, first_name="Roster",
            last_name="Officer", status=Person.Status.ACTIVE)
        self.policy = TimePolicy.objects.create(organization=self.org, workweek_start=0,
            pay_period_weeks=2, pay_period_anchor=date(2026, 10, 5))
        AuthorityScope.objects.create(organization=self.org, membership=self.membership, site=self.site)
        self.clock = patch("django.utils.timezone.now", return_value=moment(8, 10))
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def add(self, clients=(), sites=()):
        return add_roster_assignments(self.person, clients, sites, self.owner)

    def shift(self, day=21, status=Shift.Status.PUBLISHED, site=None):
        return Shift.objects.create(organization=self.org, officer=self.person, site=site or self.site,
            starts_at=moment(day, 8), ends_at=moment(day, 16), status=status)

    def test_explicit_site_roster_exposes_person_before_first_shift_on_all_scoped_surfaces(self):
        self.assertFalse(for_membership(self.membership).permits_person(self.person))
        self.add(sites=[self.site])
        self.assertTrue(for_membership(self.membership).permits_person(self.person))
        self.assertFalse(self.person.shifts.exists())
        self.client.force_login(self.supervisor)
        self.assertContains(self.client.get(reverse("people")), "Roster Officer")
        self.assertEqual(200, self.client.get(reverse("person_detail", args=[self.person.pk])).status_code)
        form = self.client.get(reverse("shift_create")).context["form"]
        self.assertIn(self.person, list(form.fields["officer"].queryset))
        self.assertIn(self.supervisor.pk, manager_recipients_by_person(self.org)[self.person.pk])

    def test_client_assignment_covers_each_site_and_multiple_independent_targets(self):
        self.add(clients=[self.client_a], sites=[self.site_b])
        scope = for_membership(self.membership)
        self.assertTrue(scope.permits_person(self.person))
        AuthorityScope.objects.filter(membership=self.membership).update(site=self.other_site)
        self.assertTrue(for_membership(self.membership).permits_person(self.person))
        AuthorityScope.objects.filter(membership=self.membership).update(site=self.site_b)
        self.assertTrue(for_membership(self.membership).permits_person(self.person))
        self.assertEqual(2, self.person.roster_assignments.count())
        self.assertEqual(0, self.person.shifts.count())

    def test_add_is_idempotent_and_reassignment_restores_visibility_after_expiry(self):
        row = self.add(sites=[self.site])[0]
        self.assertEqual([], self.add(sites=[self.site]))
        self.shift()
        removed = remove_roster_assignment(self.person, row.pk, self.owner)
        with patch("django.utils.timezone.now", return_value=removed.roster_access_until):
            self.assertFalse(for_membership(self.membership).permits_person(self.person))
            restored = self.add(sites=[self.site])[0]
            self.assertEqual(row.pk, restored.pk)
            self.assertTrue(restored.active)
            self.assertIsNone(restored.roster_access_until)
            self.assertTrue(for_membership(self.membership).permits_person(self.person))
        self.assertEqual(1, self.person.roster_assignments.count())
        self.assertEqual(2, AuditEvent.objects.filter(action="person.roster_assigned").count())
        self.assertTrue(AuditEvent.objects.filter(action="person.roster_removed").exists())

    def test_removal_preserves_shifts_blocks_all_new_assignments_and_expires_at_following_period_end(self):
        row = self.add(sites=[self.site])[0]
        existing = self.shift()
        removed = remove_roster_assignment(self.person, row.pk, self.owner)
        self.assertEqual(datetime(2026, 11, 16, tzinfo=ZONE), removed.roster_access_until)
        self.assertEqual(existing.ends_at, removed.last_shift_end)
        self.assertTrue(for_membership(self.membership).permits_person(self.person))
        self.assertEqual(Shift.Status.PUBLISHED, Shift.objects.get(pk=existing.pk).status)
        allowed, reasons = shift_eligibility(existing)
        self.assertTrue(allowed, reasons)
        existing.post_name = "Updated instructions"
        existing.save()
        probe = Shift(organization=self.org, site=self.site, officer=self.person,
            starts_at=moment(22, 8), ends_at=moment(22, 16))
        self.assertFalse(shift_eligibility(probe)[0])
        self.assertTrue(shift_eligibility(existing, purpose="clock")[0])
        with self.assertRaisesMessage(ValidationError, "removed"):
            probe.save()
        with self.assertRaisesMessage(ValidationError, "removed"):
            probe.full_clean()
        with patch("django.utils.timezone.now", return_value=removed.roster_access_until - timedelta(microseconds=1)):
            self.assertTrue(for_membership(self.membership).permits_person(self.person))
        with patch("django.utils.timezone.now", return_value=removed.roster_access_until):
            self.assertFalse(for_membership(self.membership).permits_person(self.person))
            self.client.force_login(self.supervisor)
            self.assertNotContains(self.client.get(reverse("people")), "Roster Officer")
            self.assertEqual(404, self.client.get(reverse("person_detail", args=[self.person.pk])).status_code)
            self.assertNotIn(self.person, list(self.client.get(reverse("shift_create")).context["form"].fields["officer"].queryset))
            # Historical site shifts remain reviewable, not erased.
            self.assertTrue(for_membership(self.membership).permits_shift(existing))

    def test_removed_employee_cannot_extend_or_move_existing_shift(self):
        row = self.add(sites=[self.site])[0]
        existing = self.shift()
        remove_roster_assignment(self.person, row.pk, self.owner)
        existing.ends_at += timedelta(hours=1)
        with self.assertRaisesMessage(ValidationError, "removed"):
            existing.save()
        existing.refresh_from_db()
        existing.starts_at += timedelta(days=1)
        existing.ends_at += timedelta(days=1)
        with self.assertRaisesMessage(ValidationError, "removed"):
            existing.save()

    def test_cancelled_shift_cannot_be_restored_after_removal(self):
        row = self.add(sites=[self.site])[0]
        existing = self.shift()
        remove_roster_assignment(self.person, row.pk, self.owner)
        existing.status = Shift.Status.CANCELLED
        existing.save()
        existing.status = Shift.Status.PUBLISHED
        with self.assertRaisesMessage(ValidationError, "removed"):
            existing.save()

    def test_no_shifts_uses_removal_period_and_past_final_shift_can_expire_immediately(self):
        row = self.add(sites=[self.site])[0]
        removed = remove_roster_assignment(self.person, row.pk, self.owner)
        self.assertEqual(datetime(2026, 11, 2, tzinfo=ZONE), removed.roster_access_until)
        self.add(sites=[self.site])
        Shift.objects.create(organization=self.org, site=self.site, officer=self.person,
            starts_at=datetime(2026, 8, 1, 8, tzinfo=ZONE), ends_at=datetime(2026, 8, 1, 16, tzinfo=ZONE))
        remove_roster_assignment(self.person, row.pk, self.owner)
        self.assertFalse(for_membership(self.membership).permits_person(self.person))

    def test_cancelled_future_shifts_do_not_extend_cutoff(self):
        row = self.add(sites=[self.site])[0]
        existing = self.shift(day=10)
        self.shift(day=28, status=Shift.Status.CANCELLED)
        removed = remove_roster_assignment(self.person, row.pk, self.owner)
        self.assertEqual(existing.ends_at, removed.last_shift_end)
        self.assertEqual(datetime(2026, 11, 2, tzinfo=ZONE), removed.roster_access_until)

    def test_site_removal_does_not_override_active_client_and_client_removal_keeps_individual_site(self):
        site_row = self.add(sites=[self.site])[0]
        client_row = self.add(clients=[self.client_a])[0]
        remove_roster_assignment(self.person, site_row.pk, self.owner)
        self.shift()
        self.add(sites=[self.other_site])
        remove_roster_assignment(self.person, client_row.pk, self.owner)
        self.shift(day=22, site=self.other_site)
        with self.assertRaisesMessage(ValidationError, "removed"):
            self.shift(day=23)

    def test_legacy_visibility_survives_for_untouched_sites_but_removed_target_no_longer_grants_perpetual_access(self):
        self.shift()
        self.assertTrue(for_membership(self.membership).permits_person(self.person))
        row = self.add(sites=[self.site])[0]
        removed = remove_roster_assignment(self.person, row.pk, self.owner)
        self.shift(day=22, site=self.site_b)
        with patch("django.utils.timezone.now", return_value=removed.roster_access_until):
            self.assertFalse(for_membership(self.membership).permits_person(self.person))
            AuthorityScope.objects.filter(membership=self.membership).update(site=self.site_b)
            self.assertTrue(for_membership(self.membership).permits_person(self.person))

    def test_branch_authority_still_covers_own_personnel_after_site_grace(self):
        self.person.branch = self.branch
        self.person.save()
        AuthorityScope.objects.create(organization=self.org, membership=self.membership, branch=self.branch)
        row = self.add(sites=[self.site])[0]
        removed = remove_roster_assignment(self.person, row.pk, self.owner)
        with patch("django.utils.timezone.now", return_value=removed.roster_access_until):
            self.assertTrue(for_membership(self.membership).permits_person(self.person))
            with self.assertRaisesMessage(ValidationError, "removed"):
                self.shift()

    def test_foreign_targets_and_person_assignment_ids_are_rejected(self):
        foreign_org = Organization.objects.create(legal_name="Foreign", display_name="Foreign", slug="foreign-roster")
        foreign_client = Client.objects.create(organization=foreign_org, name="Foreign client")
        with self.assertRaises(ValidationError):
            self.add(clients=[foreign_client])
        form = PersonnelAssignmentsForm({"clients": [foreign_client.pk]}, organization=self.org)
        self.assertFalse(form.is_valid())
        other_person = Person.objects.create(organization=self.org, first_name="Other", last_name="Officer")
        other_row = add_roster_assignments(other_person, [], [self.site], self.owner)[0]
        with self.assertRaises(PersonnelAssignment.DoesNotExist):
            remove_roster_assignment(self.person, other_row.pk, self.owner)
        self.assertTrue(PersonnelAssignment.objects.get(pk=other_row.pk).active)

    def test_only_company_personnel_managers_can_change_rosters_inline(self):
        self.client.force_login(self.supervisor)
        self.add(sites=[self.site])
        response = self.client.get(reverse("person_detail", args=[self.person.pk]))
        self.assertContains(response, "Site A / Client A")
        self.assertNotContains(response, "Save assignments")
        self.assertEqual(403, self.client.post(reverse("person_detail", args=[self.person.pk]),
            {"action": "roster_add", "sites": [self.site_b.pk]}).status_code)
        with self.assertRaises(PermissionDenied):
            add_roster_assignments(self.person, [], [self.site_b], self.supervisor)
        self.client.force_login(self.owner)
        response = self.client.post(reverse("person_detail", args=[self.person.pk]),
            {"action": "roster_add", "clients": [self.client_b.pk], "sites": [self.other_site.pk]})
        self.assertEqual(302, response.status_code)
        page = self.client.get(reverse("person_detail", args=[self.person.pk]))
        self.assertContains(page, "Client: Client B (all sites)")
        self.assertContains(page, "Save assignments")
        text = page.content.decode()
        self.assertLess(text.index('id="roster-assignments"'), text.index("<h2>Custom fields</h2>"))
        self.assertIn('class="person-profile-sidebar"', text)

    def test_inline_validation_and_removal_show_errors_without_leaving_file(self):
        self.client.force_login(self.owner)
        url = reverse("person_detail", args=[self.person.pk])
        self.assertContains(self.client.post(url, {"action": "roster_add"}), "Choose at least one")
        self.assertContains(self.client.post(url, {"action": "roster_remove", "assignment_id": "not-an-id"}), "Choose an assignment")
        row = self.add(sites=[self.site])[0]
        self.assertEqual(302, self.client.post(url, {"action": "roster_remove", "assignment_id": row.pk}).status_code)
        page = self.client.get(url)
        self.assertContains(page, "No new shifts")
        self.assertContains(page, "Roster access retained until")
        self.assertContains(self.client.post(url, {"action": "roster_remove", "assignment_id": row.pk}), "already removed")

    def test_policy_change_does_not_recalculate_existing_removal_cutoff(self):
        row = self.add(sites=[self.site])[0]
        removed = remove_roster_assignment(self.person, row.pk, self.owner)
        self.policy.pay_period_weeks = 4
        self.policy.save()
        removed.refresh_from_db()
        self.assertEqual(datetime(2026, 11, 2, tzinfo=ZONE), removed.roster_access_until)

    def test_recurring_plans_do_not_add_more_shifts_for_removed_officer(self):
        row = self.add(sites=[self.site])[0]
        remove_roster_assignment(self.person, row.pk, self.owner)
        template = ShiftTemplate.objects.create(organization=self.org, site=self.site, officer=self.person,
            name="Removed recurring", start_time=time(8), end_time=time(16), weekdays=[0],
            series_start=date(2026, 10, 5))
        plan = recurring_plan(template, date(2026, 10, 12), date(2026, 10, 12), Shift.Status.DRAFT)
        self.assertEqual(0, plan["count"])
        self.assertIn("removed", " ".join(plan["rows"][0]["reasons"]))

    def test_employee_can_hold_multiple_clients_and_multiple_sites(self):
        self.add(clients=[self.client_a, self.client_b], sites=[self.site, self.other_site])
        self.assertEqual(4, self.person.roster_assignments.filter(active=True).count())
        client_membership = Membership.objects.create(organization=self.org,
            user=get_user_model().objects.create_user(username="roster-client-supervisor"),
            role=Membership.Role.SUPERVISOR)
        AuthorityScope.objects.create(organization=self.org, membership=client_membership, client=self.client_b)
        self.assertTrue(for_membership(client_membership).permits_person(self.person))

    def test_midnight_final_shift_uses_period_just_completed(self):
        row = self.add(sites=[self.site])[0]
        Shift.objects.create(organization=self.org, site=self.site, officer=self.person,
            starts_at=moment(18, 16), ends_at=moment(19))
        removed = remove_roster_assignment(self.person, row.pk, self.owner)
        self.assertEqual(datetime(2026, 11, 2, tzinfo=ZONE), removed.roster_access_until)

    def test_shift_forms_block_new_draft_and_published_assignments_after_removal(self):
        row = self.add(sites=[self.site])[0]
        self.shift(day=10)
        remove_roster_assignment(self.person, row.pk, self.owner)
        self.client.force_login(self.supervisor)
        for status in (Shift.Status.DRAFT, Shift.Status.PUBLISHED):
            response = self.client.post(reverse("shift_create"),
                {"site": self.site.pk, "officer": self.person.pk, "status": status,
                 "starts_at": "2026-10-22T08:00", "ends_at": "2026-10-22T16:00"})
            self.assertEqual(200, response.status_code)
            self.assertContains(response, "removed from this client/site roster")
        self.assertEqual(1, self.person.shifts.count())

    def test_row_validation_rejects_cross_tenant_and_both_targets(self):
        org = Organization.objects.create(legal_name="Elsewhere", display_name="Elsewhere", slug="roster-elsewhere")
        row = PersonnelAssignment(organization=org, person=self.person, site=self.site)
        with self.assertRaisesMessage(ValidationError, "same company"):
            row.full_clean()
        row.organization = self.org
        row.client = self.client_a
        with self.assertRaises(ValidationError):
            row.full_clean()


class PayCalendarTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(legal_name="Calendar", display_name="Calendar", slug="pay-calendar")
        self.policy = TimePolicy.objects.create(organization=self.org, workweek_start=0,
            pay_period_weeks=2, pay_period_anchor=date(2026, 10, 5))

    def test_periods_use_anchor_before_after_and_at_boundary(self):
        self.assertEqual((moment(5), moment(19)), pay_period_bounds(self.org, moment(8)))
        self.assertEqual((moment(19), datetime(2026, 11, 2, tzinfo=ZONE)), pay_period_bounds(self.org, moment(19)))
        self.assertEqual((datetime(2026, 9, 21, tzinfo=ZONE), moment(5)), pay_period_bounds(self.org, moment(4)))

    def test_dst_keeps_local_midnight_and_following_period_cutoff_is_not_fixed_utc_duration(self):
        start, end = pay_period_bounds(self.org, datetime(2026, 11, 3, tzinfo=ZONE))
        self.assertEqual(datetime(2026, 11, 2, tzinfo=ZONE), start)
        self.assertEqual(datetime(2026, 11, 16, tzinfo=ZONE), end)
        self.policy.pay_period_anchor = date(2026, 10, 26)
        self.policy.save()
        start, end = pay_period_bounds(self.org, moment(28))
        self.assertEqual(timedelta(hours=-5), start.utcoffset())
        self.assertEqual(timedelta(hours=-6), end.utcoffset())
        self.assertEqual(0, start.hour)
        self.assertEqual(0, end.hour)

    def test_weekly_default_follows_weekday_and_multiweek_requires_matching_anchor(self):
        self.policy.pay_period_weeks = 1
        self.policy.pay_period_anchor = None
        self.policy.workweek_start = 6
        self.policy.save()
        self.assertEqual((moment(4), moment(11)), pay_period_bounds(self.org, moment(8)))
        self.policy.pay_period_weeks = 2
        with self.assertRaises(ValidationError):
            self.policy.full_clean()
        self.policy.pay_period_anchor = date(2026, 10, 5)
        with self.assertRaises(ValidationError):
            self.policy.full_clean()

    def test_form_validates_calendar_and_preserves_old_clients_defaults(self):
        data = {"timezone": "America/Chicago", "workweek_start": 0, "overtime_after_hours": "40",
            "rounding_mode": "exact", "rounding_minutes": 1, "arrival_grace_minutes": 5,
            "departure_grace_minutes": 5, "pay_period_weeks": 2, "pay_period_anchor": "2026-10-06"}
        form = TimePolicyForm(data, instance=self.policy)
        self.assertFalse(form.is_valid())
        self.assertIn("pay_period_anchor", form.errors)
        data["pay_period_anchor"] = "2026-10-05"
        form = TimePolicyForm(data, instance=self.policy)
        self.assertTrue(form.is_valid(), form.errors)

    def test_payroll_prefills_calculated_dates_and_keeps_manual_generation(self):
        from .models import PayrollRun
        owner = get_user_model().objects.create_user(username="calendar-owner")
        Membership.objects.create(organization=self.org, user=owner, role=Membership.Role.OWNER)
        self.client.force_login(owner)
        with patch("django.utils.timezone.now", return_value=moment(8)):
            page = self.client.get(reverse("payroll") + "?period=1")
            self.assertEqual(moment(19), page.context["form"].initial["period_start"])
            self.assertEqual(datetime(2026, 11, 2, tzinfo=ZONE), page.context["form"].initial["period_end"])
        self.assertContains(page, "Previous period")
        self.assertContains(page, "Oct 19 &ndash; Nov 1, 2026")
        self.assertContains(page, '<details class="payroll-period-advanced">')
        response = self.client.post(reverse("payroll"),
            {"period_start": "2026-10-07T00:00", "period_end": "2026-10-09T00:00"})
        self.assertEqual(302, response.status_code)
        run = PayrollRun.objects.get()
        self.assertEqual(moment(7), run.period_start)
        self.assertEqual(moment(9), run.period_end)
        page = self.client.get(reverse("payroll") + f"?run={run.pk}")
        self.assertContains(page, '<details class="payroll-period-advanced" open>')
        self.assertContains(page, "Oct 7 &ndash; Oct 8, 2026")

    def test_payroll_invalid_manual_dates_expand_advanced_and_keep_errors(self):
        owner = get_user_model().objects.create_user(username="calendar-errors")
        Membership.objects.create(organization=self.org, user=owner, role=Membership.Role.OWNER)
        self.client.force_login(owner)
        page = self.client.post(reverse("payroll") + "?period=0",
            {"period_start": "2026-10-09T00:00", "period_end": "2026-10-07T00:00"})
        self.assertEqual(200, page.status_code)
        self.assertContains(page, '<details class="payroll-period-advanced" open>')
        self.assertContains(page, "Period end must be after start.")
        self.assertContains(page, 'value="2026-10-09T00:00"')

    def test_payroll_navigation_limits_disable_arrows(self):
        owner = get_user_model().objects.create_user(username="calendar-limits")
        Membership.objects.create(organization=self.org, user=owner, role=Membership.Role.OWNER)
        self.client.force_login(owner)
        for offset, label in ((-26, "Previous period"), (26, "Next period")):
            page = self.client.get(reverse("payroll") + f"?period={offset}")
            self.assertContains(page, f'<span aria-disabled="true" aria-label="{label}">')
