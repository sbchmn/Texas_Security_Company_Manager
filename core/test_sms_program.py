from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from .forms import InvitationAcceptanceForm, SmsProgramForm
from .models import AuditEvent, Membership, MessageConsent, Notification, Organization, Person, Suppression
from .services import (
    current_consent, deliver_notification, handle_inbound_message, record_consent,
    sms_consent_wording, sms_enrollment_message, sms_opted_in,
)
from .sms import SMS_NOTICES, preview_sms, render_sms


class SmsProgramTest(TestCase):
    PHONE = "+12145550142"

    def setUp(self):
        self.org = Organization.objects.create(legal_name="Example Security LLC",
            display_name="Example Security", slug="sms-program")
        self.owner = get_user_model().objects.create_user(username="sms-program-owner")
        self.worker = get_user_model().objects.create_user(username="sms-program-worker")
        Membership.objects.create(organization=self.org, user=self.owner, role=Membership.Role.OWNER)
        Membership.objects.create(organization=self.org, user=self.worker, role=Membership.Role.OFFICER)
        self.person = Person.objects.create(organization=self.org, user=self.worker,
            first_name="Example", last_name="Officer", mobile_phone=self.PHONE)
        self.values = {"sms_terms_url": "https://example.com/sms-terms",
            "sms_privacy_url": "https://example.com/privacy", "support_email": "support@example.com"}

    def configure(self):
        for name, value in self.values.items():
            setattr(self.org, name, value)
        self.org.save()

    def grant(self):
        return record_consent(organization=self.org, person=self.person, destination=self.PHONE,
            state=MessageConsent.State.GRANTED)

    def confirmations(self):
        return Notification.objects.filter(event_type="message.sms_enrollment")

    def test_unconfigured_company_blocks_ui_and_service_grants_but_not_stop(self):
        self.client.force_login(self.worker)
        page = self.client.get(reverse("text_alerts"))
        self.assertContains(page, "New text-alert opt-ins are unavailable")
        self.assertTrue(page.context["form"].fields["opt_in"].disabled)
        self.assertIsNone(self.client.get(reverse("dashboard")).context["text_prompt"])
        with self.assertRaisesMessage(ValidationError, "before new opt-ins"):
            self.grant()
        response = self.client.post(reverse("text_alerts"),
            {"mobile_phone": self.PHONE, "opt_in": "on", "choice": "yes"})
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.context["form"].errors)
        self.assertFalse(MessageConsent.objects.exists())
        reply = handle_inbound_message(self.org, self.PHONE, "STOP")
        self.assertIn("will not receive more texts from Example Security", reply)
        self.assertEqual(MessageConsent.State.REVOKED, current_consent(self.org, self.PHONE).state)
        self.assertEqual(Suppression.Kind.UNSUBSCRIBE, Suppression.objects.get().kind)

    def test_owner_can_configure_and_audit_program_but_officer_cannot(self):
        self.client.force_login(self.worker)
        self.assertEqual(403, self.client.post(reverse("messaging_settings"), self.values).status_code)
        self.client.force_login(self.owner)
        self.assertRedirects(self.client.post(reverse("messaging_settings"), self.values),
            reverse("messaging_settings"))
        self.org.refresh_from_db()
        self.assertTrue(self.org.sms_program_ready)
        self.assertTrue(AuditEvent.objects.filter(action="message.sms_program_updated").exists())

    def test_invalid_or_missing_settings_do_not_enable_program(self):
        for field in self.values:
            values = dict(self.values)
            values[field] = ""
            self.assertFalse(SmsProgramForm(values, instance=self.org).is_valid())
        for field in ("sms_terms_url", "sms_privacy_url"):
            values = dict(self.values)
            values[field] = "http://example.com/policy"
            form = SmsProgramForm(values, instance=self.org)
            self.assertFalse(form.is_valid())
            self.assertIn(field, form.errors)
        self.org.sms_terms_url = "https://example.com/terms"
        self.org.sms_privacy_url = "https://example.com/privacy"
        self.org.support_email = "not an email"
        self.assertFalse(self.org.sms_program_ready)

    def test_disclosures_match_saved_wording_and_checkbox_is_never_prechecked(self):
        self.configure()
        self.client.force_login(self.worker)
        page = self.client.get(reverse("text_alerts"))
        self.assertFalse(page.context["form"]["opt_in"].value())
        for value in self.values.values():
            self.assertContains(page, value)
        self.assertContains(page, "Message frequency varies")
        self.assertContains(page, "payroll, credentials")
        self.client.post(reverse("text_alerts"),
            {"mobile_phone": self.PHONE, "opt_in": "on", "choice": "yes"})
        row = current_consent(self.org, self.PHONE)
        self.assertEqual(sms_consent_wording(self.org), row.wording)
        self.assertEqual("text_alerts", row.evidence["surface"])
        page = self.client.get(reverse("text_alerts"))
        self.assertFalse(page.context["form"]["opt_in"].value())
        self.assertContains(page, "Text alerts are currently on")
        self.assertEqual(1, self.confirmations().count())

    def test_unchecked_save_preserves_consent_and_explicit_off_works_even_with_checked_box(self):
        self.configure()
        self.grant()
        self.client.force_login(self.worker)
        self.client.post(reverse("text_alerts"), {"mobile_phone": self.PHONE, "choice": "yes"})
        self.assertTrue(sms_opted_in(self.org, self.PHONE))
        self.client.post(reverse("text_alerts"),
            {"mobile_phone": self.PHONE, "opt_in": "on", "choice": "off"})
        self.assertFalse(sms_opted_in(self.org, self.PHONE))

    def test_no_button_cannot_grant_consent_when_checkbox_is_checked(self):
        self.configure()
        self.client.force_login(self.worker)
        self.client.post(reverse("text_alerts"),
            {"mobile_phone": self.PHONE, "opt_in": "on", "choice": "no"})
        self.assertFalse(sms_opted_in(self.org, self.PHONE))
        self.assertEqual(0, self.confirmations().count())

    def test_first_login_prompt_has_unchecked_consent_and_policy_links(self):
        self.configure()
        self.client.force_login(self.worker)
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, '<input type="checkbox" name="opt_in">', html=True)
        self.assertContains(page, self.values["sms_terms_url"])
        self.assertContains(page, self.values["sms_privacy_url"])
        self.client.post(reverse("text_alerts"),
            {"mobile_phone": self.PHONE, "opt_in": "on", "choice": "yes"})
        self.assertTrue(sms_opted_in(self.org, self.PHONE))
        self.assertEqual(1, self.confirmations().count())
        self.assertIsNone(self.client.get(reverse("dashboard")).context["text_prompt"])

    def test_invitation_form_uses_same_disclosures_and_disables_unconfigured_grants(self):
        form = InvitationAcceptanceForm(organization=self.org)
        self.assertTrue(form.fields["text_alerts"].disabled)
        self.configure()
        form = InvitationAcceptanceForm(organization=self.org)
        self.assertFalse(form.fields["text_alerts"].disabled)
        self.assertFalse(form["text_alerts"].value())
        self.assertIn(self.values["sms_terms_url"], form.fields["text_alerts"].help_text)
        self.assertIn(sms_consent_wording(self.org), form.fields["text_alerts"].help_text)

    def test_confirmation_only_queued_for_new_grant_and_regrant_not_repeated_save(self):
        self.configure()
        row = self.grant()
        self.grant()
        message = self.confirmations().get()
        self.assertEqual(self.PHONE, message.destination)
        self.assertEqual(self.worker, message.recipient)
        self.assertEqual(sms_enrollment_message(self.org), message.body)
        self.assertEqual(f"sms-enrollment:{row.pk}", message.deduplication_key)
        handle_inbound_message(self.org, self.PHONE, "STOP")
        self.grant()
        self.assertEqual(2, self.confirmations().count())

    def test_stopped_confirmation_never_reaches_provider(self):
        self.configure()
        self.grant()
        handle_inbound_message(self.org, self.PHONE, "STOP")
        with patch("requests.post") as provider:
            result = deliver_notification(self.confirmations().get())
        provider.assert_not_called()
        self.assertEqual(Notification.Status.BLOCKED, result.status)
        self.assertEqual(0, result.attempts)

    def test_keyword_start_is_prior_consent_only_and_returns_complete_reply_without_duplicate_sms(self):
        self.configure()
        reply = handle_inbound_message(self.org, self.PHONE, "START")
        self.assertIn("no record", reply)
        self.assertFalse(MessageConsent.objects.exists())
        self.grant()
        handle_inbound_message(self.org, self.PHONE, "STOP")
        reply = handle_inbound_message(self.org, self.PHONE, "START")
        for value in ("Example Security", "frequency varies", "rates may apply", "HELP", "STOP", "support@example.com"):
            self.assertIn(value, reply)
        self.assertTrue(sms_opted_in(self.org, self.PHONE))
        self.assertEqual(1, self.confirmations().count())
        self.org.sms_terms_url = ""
        self.org.save()
        reply = handle_inbound_message(self.org, self.PHONE, "START")
        self.assertIn("unavailable", reply)

    def test_help_has_brand_support_frequency_and_rates(self):
        self.configure()
        reply = handle_inbound_message(self.org, self.PHONE, "HELP")
        for value in ("Example Security", "support@example.com", "frequency varies", "rates may apply", "STOP"):
            self.assertIn(value, reply)
        self.assertFalse(MessageConsent.objects.exists())
        self.assertLessEqual(len(reply), 1024)

    @override_settings(PUBLIC_BASE_URL="https://example.com")
    def test_all_default_and_custom_sms_previews_and_renderings_include_opt_out(self):
        for key, notice in SMS_NOTICES.items():
            with self.subTest(key=key):
                preview = preview_sms(self.org, notice)
                self.assertTrue(preview.startswith("Example Security: "))
                self.assertTrue(preview.endswith("Reply STOP to opt out."))
                self.assertLessEqual(len(preview), 1024)
                self.assertTrue(render_sms(self.org, key, template="Custom notice.").endswith("Reply STOP to opt out."))

    def test_consent_and_confirmation_roll_back_together(self):
        self.configure()
        with patch.object(Notification.objects, "create", side_effect=RuntimeError("queue unavailable")):
            with self.assertRaisesMessage(RuntimeError, "queue unavailable"):
                self.grant()
        self.assertFalse(MessageConsent.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action="message.consent_granted").exists())
