import json
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    AuditEvent, AuthorityScope, Client, HoldOver, Membership, Organization, PayCategory,
    PayrollRun, Person, Punch, PunchAdjustment, Shift, ShiftHourDesignation, Site, TimePolicy,
)
from .services import create_payroll_run, payroll_rows, record_punch, set_shift_designation
from .timekeeping import pair_tours


class TimesheetsTest(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(second=0, microsecond=0)
        self.start = self.now - timedelta(hours=8)
        self.org = Organization.objects.create(legal_name="Timesheet Security", slug="timesheet-security")
        self.owner = get_user_model().objects.create_user(username="timesheet-owner")
        self.worker = get_user_model().objects.create_user(username="timesheet-worker")
        Membership.objects.create(organization=self.org, user=self.owner, role=Membership.Role.OWNER)
        Membership.objects.create(organization=self.org, user=self.worker, role=Membership.Role.OFFICER)
        self.person = Person.objects.create(organization=self.org, user=self.worker, first_name="Ari", last_name="Guard")
        customer = Client.objects.create(organization=self.org, name="Customer")
        self.site = Site.objects.create(organization=self.org, client=customer, name="Gate", latitude="32.7", longitude="-96.8")
        self.shift = Shift.objects.create(organization=self.org, site=self.site, officer=self.person,
            starts_at=self.start, ends_at=self.now, status=Shift.Status.PUBLISHED)
        TimePolicy.objects.create(organization=self.org, require_geofence=False, rounding_mode="exact")
        self.client.force_login(self.owner)

    def punch(self, kind, minutes=0, **values):
        return Punch.objects.create(organization=self.org, person=self.person, shift=self.shift,
            kind=kind, occurred_at=self.start + timedelta(minutes=minutes), client_event_id=uuid.uuid4(), **values)

    def tour(self):
        self.clock_in = self.punch("in")
        self.break_start = self.punch("break_start", 180)
        self.break_end = self.punch("break_end", 210)
        self.clock_out = self.punch("out", 480)
        return [self.clock_in, self.break_start, self.break_end, self.clock_out]

    def rows(self):
        return payroll_rows(self.org, self.start - timedelta(minutes=1), self.now + timedelta(minutes=1))

    def test_paid_breaks_do_not_reduce_hours(self):
        events = self.tour()
        tour = pair_tours(events)[0]
        self.assertEqual(tour["break_minutes"], 30)
        self.assertEqual(tour["paid_minutes"], 480)
        self.assertFalse(tour["needs_review"])
        self.assertEqual(self.rows()[0]["total_hours"], Decimal("8.00"))

    def test_explicitly_unpaid_break_deducted_once(self):
        self.tour()
        response = self.client.post(reverse("punch_detail", args=[self.break_start.pk]),
                                    {"action": "break_pay", "paid": "no", "reason": "Unpaid meal verified"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.rows()[0]["total_hours"], Decimal("7.50"))
        self.assertEqual(self.rows()[0]["raw_hours"], Decimal("8.00"))
        self.break_start.refresh_from_db()
        self.assertFalse(self.break_start.break_paid)
        self.assertTrue(AuditEvent.objects.filter(action="break.classified").exists())

    def test_multiple_breaks_and_correction_use_actual_intervals(self):
        self.tour()
        self.break_start.break_paid = False
        self.break_start.save()
        second = self.punch("break_start", 300, break_paid=False)
        self.punch("break_end", 315)
        PunchAdjustment.objects.create(organization=self.org, punch=self.break_end, requested_by=self.owner,
            proposed_at=self.start + timedelta(minutes=220), reason="Actual break end verified",
            status="approved", reviewed_by=self.owner, reviewed_at=self.now)
        self.assertEqual(self.rows()[0]["total_hours"], Decimal("7.08"))
        self.assertFalse(second.break_paid)

    def test_missing_break_end_is_not_invented_or_deducted(self):
        self.punch("in")
        self.punch("break_start", 180, break_paid=False)
        self.punch("out", 480)
        self.assertEqual(self.rows()[0]["total_hours"], Decimal("8.00"))
        run = create_payroll_run(organization=self.org, start=self.start, end=self.now + timedelta(minutes=1), actor=self.owner)
        self.assertTrue(any("break ended" in entry["reason"] for entry in run.exceptions))

    def test_manual_designation_conflict_is_visible_and_not_double_deducted(self):
        category = PayCategory.objects.create(organization=self.org, kind="break", name="Meal", paid=False,
                                             counts_toward_overtime=False)
        ShiftHourDesignation.objects.create(organization=self.org, shift=self.shift, category=category,
                                           hours="0.50", reason="Legacy break entry", recorded_by=self.owner)
        self.tour()
        self.break_start.break_paid = False
        self.break_start.save()
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total_hours"], Decimal("7.50"))
        self.assertIn("conflict", rows[0]["exception"])
        with self.assertRaisesMessage(ValidationError, "recorded break punches"):
            set_shift_designation(self.shift, category, "0.50", "Meal verified", self.owner)

    def test_capture_breaks_are_paid_and_do_not_break_clock_out_matching(self):
        for kind, minutes in (("in", 0), ("break_start", 180), ("break_end", 210), ("out", 480)):
            punch, _ = record_punch(organization=self.org, person=self.person, shift=self.shift,
                kind=kind, occurred_at=self.start + timedelta(minutes=minutes), client_event_id=uuid.uuid4())
            self.assertEqual(punch.review_status, "accepted", punch.exception_reason)
            self.assertTrue(punch.break_paid)
        self.assertEqual(self.rows()[0]["total_hours"], Decimal("8.00"))

    def test_invalid_break_sequence_preserves_evidence_for_review(self):
        punch, created = record_punch(organization=self.org, person=self.person, shift=self.shift,
            kind="break_end", occurred_at=self.start, client_event_id=uuid.uuid4(), offline=True)
        self.assertTrue(created)
        self.assertEqual(punch.review_status, "pending")
        self.assertIn("sequence", punch.exception_reason)

    def test_offline_break_api_reuses_existing_encrypted_device_path(self):
        self.client.force_login(self.worker)
        enrollment = self.client.post(reverse("clock_device_enroll"), data=json.dumps({"label": "Phone"}), content_type="application/json")
        token = enrollment.json()["token"]
        for sequence, kind, minutes in ((1, "in", 0), (2, "break_start", 180), (3, "break_end", 210), (4, "out", 480)):
            payload = {"device_token": token, "device_sequence": sequence, "kind": kind,
                       "occurred_at": (self.start + timedelta(minutes=minutes)).isoformat(),
                       "shift_id": str(self.shift.pk), "offline": True, "client_event_id": str(uuid.uuid4())}
            response = self.client.post(reverse("offline_punch_sync"), json.dumps(payload), content_type="application/json")
            self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.rows()[0]["total_hours"], Decimal("8.00"))

    def test_correction_preserves_original_and_latest_approved_wins(self):
        self.tour()
        original = self.clock_out.occurred_at
        for minute in (470, 465):
            response = self.client.post(reverse("punch_detail", args=[self.clock_out.pk]), {
                "action": "correct", "proposed_at": (self.start + timedelta(minutes=minute)).isoformat(),
                "reason": "Confirmed with supervisor",
            })
            self.assertEqual(response.status_code, 302)
        self.clock_out.refresh_from_db()
        self.assertEqual(self.clock_out.occurred_at, original)
        self.assertEqual(self.rows()[0]["total_hours"], Decimal("7.75"))
        self.assertEqual(self.clock_out.adjustments.count(), 2)

    def test_locked_payroll_blocks_correction_and_classification(self):
        self.tour()
        PayrollRun.objects.create(organization=self.org, period_start=self.start, period_end=self.now + timedelta(minutes=1),
                                  status="approved", created_by=self.owner)
        for data in ({"action": "break_pay", "paid": "no", "reason": "Meal verified"},
                     {"action": "correct", "proposed_at": self.break_start.occurred_at.isoformat(), "reason": "Time verified"}):
            response = self.client.post(reverse("punch_detail", args=[self.break_start.pk]), data)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "locked")
        self.assertFalse(PunchAdjustment.objects.exists())
        self.break_start.refresh_from_db()
        self.assertTrue(self.break_start.break_paid)

    def test_correction_invalidates_existing_payroll_draft(self):
        self.tour()
        run = create_payroll_run(organization=self.org, start=self.start, end=self.now + timedelta(minutes=1), actor=self.owner)
        self.assertFalse(run.exceptions)
        self.client.post(reverse("punch_detail", args=[self.break_start.pk]),
                         {"action": "break_pay", "paid": "no", "reason": "Meal verified"})
        run.refresh_from_db()
        self.assertTrue(any("regenerate" in entry["reason"] for entry in run.exceptions))

    def test_officer_cannot_review_or_classify_and_foreign_tenant_404(self):
        self.tour()
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(reverse("time_review")).status_code, 403)
        self.assertEqual(self.client.get(reverse("punch_detail", args=[self.break_start.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("punch_detail", args=[self.break_start.pk]), {"action": "break_pay"}).status_code, 403)
        self.client.force_login(self.owner)
        other = Organization.objects.create(legal_name="Other", slug="other-timesheet")
        person = Person.objects.create(organization=other, first_name="Foreign", last_name="Guard")
        foreign = Punch.objects.create(organization=other, person=person, kind="in", occurred_at=self.now, client_event_id=uuid.uuid4())
        self.assertEqual(self.client.get(reverse("punch_detail", args=[foreign.pk])).status_code, 404)

    def test_scoped_supervisor_cannot_see_out_of_scope_punch(self):
        self.tour()
        user = get_user_model().objects.create_user(username="scoped-timesheet")
        member = Membership.objects.create(organization=self.org, user=user, role="supervisor")
        other_site = Site.objects.create(organization=self.org, client=self.site.client, name="Other gate")
        AuthorityScope.objects.create(organization=self.org, membership=member, site=other_site)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("punch_detail", args=[self.clock_in.pk])).status_code, 404)
        response = self.client.get(reverse("time_review"))
        self.assertNotContains(response, self.person.full_name)

    def test_timesheets_modal_links_and_legacy_redirect(self):
        self.tour()
        response = self.client.get(reverse("time_review"))
        self.assertContains(response, "Timesheets")
        self.assertContains(response, "data-timesheet-dialog")
        self.assertContains(response, reverse("punch_detail", args=[self.clock_in.pk]))
        self.assertRedirects(self.client.get("/time/review/?punches=all"), reverse("time_review") + "?punches=all")
        modal = self.client.get(reverse("punch_detail", args=[self.break_start.pk]) + "?modal=1")
        self.assertNotContains(modal, 'aria-label="Main navigation"')
        self.assertContains(modal, 'class="punch-modal-page"')
        self.assertContains(modal, "Break pay classification")
        self.assertIn("frame-ancestors 'self'", modal["Content-Security-Policy"])
        self.assertEqual(modal["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("no-store", modal["Cache-Control"])

    @override_settings(TIMESHEET_MAP_PROVIDER="osm", TIMESHEET_MAP_TILE_URL="https://tile.openstreetmap.org/{z}/{x}/{y}.png")
    def test_location_evidence_map_and_holdover(self):
        punch, _ = record_punch(organization=self.org, person=self.person, shift=self.shift, kind="in",
            occurred_at=self.start, client_event_id=uuid.uuid4(), latitude=32.7, longitude=-96.8, accuracy_m=12, fix_age_seconds=3)
        HoldOver.objects.create(organization=self.org, shift=self.shift, officer=self.person,
            scheduled_ends_at=self.now, expected_release_at=self.now + timedelta(minutes=30), reason="unfilled",
            note="Relief is en route", recorded_by=self.owner)
        response = self.client.get(reverse("punch_detail", args=[punch.pk]))
        self.assertContains(response, "12 metres")
        self.assertContains(response, "3 seconds")
        self.assertContains(response, "Relief is en route")
        self.assertContains(response, "Load location map")
        self.assertIn("https://tile.openstreetmap.org", response["Content-Security-Policy"])
        self.assertNotIn("maps.googleapis.com", response["Content-Security-Policy"])

    def test_no_location_no_map_and_zero_coordinates_are_valid(self):
        punch = self.punch("in")
        self.assertNotContains(self.client.get(reverse("punch_detail", args=[punch.pk])), "data-map-load")
        punch.latitude, punch.longitude = 0, 0
        punch.save()
        response = self.client.get(reverse("punch_detail", args=[punch.pk]))
        self.assertContains(response, "Maps are disabled")
        self.assertEqual(response.context["map_data"]["lat"], 0)

    def test_date_site_filters_and_bad_input(self):
        self.tour()
        response = self.client.get(reverse("time_review"), {"site": str(self.site.pk),
            "start": timezone.localdate(self.start).isoformat(), "end": timezone.localdate(self.now).isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["tours"]), 1)
        for data in ({"site": "invalid"}, {"start": "bad"}, {"start": "2020-01-01", "end": "2026-01-01"}):
            self.assertEqual(self.client.get(reverse("time_review"), data).status_code, 404)

    def test_unlinked_clock_out_preserves_existing_payroll_pairing(self):
        self.tour()
        self.clock_out.shift = None
        self.clock_out.save()
        self.assertEqual(self.rows()[0]["total_hours"], Decimal("8.00"))

    def test_review_filter_is_applied_before_pagination(self):
        self.tour()
        response = self.client.get(reverse("time_review"), {"view": "review"})
        self.assertEqual(response.context["sheet_page"].paginator.count, 0)
        self.break_end.review_status = "pending"
        self.break_end.save()
        response = self.client.get(reverse("time_review"), {"view": "review"})
        self.assertEqual(response.context["sheet_page"].paginator.count, 1)

    @override_settings(TIMESHEET_MAP_PROVIDER="google", GOOGLE_MAPS_BROWSER_KEY="browser-test-key")
    def test_google_configuration_is_scoped_to_authorized_detail(self):
        punch = self.punch("in", latitude=32.7, longitude=-96.8)
        response = self.client.get(reverse("punch_detail", args=[punch.pk]))
        self.assertContains(response, "browser-test-key")
        self.assertIn("https://maps.googleapis.com", response["Content-Security-Policy"])
        listing = self.client.get(reverse("time_review"))
        self.assertNotContains(listing, "browser-test-key")
        self.assertNotIn("maps.googleapis.com", listing["Content-Security-Policy"])

    def test_week_navigation_uses_company_schedule_boundary(self):
        with patch("core.timesheet_views.timezone.localdate", return_value=date(2026, 10, 7)):
            response = self.client.get(reverse("time_review"))
            self.assertEqual(response.context["sheet_start"], date(2026, 10, 5))
            self.assertEqual(response.context["sheet_end"], date(2026, 10, 11))
            self.assertContains(response, 'aria-label="Previous week"')
            self.assertContains(response, 'aria-label="Next week"')
            self.assertContains(response, "Advanced filters")
            policy = TimePolicy.objects.get(organization=self.org)
            policy.workweek_start = 6
            policy.save()
            response = self.client.get(reverse("time_review"), {"week": -1})
            self.assertEqual(response.context["sheet_start"], date(2026, 9, 27))
            self.assertEqual(response.context["sheet_end"], date(2026, 10, 3))

    def test_week_controls_clear_custom_dates_pages_but_keep_filters(self):
        self.tour()
        response = self.client.get(reverse("time_review"), {"start": "2026-10-01", "end": "2026-10-15",
            "person": self.person.pk, "site": self.site.pk, "view": "review", "sheet_page": "2"})
        self.assertTrue(response.context["sheet_custom"])
        self.assertTrue(response.context["sheet_advanced"])
        from html.parser import HTMLParser
        class Links(HTMLParser):
            urls = {}
            def handle_starttag(self, tag, attrs):
                row = dict(attrs)
                if tag == "a" and row.get("aria-label") in ("Previous week", "Next week"):
                    self.urls[row["aria-label"]] = row["href"]
        parser = Links()
        parser.feed(response.content.decode())
        from urllib.parse import parse_qs, urlsplit
        for url in parser.urls.values():
            query = parse_qs(urlsplit(url).query)
            self.assertNotIn("start", query)
            self.assertNotIn("end", query)
            self.assertNotIn("sheet_page", query)
            self.assertEqual(query["person"], [str(self.person.pk)])
            self.assertEqual(query["site"], [str(self.site.pk)])
            self.assertEqual(query["view"], ["review"])
        self.assertEqual(len(parser.urls), 2)

    def test_payroll_period_is_pinned_instead_of_changed_by_week(self):
        run = PayrollRun.objects.create(organization=self.org, created_by=self.owner,
            period_start=self.start - timedelta(days=4), period_end=self.now + timedelta(days=4))
        response = self.client.get(reverse("time_review"), {"run": run.pk, "week": "3"})
        self.assertContains(response, "Pinned to payroll period")
        self.assertContains(response, "Browse schedule weeks instead")
        self.assertNotContains(response, 'aria-label="Next week"')
        self.assertEqual(response.context["sheet_start"], timezone.localdate(run.period_start))
        self.assertEqual(response.context["sheet_end"], timezone.localdate(run.period_end))

    def test_week_offset_is_bounded_and_invalid_value_is_explicit(self):
        response = self.client.get(reverse("time_review"), {"week": "99"})
        self.assertEqual(response.context["sheet_offset"], 26)
        self.assertIsNone(response.context["sheet_next"])
        self.assertEqual(self.client.get(reverse("time_review"), {"week": "not-a-week"}).status_code, 404)

    def test_overnight_tour_is_not_split_at_week_boundary(self):
        self.clock_in = self.punch("in")
        self.clock_in.occurred_at = timezone.make_aware(datetime(2026, 10, 4, 23))
        self.clock_in.save()
        self.clock_out = self.punch("out", 480)
        self.clock_out.occurred_at = self.clock_in.occurred_at + timedelta(hours=8)
        self.clock_out.save()
        with patch("core.timesheet_views.timezone.localdate", return_value=date(2026, 10, 7)):
            response = self.client.get(reverse("time_review"))
        self.assertEqual(len(response.context["tours"]), 1)
        self.assertEqual(response.context["tours"][0]["hours"], 8)
        self.assertEqual(response.context["tours"][0]["start"], self.clock_in)

    def test_selected_week_limits_pending_counts_and_queues(self):
        punch = self.punch("in", review_status="pending")
        punch.occurred_at = timezone.make_aware(datetime(2026, 9, 29, 12))
        punch.save()
        with patch("core.timesheet_views.timezone.localdate", return_value=date(2026, 10, 7)):
            response = self.client.get(reverse("time_review"), {"week": "0"})
            self.assertEqual(response.context["pending_count"], 0)
            self.assertEqual(response.context["punches"].paginator.count, 0)
            previous = self.client.get(reverse("time_review"), {"week": "-1"})
            self.assertEqual(previous.context["pending_count"], 1)
            self.assertEqual(previous.context["punches"].paginator.count, 1)
