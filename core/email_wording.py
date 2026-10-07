"""Company email wording, rendered when queued so retries retain the same message."""
from dataclasses import dataclass

from django.core.exceptions import ValidationError

from . import sms

MAX_SUBJECT_LENGTH = 200
MAX_BODY_LENGTH = 10000
DEFAULT_SUBJECT = "{subject}"
DEFAULT_BODY = "{body}"
PLACEHOLDERS = {
    **sms.PLACEHOLDERS,
    "company": "Company display name",
    "body": "Complete original workflow message, including its details and links",
    "role": "Access role granted by an invitation",
}


@dataclass(frozen=True)
class EmailNotice:
    key: str
    label: str
    audience: str
    fields: tuple
    family: str
    sample_subject: str
    sample_body: str
    required_link: bool = False

    @property
    def allowed(self):
        return tuple(dict.fromkeys(("subject", "body", "company") + self.fields))


EMAIL_NOTICES = {
    key: EmailNotice(key, notice.label, notice.audience, notice.allowed, notice.family,
                     notice.label, sms.fill(notice.default, sms.SAMPLE_VALUES))
    for key, notice in sms.SMS_NOTICES.items()
}
# These samples illustrate the original longer workflow content, not a delivered record.
EMAIL_SAMPLES = {
    "shift.published": ("Shift published", "Northpark Center on Fri Oct 9, 6 PM-2 AM has been published."),
    "shift.changed": ("Shift changed", "Northpark Center now runs Fri Oct 9, 6 PM-2 AM. Open your shifts to review the change."),
    "credential.reminder": ("Credential renewal: Officer registration",
        "Jordan Lee's Officer registration expires on 2026-10-09 (30 days)."),
    "credential.reminder.state": ("Credential attention: Officer registration",
        "Jordan Lee's Officer registration is expired."),
    "credential.reminder.no_expiry": ("No expiry recorded: Officer registration",
        "Jordan Lee's Officer registration is active with no renewal date, so nothing can warn before it lapses."),
    "credential.missing": ("Missing credential: Officer registration",
        "Jordan Lee has no Officer registration record, which their role requires. They cannot be assigned a post that blocks on it."),
    "training.reminder": ("Training renewal: First aid",
        "Jordan Lee's First aid training expires on 2026-10-09 (30 days)."),
    "onboarding.signed": ("Employment agreement - signed and filed",
        "Your signed document and its signing audit certificate are now in your personnel file."),
    "onboarding.signature_requested": ("Document ready to sign",
        "Please sign Employment agreement for Example Security.\nhttps://sign.example.com/s/sample\n"
        "The step completes after the signed document is filed in your personnel file."),
}
for key, (subject, body) in EMAIL_SAMPLES.items():
    original = EMAIL_NOTICES[key]
    EMAIL_NOTICES[key] = EmailNotice(
        key, original.label, original.audience, original.fields, original.family, subject, body,
        required_link=key == "onboarding.signature_requested",
    )
for key, label, subject, body in (
    ("membership.invitation", "Team invitation", "Join Example Security",
     "You were invited as Officer. Accept this single-use invitation within 72 hours: https://example.com/invitations/sample"),
    ("membership.invitation.person", "Personnel sign-in invitation", "Set up your sign-in for Example Security",
     "Jordan Lee, you were given access to Example Security. Create your sign-in within 72 hours: https://example.com/invitations/sample"),
):
    EMAIL_NOTICES[key] = EmailNotice(key, label, "Invitee", ("first_name", "officer", "role", "link"),
                                    "membership", subject, body, required_link=True)


def notice_for(key):
    return EMAIL_NOTICES.get(key)


def validate_wording(notice, subject, body):
    errors = []
    for label, text, limit in (("Subject", subject, MAX_SUBJECT_LENGTH), ("Body", body, MAX_BODY_LENGTH)):
        if not text.strip():
            errors.append(f"{label}: enter wording or restore the built-in default.")
        if len(text) > limit:
            errors.append(f"{label}: keep the template at or below {limit} characters.")
        remaining = sms.TOKEN.sub("", text)
        if "{" in remaining or "}" in remaining:
            errors.append(f"{label}: a brace is unmatched.")
        unknown = sorted(set(sms.TOKEN.findall(text)) - set(notice.allowed))
        if unknown:
            errors.append(f"{label}: unavailable placeholders: " + ", ".join("{" + name + "}" for name in unknown))
    if "\n" in subject or "\r" in subject:
        errors.append("Subject: use a single line.")
    if notice.required_link and not ({"body", "link"} & set(sms.TOKEN.findall(body))):
        errors.append("Keep {body} or {link} in the body so the recipient can accept the invitation or sign.")
    return errors


def sample_values(organization, notice, *, base_url=None):
    values = {**sms.SAMPLE_VALUES, "company": organization.display_name,
              "subject": notice.sample_subject, "body": notice.sample_body, "role": "Officer"}
    sms_notice = sms.notice_for(notice.key)
    values["link"] = (
        "https://example.com/invitations/sample" if notice.family == "membership"
        else "https://sign.example.com/s/sample" if notice.required_link
        else sms.sample_link(organization, sms_notice, base_url=base_url) if sms_notice else ""
    )
    return values


def preview(organization, notice, subject=DEFAULT_SUBJECT, body=DEFAULT_BODY, *, base_url=None):
    values = sample_values(organization, notice, base_url=base_url)
    return sms.fill(subject, values), sms.fill(body, values)


def render_email(organization, key, subject, body, context=None, first_name="", template=None):
    notice = notice_for(key)
    if notice is None:
        return subject, body
    if template is None:
        template = organization.email_templates.filter(notice_key=key).first()
    if template is None:
        return subject, body
    errors = validate_wording(notice, template.subject, template.body)
    if errors:
        raise ValidationError(errors)
    values = sms.context_values(organization, context, subject)
    sms_notice = sms.notice_for(key)
    values["link"] = (context or {}).get("link") or (
        sms.build_link(organization, sms_notice, values) if sms_notice else "")
    values.update(company=organization.display_name, body=body, first_name=first_name)
    if notice.required_link and "{body}" not in template.body and not values["link"]:
        raise ValidationError("The email's required invitation or signing link is unavailable.")
    result_subject, result_body = sms.fill(template.subject, values), sms.fill(template.body, values)
    if not result_subject.strip() or len(result_subject) > MAX_SUBJECT_LENGTH or "\n" in result_subject or "\r" in result_subject:
        raise ValidationError("The rendered email subject must be a nonempty single line of at most 200 characters.")
    if not result_body.strip():
        raise ValidationError("The rendered email body is empty.")
    return result_subject, result_body
