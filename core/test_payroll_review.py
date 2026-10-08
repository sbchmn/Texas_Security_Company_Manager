import base64
import re
import uuid
import zlib
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import AuditEvent, Membership, Organization, PayrollRun, Person, Punch, PunchAdjustment
from .services import create_payroll_run, payroll_snapshot_pdf, payroll_snapshot_summary


def pdf_streams(document):
    streams = []
    for header, body in re.findall(rb"<<(.*?)>>\s*stream\r?\n(.*?)endstream", document, re.S):
        if b"ASCII85Decode" in header:
            body = base64.a85decode(body, adobe=True)
        if b"FlateDecode" in header:
            body = zlib.decompress(body)
        if b" Tj" in body:
            streams.append(body)
    return b"\n".join(streams)


class PayrollReviewTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(legal_name="Review Payroll LLC", display_name="Review Payroll", slug="pay-review")
        self.owner = get_user_model().objects.create_user(username="pay-review-owner")
        self.worker = get_user_model().objects.create_user(username="pay-review-worker")
        Membership.objects.create(organization=self.org, user=self.owner, role=Membership.Role.OWNER)
        Membership.objects.create(organization=self.org, user=self.worker, role=Membership.Role.OFFICER)
        self.person = Person.objects.create(organization=self.org, user=self.worker, first_name="Alex", last_name="Officer")
        self.start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
        self.end = self.start + timedelta(days=1)
        self.punch = self.event("in", 8)
        self.event("out", 16)
        self.run = create_payroll_run(organization=self.org, start=self.start, end=self.end, actor=self.owner)
        self.client.force_login(self.owner)

    def event(self, kind, hour, **kwargs):
        return Punch.objects.create(organization=self.org, person=self.person, kind=kind,
            occurred_at=self.start + timedelta(hours=hour), client_event_id=uuid.uuid4(), **kwargs)

    def page(self, suffix=""):
        return self.client.get(reverse("payroll") + f"?run={self.run.pk}{suffix}")

    def test_period_counts_snapshot_and_original_boundary(self):
        self.event("break_start", 10, review_status="pending")
        self.event("break_end", 11, review_status="rejected")
        self.event("in", 24)
        other = Organization.objects.create(legal_name="Other", slug="pay-review-other")
        person = Person.objects.create(organization=other, first_name="Secret", last_name="Person")
        Punch.objects.create(organization=other, person=person, kind="in",
            occurred_at=self.start + timedelta(hours=9), client_event_id=uuid.uuid4())
        page = self.page()
        self.assertEqual(4, page.context["period_counts"]["total"])
        self.assertEqual(2, page.context["period_counts"]["accepted"])
        self.assertEqual(1, page.context["pending_punch_count"])
        self.assertEqual(1, page.context["period_counts"]["rejected"])
        self.assertEqual(2, page.context["period_counts"]["breaks"])
        self.assertEqual(1, page.context["snapshot_summary"]["employee_count"])
        self.assertEqual(Decimal("8.00"), page.context["snapshot_summary"]["total_hours"])
        self.assertNotContains(page, "Secret Person")
        self.assertContains(page, "Details / resolve")
        self.assertContains(page, "Saved snapshot by employee")

    def test_attention_filter_catches_requests_without_duplicate_punches(self):
        for offset in (5, 10):
            PunchAdjustment.objects.create(organization=self.org, punch=self.punch, requested_by=self.worker,
                proposed_at=self.punch.occurred_at + timedelta(minutes=offset), reason="Verified time")
        page = self.page("&evidence=attention")
        self.assertEqual(2, page.context["pending_correction_count"])
        self.assertEqual(1, page.context["period_punch_page"].paginator.count)
        self.assertContains(page, "2 pending corrections")
        self.assertEqual(404, self.page("&evidence=bogus").status_code)
        self.assertEqual(404, self.page("&employee=bad-id").status_code)

    def test_evidence_paginates_without_losing_run_or_filter(self):
        for i in range(30):
            self.event("checkpoint", 12, exception_reason=f"Flag {i}")
        page = self.page("&evidence=attention")
        self.assertEqual(30, page.context["period_punch_page"].paginator.count)
        self.assertEqual(25, len(page.context["period_punch_page"].object_list))
        self.assertContains(page, "evidence_page=2")
        self.assertContains(page, f"run={self.run.pk}")
        self.assertContains(page, "evidence=attention")

    def test_modal_review_uses_existing_audit_and_marks_snapshot_stale(self):
        response = self.client.post(reverse("punch_review", args=[self.punch.pk]) + "?modal=1",
            {"action": "accepted", "reason": "Supervisor verified"})
        self.assertRedirects(response, reverse("punch_detail", args=[self.punch.pk]) + "?modal=1")
        self.run.refresh_from_db()
        self.assertTrue(any("regenerate" in row["reason"] for row in self.run.exceptions))
        self.assertTrue(AuditEvent.objects.filter(action="punch.reviewed", target_id=str(self.punch.pk)).exists())

    def test_modal_correction_review_keeps_original_and_returns_to_detail(self):
        item = PunchAdjustment.objects.create(organization=self.org, punch=self.punch, requested_by=self.worker,
            proposed_at=self.punch.occurred_at + timedelta(minutes=15), reason="Time verified")
        modal = self.client.get(reverse("punch_detail", args=[self.punch.pk]) + "?modal=1")
        self.assertContains(modal, "Approve correction")
        original = self.punch.occurred_at
        response = self.client.post(reverse("adjustment_review", args=[item.pk]) + "?modal=1",
            {"action": "approved", "note": "Verified"})
        self.assertRedirects(response, reverse("punch_detail", args=[self.punch.pk]) + "?modal=1")
        self.punch.refresh_from_db()
        self.assertEqual(original, self.punch.occurred_at)
        page = self.page()
        row = next(punch for punch in page.context["period_punch_page"] if punch.pk == self.punch.pk)
        self.assertEqual(item.proposed_at, row.effective_at)

    def test_modal_locked_period_and_officer_permission_are_enforced(self):
        self.run.status = PayrollRun.Status.APPROVED
        self.run.save()
        item = PunchAdjustment.objects.create(organization=self.org, punch=self.punch, requested_by=self.worker,
            proposed_at=self.punch.occurred_at + timedelta(minutes=15), reason="Time verified")
        for url, data in (
            (reverse("punch_review", args=[self.punch.pk]), {"action": "rejected", "reason": "Time disputed"}),
            (reverse("adjustment_review", args=[item.pk]), {"action": "approved"}),
        ):
            response = self.client.post(url + "?modal=1", data, follow=True)
            self.assertContains(response, "locked payroll period")
        self.punch.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual("accepted", self.punch.review_status)
        self.assertEqual("requested", item.status)
        self.client.force_login(self.worker)
        self.assertEqual(403, self.page().status_code)
        self.assertEqual(403, self.client.get(reverse("punch_detail", args=[self.punch.pk]) + "?modal=1").status_code)

    def test_refresh_exact_period_after_modal_correction(self):
        response = self.client.post(reverse("payroll") + f"?run={self.run.pk}", {
            "period_start": self.start.isoformat(), "period_end": self.end.isoformat(),
        })
        self.assertRedirects(response, reverse("payroll") + f"?run={self.run.pk}")
        self.assertEqual(1, PayrollRun.objects.filter(organization=self.org).count())

    def test_review_respects_locked_effective_time_outside_original_period(self):
        proposed = self.end + timedelta(hours=2)
        PunchAdjustment.objects.create(organization=self.org, punch=self.punch, requested_by=self.worker,
            proposed_at=proposed, reason="Verified on following day", status="approved",
            reviewed_by=self.owner, reviewed_at=timezone.now())
        PayrollRun.objects.create(organization=self.org, period_start=self.end, period_end=self.end + timedelta(days=1),
            created_by=self.owner, status=PayrollRun.Status.APPROVED)
        response = self.client.post(reverse("punch_review", args=[self.punch.pk]) + "?modal=1",
            {"action": "rejected", "reason": "Time disputed"}, follow=True)
        self.assertContains(response, "locked payroll period")
        self.punch.refresh_from_db()
        self.assertEqual("accepted", self.punch.review_status)

    def test_pdf_export_uses_snapshot_and_includes_period_and_approval(self):
        self.run.status = PayrollRun.Status.APPROVED
        self.run.approved_by = self.owner
        self.run.approved_at = timezone.now()
        self.run.save()
        response = self.client.get(reverse("payroll_run_export", args=[self.run.pk]) + "?format=pdf")
        self.assertEqual(200, response.status_code)
        text = pdf_streams(response.content)
        for value in (b"Review Payroll", str(self.run.pk).encode(), b"Alex Officer", b"Approved by:", b"8.00"):
            self.assertIn(value, text)
        self.run.refresh_from_db()
        self.assertEqual(PayrollRun.Status.EXPORTED, self.run.status)


class PayrollPdfTest(TestCase):
    def row(self, employee="Alex Officer", **kwargs):
        return {"employee_id": employee, "employee": employee, "client": "Client", "site": "Gate",
                "pay_category": "regular", "pay_code": "GUARD", "total_hours": "8.00",
                "regular_hours": "8.00", "overtime_hours": "0.00", "pay_rate": "20.00", "bill_rate": "30.00",
                "estimated_pay": "160.00", "estimated_bill": "240.00", "margin": "80.00", **kwargs}

    def test_multi_page_report_repeats_headers_and_keeps_final_row_and_totals(self):
        rows = [self.row(f"Officer {i}") for i in range(130)]
        rows[-1]["employee"] = "LASTPAYROLLROW123"
        document = payroll_snapshot_pdf(rows)
        page_count = len(re.findall(rb"/Type /Page\b", document))
        self.assertGreater(page_count, 3)
        text = pdf_streams(document)
        self.assertIn(b"LASTPAYROLLROW123", text)
        self.assertIn(b"20,800.00", text)
        self.assertEqual(page_count, text.count(b"(Est. pay)"))
        self.assertEqual(page_count, text.count(b"(TSCM | Saved payroll snapshot | Confidential)"))

    def test_long_text_is_wrapped_not_truncated_and_markup_is_escaped(self):
        row = self.row(employee="Alex <Officer> & Co.", site="Very long location " * 30 + "ENDLOCATIONMARKER",
                       exception="Note " * 100 + "ENDNOTEMARKER")
        text = pdf_streams(payroll_snapshot_pdf([row]))
        self.assertIn(b"ENDLOCATIONMARKER", text)
        self.assertIn(b"ENDNOTEMARKER", text)
        self.assertIn(b"(Alex <)", text)
        self.assertIn(b"(Officer)", text)
        self.assertIn(b"(> & Co.)", text)

    def test_empty_report_and_unset_amounts_are_explicit(self):
        self.assertIn(b"No payroll lines", pdf_streams(payroll_snapshot_pdf([])))
        rows = [self.row(estimated_pay="", margin=""), self.row("Other")]
        summary = payroll_snapshot_summary(rows)
        self.assertEqual(Decimal("160.00"), summary["estimated_pay"])
        self.assertEqual(1, summary["missing"]["estimated_pay"])
        self.assertIn(b"Not set", pdf_streams(payroll_snapshot_pdf(rows)))

    def test_employee_totals_use_ids_not_duplicate_display_names(self):
        rows = [self.row("Same name", employee_id="one"), self.row("Same name", employee_id="two"),
                self.row("Same name", employee_id="one")]
        summary = payroll_snapshot_summary(rows)
        self.assertEqual(2, summary["employee_count"])
        self.assertEqual([1, 2], sorted(item["rows"] for item in summary["employees"]))

    def test_single_oversized_row_splits_across_pages_without_losing_its_end(self):
        document = payroll_snapshot_pdf([self.row(site="Long location detail " * 300 + "ENDOVERSIZEDROW")])
        self.assertGreater(len(re.findall(rb"/Type /Page\b", document)), 1)
        self.assertIn(b"ENDOVERSIZEDROW", pdf_streams(document))
