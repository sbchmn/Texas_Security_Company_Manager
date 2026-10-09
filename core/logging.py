import logging
import re

from django.conf import settings
from django.utils.log import AdminEmailHandler


def redact_credential_paths(message):
    for pattern in CredentialPathRedactionFilter._PATTERNS:
        message = pattern.sub(r"\1[redacted]", message)
    return message


class CredentialPathRedactionFilter(logging.Filter):
    """Remove bearer URL path values from Django's request/error log messages."""

    _PATTERNS = (
        re.compile(r"(?i)(/invitations/)[^/?#\s]+"),
        re.compile(r"(?i)(/webhooks/[^/?#\s]+/)[^/?#\s]+"),
    )

    def filter(self, record):
        message = redact_credential_paths(record.getMessage())
        # Materialize the already formatted message so the original args cannot reinsert
        # an unredacted path when a downstream handler formats the same LogRecord.
        record.msg = message
        record.args = ()
        return True


class CredentialPathRedactionFormatter(logging.Formatter):
    def format(self, record):
        return redact_credential_paths(super().format(record))


class CredentialPathRedactionEmailHandler(AdminEmailHandler):
    def emit(self, record):
        if settings.ADMINS:
            super().emit(record)

    def send_mail(self, subject, message, *args, **kwargs):
        # Django's exception reporter includes request URLs independently of the
        # LogRecord message, so sanitize the completed report as well.
        html = kwargs.get("html_message")
        if html is not None:
            kwargs["html_message"] = redact_credential_paths(html)
        return super().send_mail(
            redact_credential_paths(subject), redact_credential_paths(message),
            *args, **kwargs,
        )
