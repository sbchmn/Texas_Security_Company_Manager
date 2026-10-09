import io
import logging
import re
from pathlib import Path

from django.test import SimpleTestCase, override_settings
from django.urls import path


def failing_view(request):
    raise RuntimeError("upload diagnostic test failure")


urlpatterns = [path("logging-test-failure/", failing_view)]


@override_settings(DEBUG=False, ROOT_URLCONF=__name__, MIDDLEWARE=[], ADMINS=[])
class ProductionLoggingTest(SimpleTestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.console = next(
            handler for handler in logging.getLogger("django").handlers
            if handler.name == "console"
        )
        self.original_stream = self.console.setStream(self.output)
        self.addCleanup(self.console.setStream, self.original_stream)

    def test_unhandled_request_error_logs_traceback_without_debug_page(self):
        self.client.raise_request_exception = False
        response = self.client.get("/logging-test-failure/")

        self.assertEqual(response.status_code, 500)
        self.assertNotContains(response, "upload diagnostic test failure", status_code=500)
        output = self.output.getvalue()
        self.assertIn("ERROR django.request: Internal Server Error:", output)
        self.assertIn("Traceback (most recent call last)", output)
        self.assertIn("RuntimeError: upload diagnostic test failure", output)
        self.assertEqual(output.count("Internal Server Error:"), 1)

    def test_application_errors_reach_console_without_debug_noise(self):
        logger = logging.getLogger("core.upload_diagnostic_test")
        logger.error("application error diagnostic")
        logger.debug("debug diagnostic must not be logged")

        output = self.output.getvalue()
        self.assertIn("ERROR core.upload_diagnostic_test: application error diagnostic", output)
        self.assertEqual(output.count("application error diagnostic"), 1)
        self.assertNotIn("debug diagnostic must not be logged", output)

    def test_django_request_logs_redact_invitation_and_webhook_tokens(self):
        logger = logging.getLogger("django.request")
        logger.warning("Gone: %s", "/invitations/invite-SYNTHETIC-never-real/")
        logger.warning(
            "Bad request: %s",
            "/webhooks/twilio/webhook-SYNTHETIC-never-real/",
        )

        output = self.output.getvalue()
        self.assertNotIn("invite-SYNTHETIC-never-real", output)
        self.assertNotIn("webhook-SYNTHETIC-never-real", output)
        self.assertIn("/invitations/[redacted]/", output)
        self.assertIn("/webhooks/twilio/[redacted]/", output)


class CredentialUrlLogRedactionTest(SimpleTestCase):
    def test_completed_exception_traceback_is_redacted(self):
        from core.logging import CredentialPathRedactionFormatter

        token = "traceback-SYNTHETIC-never-real"
        try:
            raise RuntimeError(f"Request failed for /invitations/{token}/")
        except RuntimeError:
            import sys
            record = logging.LogRecord(
                "core.test", logging.ERROR, __file__, 1, "Request failed", (),
                sys.exc_info(),
            )
        rendered = CredentialPathRedactionFormatter().format(record)
        self.assertNotIn(token, rendered)
        self.assertIn("RuntimeError:", rendered)
        self.assertIn("/invitations/[redacted]/", rendered)

    @override_settings(ADMINS=[("Operations", "ops@example.com")],
                       EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_error_email_redacts_reporter_request_url_and_html(self):
        from django.core import mail
        from django.test import RequestFactory
        from core.logging import CredentialPathRedactionEmailHandler

        token = "email-SYNTHETIC-never-real"
        request = RequestFactory().get(f"/webhooks/twilio/{token}/")
        record = logging.LogRecord(
            "django.request", logging.ERROR, __file__, 1, "Request failed", (), None,
        )
        record.request = request
        handler = CredentialPathRedactionEmailHandler(include_html=True)
        handler.emit(record)
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertNotIn(token, email.subject)
        self.assertNotIn(token, email.body)
        self.assertIn("[redacted]", email.body)
        self.assertTrue(email.alternatives)
        self.assertNotIn(token, email.alternatives[0][0])

    @staticmethod
    def _apply_caddy_uri_filters(uri, filters):
        for pattern, replacement in filters:
            # Caddy's RE2 replacement uses $1; translate that capture syntax to
            # Python's equivalent solely to exercise the exact configured expressions.
            replacement = re.sub(r"\$(\d+)", r"\\g<\1>", replacement)
            uri = re.sub(pattern, replacement, uri)
        return uri

    def test_caddy_access_log_filters_remove_synthetic_bearer_path_tokens(self):
        root = Path(__file__).resolve().parent.parent
        caddyfile = (root / "Caddyfile").read_text(encoding="utf-8")
        filters = re.findall(
            r'^\s*request>uri regexp "([^"]+)" "([^"]+)"\s*$',
            caddyfile,
            re.MULTILINE,
        )
        self.assertEqual(len(filters), 6)  # default errors and both site access logs

        synthetic_tokens = (
            ("invite-SYNTHETIC-token-never-real", "/invitations/"),
            ("webhook-SYNTHETIC-token-never-real", "/webhooks/twilio/"),
        )
        for token, prefix in synthetic_tokens:
            with self.subTest(path=prefix):
                uri = self._apply_caddy_uri_filters(
                    f"{prefix}{token}/?source=test", filters,
                )
                self.assertNotIn(token, uri)
                self.assertIn("[redacted]", uri)
                self.assertIn("?source=test", uri)

        ordinary_uri = "/reports/?period=current"
        self.assertEqual(
            self._apply_caddy_uri_filters(ordinary_uri, filters), ordinary_uri,
        )

    def test_gunicorn_access_format_never_includes_the_request_uri(self):
        root = Path(__file__).resolve().parent.parent
        entrypoint = (root / "entrypoint.sh").read_text(encoding="utf-8")
        match = re.search(r"--access-logformat\s+'([^']+)'", entrypoint)
        self.assertIsNotNone(match)
        access_format = match.group(1)
        self.assertIn("[path redacted]", access_format)
        for uri_atom in ("%(r)s", "%(U)s", "%(q)s"):
            self.assertNotIn(uri_atom, access_format)
