from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from . import sms
from .models import (
    AuditEvent, Client, Membership, Notification, Organization, Person, Shift, Site, SmsTemplate,
)
from .services import queue_notice

CHICAGO = ZoneInfo("America/Chicago")


class SmsSegmentTest(TestCase):
    def test_gsm_text_fits_160_per_segment_and_153_after(self):
        self.assertEqual(1, sms.segment_info("a" * 160)["segments"])
        info = sms.segment_info("a" * 161)
        self.assertEqual((2, "GSM-7", 306), (info["segments"], info["encoding"], info["limit"]))

    def test_extended_characters_count_twice_and_others_switch_to_unicode(self):
        self.assertEqual(2, sms.segment_info("\u20ac")["characters"])
        self.assertEqual("Unicode", sms.segment_info("a" * 71 + "\u4e2d")["encoding"])
        self.assertEqual(2, sms.segment_info("a" * 71 + "\u4e2d")["segments"])

    def test_curly_quotes_and_dashes_are_straightened(self):
        self.assertEqual('Hi - "there".', sms.tidy("Hi \u2014 \u201cthere\u201d .."))


class SmsTemplateTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.org = Organization.objects.create(legal_name="Acme Security LLC", display_name="Acme Security",
                                               slug="acme-sms", timezone="America/Chicago")
        self.owner = User.objects.create_user(username="sms-owner@example.com", first_name="Pat")
        self.officer_user = User.objects.create_user(username="sms-officer@example.com", first_name="Jordan")
        self.other_user = User.objects.create_user(username="sms-other@example.com", first_name="Alex")
        Membership.objects.create(user=self.owner, organization=self.org, role=Membership.Role.OWNER)
        Membership.objects.create(user=self.officer_user, organization=self.org, role=Membership.Role.OFFICER)
        Membership.objects.create(user=self.other_user, organization=self.org, role=Membership.Role.OFFICER)
        self.person = Person.objects.create(organization=self.org, user=self.officer_user, first_name="Jordan",
                                            last_name="Lee", status=Person.Status.ACTIVE)
        self.other = Person.objects.create(organization=self.org, user=self.other_user, first_name="Alex",
                                           last_name="Kim", status=Person.Status.ACTIVE)
        client = Client.objects.create(organization=self.org, name="Northpark Holdings")
        self.site = Site.objects.create(organization=self.org, client=client, name="Northpark Center", address="Dallas")
        starts = datetime(2026, 10, 9, 18, 0, tzinfo=CHICAGO)
        self.shift = Shift.objects.create(organization=self.org, site=self.site, officer=self.person,
                                          starts_at=starts, ends_at=starts + timedelta(hours=8))

    @override_settings(PUBLIC_BASE_URL="")
    def test_default_wording_uses_local_time_and_the_company_prefix(self):
        text = sms.render_sms(self.org, "shift.published", {"shift": self.shift})
        self.assertEqual("Acme Security: You're scheduled at Northpark Center Fri Oct 9, 6 PM-2 AM. Reply STOP to opt out.", text)

    @override_settings(PUBLIC_BASE_URL="https://tscm.example.com/")
    def test_link_uses_public_base_url(self):
        text = sms.render_sms(self.org, "shift.published", {"shift": self.shift})
        self.assertRegex(text, r"\.\s+https://tscm\.example\.com/[^/]")

    @override_settings(PUBLIC_BASE_URL="")
    def test_company_wording_replaces_the_default_until_it_is_reset(self):
        SmsTemplate.objects.create(organization=self.org, notice_key="shift.published",
                                   body="Hi {first_name}, new shift: {site}, {when}.")
        text = sms.render_sms(self.org, "shift.published", {"shift": self.shift}, first_name="Jordan")
        self.assertEqual("Acme Security: Hi Jordan, new shift: Northpark Center, Fri Oct 9, 6 PM-2 AM. Reply STOP to opt out.", text)
        SmsTemplate.objects.all().delete()
        self.assertIn("You're scheduled", sms.render_sms(self.org, "shift.published", {"shift": self.shift}))

    def test_validation_names_unknown_placeholders_and_unmatched_braces(self):
        notice = sms.notice_for("shift.published")
        self.assertEqual([], sms.validate_template(notice, "Shift at {site} {when}."))
        self.assertIn("{officer}", " ".join(sms.validate_template(notice, "{officer} works {when}")))
        self.assertIn("unmatched", " ".join(sms.validate_template(notice, "Shift at {site")).lower())
        self.assertTrue(sms.validate_template(notice, "   "))

    @override_settings(PUBLIC_BASE_URL="")
    def test_the_text_row_carries_the_short_wording_and_email_keeps_the_long_body(self):
        queue_notice(organization=self.org, recipients={self.officer_user.pk}, event_type="shift.published",
                     subject="Shift published", body="A long email paragraph about the shift.",
                     dedup_key="sms-test", channels=(Notification.Channel.EMAIL, Notification.Channel.SMS),
                     sms={"shift": self.shift})
        rows = dict(Notification.objects.filter(organization=self.org).values_list("channel", "body"))
        self.assertEqual("A long email paragraph about the shift.", rows["email"])
        self.assertEqual("Acme Security: You're scheduled at Northpark Center Fri Oct 9, 6 PM-2 AM. Reply STOP to opt out.", rows["sms"])

    @override_settings(PUBLIC_BASE_URL="")
    def test_first_name_is_filled_per_recipient(self):
        SmsTemplate.objects.create(organization=self.org, notice_key="shift.published", body="Hi {first_name}: {site}.")
        queue_notice(organization=self.org, recipients={self.officer_user.pk, self.other_user.pk},
                     event_type="shift.published", subject="s", body="b", dedup_key="names",
                     channels=(Notification.Channel.SMS,), sms={"shift": self.shift})
        bodies = set(Notification.objects.filter(channel="sms").values_list("body", flat=True))
        self.assertEqual({"Acme Security: Hi Jordan: Northpark Center. Reply STOP to opt out.", "Acme Security: Hi Alex: Northpark Center. Reply STOP to opt out."},
                         bodies)

    @override_settings(PUBLIC_BASE_URL="")
    def test_an_explicit_notice_key_picks_the_audience_specific_wording(self):
        text = sms.render_sms(self.org, sms.resolve_key("shift.exchange_approved",
                              {"notice": "shift.exchange_approved", "other": self.other}),
                              {"notice": "shift.exchange_approved", "other": self.other})
        self.assertEqual("Acme Security: Your shift trade with Alex Kim was approved. Reply STOP to opt out.", text)

    def test_only_privileged_members_can_open_the_wording_pages(self):
        self.client.force_login(self.officer_user)
        self.assertEqual(403, self.client.get(reverse("sms_templates")).status_code)
        self.assertEqual(403, self.client.get(reverse("sms_template_edit", args=["shift.published"])).status_code)

    def test_owner_sees_every_notice_and_an_unknown_key_is_not_found(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("sms_templates"))
        self.assertEqual(200, response.status_code)
        self.assertContains(response, "Acme Security: You&#x27;re scheduled at Northpark Center")
        self.assertContains(response, reverse("sms_template_edit", args=["credential.reminder"]))
        self.assertEqual(404, self.client.get(reverse("sms_template_edit", args=["nope.nothing"])).status_code)

    def test_saving_and_resetting_wording_is_audited(self):
        self.client.force_login(self.owner)
        url = reverse("sms_template_edit", args=["shift.published"])
        response = self.client.post(url, {"action": "save", "body": "New shift: {site} {when}."})
        self.assertRedirects(response, reverse("sms_templates"), fetch_redirect_response=False)
        self.assertEqual("New shift: {site} {when}.",
                         SmsTemplate.objects.get(organization=self.org, notice_key="shift.published").body)
        self.assertTrue(AuditEvent.objects.filter(organization=self.org, action="message.sms_template_updated",
                                                  target_id="shift.published").exists())
        self.client.post(url, {"action": "reset"})
        self.assertFalse(SmsTemplate.objects.filter(organization=self.org).exists())
        self.assertTrue(AuditEvent.objects.filter(organization=self.org, action="message.sms_template_reset").exists())

    def test_invalid_wording_is_refused_and_redisplayed(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("sms_template_edit", args=["shift.published"]),
                                    {"action": "save", "body": "{officer} works {when}"})
        self.assertEqual(200, response.status_code)
        self.assertContains(response, "Not available for this notice")
        self.assertFalse(SmsTemplate.objects.exists())

    def test_saving_the_built_in_wording_keeps_no_override(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("sms_template_edit", args=["shift.published"]),
                         {"action": "save", "body": sms.notice_for("shift.published").default})
        self.assertFalse(SmsTemplate.objects.exists())
