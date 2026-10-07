from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from . import email_wording, sms, test_sms
from .models import AuditEvent, EmailTemplate, Membership, Notification, Organization, SmsTemplate
from .services import queue_notice


@override_settings(PUBLIC_BASE_URL="https://example.com")
class EmailWordingTest(TestCase):
    setUp = test_sms.SmsTemplateTest.setUp

    def template(self, key="shift.published", subject="{company}: {subject}", body="Hi {first_name}, {site} {when}.\n{body}"):
        return EmailTemplate.objects.create(organization=self.org, notice_key=key,
                                            subject=subject, body=body)

    def queue(self, **kwargs):
        return queue_notice(organization=self.org,
            recipients={self.officer_user.pk, self.other_user.pk},
            event_type="shift.published", subject="Assigned", body="Original detailed message.",
            dedup_key="wording-test", channels=("in_app", "email", "sms"),
            sms=kwargs.pop("sms", {"shift": self.shift}), **kwargs)

    def test_email_override_is_personalized_and_does_not_change_sms_or_in_app(self):
        self.template()
        SmsTemplate.objects.create(organization=self.org, notice_key="shift.published", body="Shift: {site}.")
        self.assertEqual(self.queue(mandatory=True), 6)
        emails = Notification.objects.filter(channel="email")
        self.assertEqual(set(emails.values_list("subject", flat=True)), {"Acme Security: Assigned"})
        self.assertEqual(set(emails.values_list("body", flat=True)), {
            "Hi Jordan, Northpark Center Fri Oct 9, 6 PM-2 AM.\nOriginal detailed message.",
            "Hi Alex, Northpark Center Fri Oct 9, 6 PM-2 AM.\nOriginal detailed message.",
        })
        self.assertEqual(set(Notification.objects.filter(channel="in_app").values_list("body", flat=True)),
                         {"Original detailed message."})
        self.assertEqual(set(Notification.objects.filter(channel="sms").values_list("body", flat=True)),
                         {"Acme Security: Shift: Northpark Center."})
        self.assertTrue(all(emails.values_list("mandatory", flat=True)))

    def test_deduplication_and_queued_snapshot_survive_later_template_edits(self):
        row = self.template(body="First wording at {site}.")
        self.queue()
        row.body = "Later wording."
        row.save()
        self.assertEqual(self.queue(), 0)
        self.assertEqual(set(Notification.objects.filter(channel="email").values_list("body", flat=True)),
                         {"First wording at Northpark Center."})

    def test_audience_specific_variant_does_not_use_the_base_template(self):
        self.template(key="shift.published", body="Base")
        self.template(key="shift.published.series", body="Added {count} shifts at {site}.")
        self.queue(sms={"notice": "shift.published.series", "site": self.site, "count": 3})
        self.assertEqual(set(Notification.objects.filter(channel="email").values_list("body", flat=True)),
                         {"Added 3 shifts at Northpark Center."})

    def test_unconfigured_and_unknown_notifications_keep_original_content(self):
        self.queue()
        self.assertEqual(set(Notification.objects.filter(channel="email").values_list("body", flat=True)),
                         {"Original detailed message."})
        self.assertEqual(email_wording.render_email(self.org, "unknown.future", "Title", "Body"), ("Title", "Body"))

    def test_overrides_are_tenant_bound(self):
        other = Organization.objects.create(legal_name="Other", slug="other-wording")
        EmailTemplate.objects.create(organization=other, notice_key="shift.published",
                                     subject="Other company", body="Other body")
        self.queue()
        self.assertEqual(Notification.objects.filter(channel="email").first().subject, "Assigned")
        self.client.force_login(self.owner)
        self.client.post(reverse("email_template_edit", args=["shift.published"]),
                         {"subject": "Our wording", "body": "Our body", "action": "save"})
        self.assertEqual(other.email_templates.get().body, "Other body")

    def test_validation_rejects_bad_placeholders_empty_text_headers_and_missing_action_links(self):
        normal = email_wording.notice_for("shift.published")
        for subject, body in (("", "Body"), ("Title", ""), ("Title\nBcc: x", "Body"),
                              ("{officer}", "Body"), ("Title", "{unknown}"), ("Title", "{site"),
                              ("x" * 201, "Body"), ("Title", "x" * 10001)):
            with self.subTest(subject=subject, body=body):
                self.assertTrue(email_wording.validate_wording(normal, subject, body))
        for key in ("membership.invitation", "membership.invitation.person", "onboarding.signature_requested"):
            notice = email_wording.notice_for(key)
            self.assertTrue(email_wording.validate_wording(notice, "Title", "No action link"))
            self.assertEqual(email_wording.validate_wording(notice, "Title", "{body}"), [])
            self.assertEqual(email_wording.validate_wording(notice, "Title", "Act here: {link}"), [])

    def test_runtime_header_and_missing_link_checks_fail_explicitly(self):
        self.template(subject="{company}")
        self.org.display_name = "Bad\nHeader"
        with self.assertRaisesMessage(ValidationError, "single line"):
            self.queue()
        row = self.template(key="membership.invitation", subject="Welcome", body="{link}")
        with self.assertRaisesMessage(ValidationError, "required invitation"):
            email_wording.render_email(self.org, row.notice_key, "Title", "Original", template=row)

    def test_owner_catalog_has_ids_channels_and_search_filters(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("sms_templates"))
        self.assertContains(page, "Notification wording")
        self.assertContains(page, "membership.invitation.person")
        self.assertContains(page, reverse("email_template_edit", args=["shift.published"]))
        self.assertContains(page, reverse("sms_template_edit", args=["shift.published"]))
        filtered = self.client.get(reverse("sms_templates"), {"q": "membership.invitation", "channel": "sms"})
        self.assertEqual(len(filtered.context["catalog_rows"]), 0)
        self.template()
        filtered = self.client.get(reverse("sms_templates"), {"channel": "email", "status": "custom"})
        self.assertEqual([row["notice"].key for row in filtered.context["catalog_rows"]], ["shift.published"])
        self.assertEqual(self.client.get("/settings/messaging/texts/").status_code, 200)

    def test_email_editor_save_preview_reset_and_audit(self):
        self.client.force_login(self.owner)
        url = reverse("email_template_edit", args=["shift.published"])
        page = self.client.get(url)
        self.assertContains(page, "Message catalog")
        self.assertContains(page, 'data-email-editor')
        self.assertContains(page, "Email subject")
        values = {"subject": "Hello {first_name}", "body": "Your post: {site}.\n{body}"}
        preview = self.client.post(url, {**values, "action": "preview"})
        self.assertContains(preview, "Hello Jordan")
        self.assertFalse(EmailTemplate.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action="message.email_template_updated").exists())
        response = self.client.post(url, {**values, "action": "save"})
        self.assertRedirects(response, reverse("sms_templates"), fetch_redirect_response=False)
        self.assertEqual(self.org.email_templates.get().body, values["body"])
        self.assertTrue(AuditEvent.objects.filter(action="message.email_template_updated").exists())
        self.client.post(url, {"action": "reset"})
        self.assertFalse(self.org.email_templates.exists())
        self.assertTrue(AuditEvent.objects.filter(action="message.email_template_reset").exists())
        self.assertEqual(email_wording.render_email(self.org, "shift.published", "Original", "Original body"),
                         ("Original", "Original body"))

    def test_invalid_editor_submission_retains_values_and_does_not_save(self):
        self.client.force_login(self.owner)
        page = self.client.post(reverse("email_template_edit", args=["shift.published"]),
                                {"subject": "Keep this", "body": "Hi {typo}"})
        self.assertContains(page, "Keep this")
        self.assertContains(page, "{typo}")
        self.assertContains(page, "unavailable placeholders")
        self.assertFalse(EmailTemplate.objects.exists())

    def test_permissions_and_unknown_ids_fail_closed(self):
        self.client.force_login(self.officer_user)
        url = reverse("email_template_edit", args=["shift.published"])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"subject": "x", "body": "y"}).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("email_template_edit", args=["unknown.event"])).status_code, 404)

    def test_sms_preview_does_not_save(self):
        self.client.force_login(self.owner)
        page = self.client.post(reverse("sms_template_edit", args=["shift.published"]),
                                {"action": "preview", "body": "New shift at {site}"})
        self.assertContains(page, "New shift at Northpark Center")
        self.assertFalse(SmsTemplate.objects.exists())

    def test_all_catalog_defaults_validate(self):
        self.assertTrue(set(sms.SMS_NOTICES).issubset(email_wording.EMAIL_NOTICES))
        for notice in email_wording.EMAIL_NOTICES.values():
            self.assertEqual(email_wording.validate_wording(notice, "{subject}", "{body}"), [], notice.key)

    def test_team_and_personnel_invites_use_their_own_wording_and_real_token(self):
        self.client.force_login(self.owner)
        self.template(key="membership.invitation", subject="Team welcome",
                      body="Join {company} as {role}: {link}")
        self.client.post(reverse("team"), {"email": "new@example.com", "role": Membership.Role.ADMIN})
        notice = Notification.objects.get(destination="new@example.com")
        self.assertEqual(notice.subject, "Team welcome")
        self.assertIn("http://testserver/invitations/", notice.body)
        self.assertNotIn("{link}", notice.body)
        self.person.email = "person@example.com"
        self.person.user = None
        self.person.save()
        self.template(key="membership.invitation.person", subject="Personnel welcome",
                      body="Hi {first_name}, activate: {link}")
        self.client.post(reverse("person_access_invite", args=[self.person.pk]),
                         {"role": Membership.Role.OFFICER})
        notice = Notification.objects.get(destination="person@example.com")
        self.assertEqual(notice.subject, "Personnel welcome")
        self.assertIn("Hi Jordan, activate: http://testserver/invitations/", notice.body)
