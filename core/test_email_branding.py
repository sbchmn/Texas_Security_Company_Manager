import base64
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock, patch

from django.core.files.base import ContentFile
from django.core.files.storage import InMemoryStorage
from django.test import TestCase, override_settings

from .models import Notification, Organization
from .services import branded_email_html, deliver_notification


class NoURLStorage(InMemoryStorage):
    def url(self, name):
        raise AssertionError("Email must not generate an expiring storage URL")


@override_settings(
    POSTMARK_SERVER_TOKEN="test-token",
    MAILJET_API_KEY="test-key",
    MAILJET_SECRET_KEY="test-secret",
)
class EmailBrandingTest(TestCase):
    def setUp(self):
        storage_patch = patch.object(Organization._meta.get_field("logo"), "storage", NoURLStorage())
        storage_patch.start()
        self.addCleanup(storage_patch.stop)
        self.org = Organization.objects.create(
            legal_name="Email LLC", display_name="Email & Company", slug="email-brand",
            email_from="alerts@example.com",
        )
        self.content = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "/x8AAwMCAO+aXioAAAAASUVORK5CYII="
        )
        self.org.logo.save("brand.png", ContentFile(self.content))

    def notice(self, provider):
        self.org.email_provider = provider
        return Notification.objects.create(
            organization=self.org, channel=Notification.Channel.EMAIL,
            destination="recipient@example.com", event_type="test",
            subject="Notice <test>", body="First & second\nAnother line",
        )

    def test_http_providers_embed_logo_bytes_and_matching_cid(self):
        for provider in (Organization.EmailProvider.MAILJET, Organization.EmailProvider.POSTMARK):
            with self.subTest(provider=provider):
                item = self.notice(provider)
                with patch("requests.post", return_value=Mock()) as post:
                    deliver_notification(item)
                self.assertEqual(item.status, Notification.Status.SENT, item.last_error)
                payload = post.call_args.kwargs["json"]
                if provider == Organization.EmailProvider.MAILJET:
                    message = payload["Messages"][0]
                    image = message["InlinedAttachments"][0]
                    content = image["Base64Content"]
                    cid = image["ContentID"]
                    html = message["HTMLPart"]
                    self.assertEqual(message["TextPart"], item.body)
                else:
                    image = payload["Attachments"][0]
                    content = image["Content"]
                    cid = image["ContentID"].removeprefix("cid:")
                    html = payload["HtmlBody"]
                    self.assertEqual(payload["TextBody"], item.body)
                self.assertEqual(base64.b64decode(content), self.content)
                self.assertEqual(image["ContentType"], "image/png")
                self.assertIn(f'src="cid:{cid}"', html)
                self.assertIn("Email &amp; Company", html)
                self.assertIn("Notice &lt;test&gt;", html)
                self.assertNotIn("https://", html)

    def test_ses_embeds_inline_logo_in_related_mime_part(self):
        item = self.notice(Organization.EmailProvider.SES)
        with patch("boto3.client") as client:
            deliver_notification(item)
        self.assertEqual(item.status, Notification.Status.SENT, item.last_error)
        ses = client.return_value
        ses.send_email.assert_not_called()
        arguments = ses.send_raw_email.call_args.kwargs
        message = BytesParser(policy=policy.default).parsebytes(arguments["RawMessage"]["Data"])
        self.assertEqual(arguments["Destinations"], ["recipient@example.com"])
        self.assertEqual(str(message["Subject"]), item.subject)
        self.assertEqual(message.get_content_type(), "multipart/alternative")
        image = next(part for part in message.walk() if part.get_content_maintype() == "image")
        self.assertEqual(image.get_content_disposition(), "inline")
        self.assertEqual(image.get_payload(decode=True), self.content)
        cid = image["Content-ID"].strip("<>")
        self.assertIn(f'src="cid:{cid}"', message.get_body(preferencelist=("html",)).get_content())
        self.assertEqual(message.get_body(preferencelist=("plain",)).get_content().splitlines(), item.body.splitlines())

    def test_no_logo_keeps_all_providers_attachment_free(self):
        self.org.logo = ""
        for provider in Organization.EmailProvider.values:
            with self.subTest(provider=provider):
                item = self.notice(provider)
                with patch("requests.post", return_value=Mock()) as post, patch("boto3.client") as client:
                    deliver_notification(item)
                self.assertEqual(item.status, Notification.Status.SENT, item.last_error)
                if provider == Organization.EmailProvider.SES:
                    client.return_value.send_raw_email.assert_not_called()
                    html = client.return_value.send_email.call_args.kwargs["Message"]["Body"]["Html"]["Data"]
                else:
                    payload = post.call_args.kwargs["json"]
                    message = payload["Messages"][0] if provider == Organization.EmailProvider.MAILJET else payload
                    self.assertNotIn("InlinedAttachments", message)
                    self.assertNotIn("Attachments", message)
                    html = message.get("HTMLPart", message.get("HtmlBody"))
                self.assertNotIn("<img", html)

    def test_missing_logo_is_reported_as_failed_delivery(self):
        item = self.notice(Organization.EmailProvider.POSTMARK)
        with patch.object(self.org.logo.storage, "open", side_effect=FileNotFoundError("Logo missing")), patch("requests.post") as post:
            deliver_notification(item)
        self.assertEqual(item.status, Notification.Status.FAILED)
        self.assertIn("Logo missing", item.last_error)
        post.assert_not_called()

    def test_layout_without_attachment_has_no_broken_logo_reference(self):
        self.assertNotIn("<img", branded_email_html(self.notice(Organization.EmailProvider.POSTMARK)))
