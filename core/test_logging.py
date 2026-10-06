import io
import logging

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
