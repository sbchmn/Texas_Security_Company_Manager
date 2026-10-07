"""Text-message wording: a built-in default for every notice, and a per-company override.

An email can carry a subject, a paragraph and a signature; a text has about 160 characters before
the carrier splits it and bills twice. So every notice that can be texted has its own short wording
here rather than reusing the email body, and an owner can rewrite any of them from
Settings → Messaging → Notification wording.

Three rules hold whatever an owner writes:

* the company name is always prepended, because a text from an unknown number with no sender
  name reads as spam and carriers filter it as such;
* only the placeholders listed for that notice may be used, checked on save, so a typo cannot
  turn into a literal ``{sight}`` on forty phones;
* times are rendered in the company's own time zone, in 12-hour form, because the database keeps
  UTC and "23:00" is not when a Dallas guard's 6 PM shift starts.
"""
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

MAX_TEMPLATE_LENGTH = 480

# ── Segment arithmetic ────────────────────────────────────────────────────────

GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENDED = set("^{}\\[~]|€\f")

# Word processors and phones substitute these for their plain forms, and one of them anywhere in a
# message flips the whole text to UCS-2 — 70 characters a segment instead of 160.
PLAIN_SUBSTITUTES = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2026": "...",
    "\u00b7": "-", "\u2022": "-", "\u00a0": " ", "\u2009": " ", "\u202f": " ",
}


def plain_text(text):
    return "".join(PLAIN_SUBSTITUTES.get(char, char) for char in text)


def segment_info(text):
    """How carriers will bill ``text``: encoding, characters counted, and segments."""
    text = text or ""
    if all(char in GSM7_BASIC or char in GSM7_EXTENDED for char in text):
        units = sum(2 if char in GSM7_EXTENDED else 1 for char in text)
        single, multi, encoding = 160, 153, "GSM-7"
    else:
        units = len(text.encode("utf-16-le")) // 2
        single, multi, encoding = 70, 67, "Unicode"
    segments = 0 if units == 0 else (1 if units <= single else math.ceil(units / multi))
    return {"encoding": encoding, "characters": units, "segments": segments,
            "limit": single if segments <= 1 else multi * segments}


# ── Local time ────────────────────────────────────────────────────────────────

def zone_for(organization):
    try:
        return ZoneInfo(getattr(organization, "timezone", "") or settings.TIME_ZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(settings.TIME_ZONE)


def _local(value, zone):
    if isinstance(value, datetime):
        return timezone.localtime(value, zone) if timezone.is_aware(value) else value
    return value


def day_label(value, zone=None, weekday=True):
    """``Fri Oct 9`` — no leading zero, no year, which is what a schedule text needs."""
    value = _local(value, zone) if zone else value
    text = f"{value:%b} {value.day}"
    return f"{value:%a} {text}" if weekday else text


def clock_label(value, zone=None):
    """``6 PM`` on the hour, ``6:30 PM`` otherwise."""
    value = _local(value, zone) if zone else value
    hour = value.hour % 12 or 12
    suffix = "AM" if value.hour < 12 else "PM"
    return f"{hour} {suffix}" if value.minute == 0 else f"{hour}:{value.minute:02d} {suffix}"


def window_label(starts_at, ends_at, zone):
    """``Fri Oct 9, 6 PM-2 AM``; a window longer than a day names its end date too."""
    start, end = _local(starts_at, zone), _local(ends_at, zone)
    text = f"{day_label(start)}, {clock_label(start)}"
    if end is None:
        return text
    if (end - start).total_seconds() >= 24 * 3600:
        return f"{text} to {day_label(end)}, {clock_label(end)}"
    return f"{text}-{clock_label(end)}"


def shift_when(shift):
    """A post's hours in its company's time zone — what every notice body should say."""
    return window_label(shift.starts_at, shift.ends_at, zone_for(shift.organization))


def moment_label(organization, value):
    """``Fri Oct 9, 6:30 PM`` in the company's time zone."""
    zone = zone_for(organization)
    return f"{day_label(value, zone)}, {clock_label(value, zone)}"


def date_span(organization, first, last):
    """``Oct 9-Oct 12`` (or one day) in the company's time zone."""
    zone = zone_for(organization)
    first_label = day_label(_local(first, zone), weekday=False)
    last_label = day_label(_local(last, zone), weekday=False)
    return first_label if first_label == last_label else f"{first_label}-{last_label}"


# ── Links ─────────────────────────────────────────────────────────────────────

def public_base_url(organization):
    """The address a phone can open: the company's verified domain, else PUBLIC_BASE_URL, else none."""
    if organization is not None and getattr(organization, "pk", None):
        from .models import OrganizationDomain
        domain = organization.domains.filter(verified=True, status=OrganizationDomain.Status.VERIFIED) \
            .order_by("created_at").values_list("hostname", flat=True).first()
        if domain:
            return f"https://{domain}"
    return (getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/")


def build_link(organization, notice, values):
    base = public_base_url(organization)
    if not base or not notice.link:
        return ""
    name, *keys = notice.link
    try:
        return base + reverse(name, kwargs={key: values[key] for key in keys})
    except (KeyError, NoReverseMatch):
        return base + reverse("notifications")


# ── Registry ──────────────────────────────────────────────────────────────────

PLACEHOLDERS = {
    "first_name": "Recipient's first name",
    "site": "Site name",
    "client": "Client name",
    "post": "Post name on the shift",
    "date": "Day, e.g. Fri Oct 9",
    "start": "Start time, e.g. 6 PM",
    "end": "End time, e.g. 2 AM",
    "when": "Day and hours, e.g. Fri Oct 9, 6 PM-2 AM",
    "officer": "The officer the notice is about",
    "other": "The second officer in a swap or trade",
    "note": "Reason or note someone typed",
    "item": "Credential, training, document or step name",
    "status": "Outcome or state, e.g. expired",
    "due": "Due or expiry date, e.g. Oct 9",
    "days": "Days remaining",
    "dates": "Date range, e.g. Oct 9-Oct 12",
    "kind": "clock-in or clock-out",
    "time": "Day and time, e.g. Fri Oct 9, 6 PM",
    "count": "How many",
    "period": "Payroll period",
    "actor": "Person who took the action",
    "hours": "Hours uncovered",
    "gaps": "What is missing, e.g. clock-out",
    "minutes": "Minutes past the alert deadline when first observed",
    "action": "archive or delete",
    "link": "Link to the page to act on (needs PUBLIC_BASE_URL or a verified domain)",
    "subject": "The notice's in-app title",
}
COMMON_FIELDS = ("first_name", "link", "subject")
SHIFT_FIELDS = ("site", "client", "post", "date", "start", "end", "when")

SAMPLE_VALUES = {
    "first_name": "Jordan", "site": "Northpark Center", "client": "Northpark Holdings", "post": "Lobby",
    "date": "Fri Oct 9", "start": "6 PM", "end": "2 AM", "when": "Fri Oct 9, 6 PM-2 AM",
    "officer": "Sam Rivera", "other": "Alex Kim", "note": "Coverage changed", "item": "Level III license",
    "status": "expired", "due": "Oct 30", "days": "21", "dates": "Oct 9-Oct 12", "kind": "clock-out",
    "time": "Fri Oct 9, 2:07 AM", "count": "3", "period": "Oct 1-Oct 15", "actor": "Pat Owner",
    "hours": "8", "gaps": "clock-out", "action": "archive", "subject": "Notice title", "minutes": "2",
}


@dataclass(frozen=True)
class SmsNotice:
    key: str
    label: str
    audience: str
    default: str
    fields: tuple = ()
    link: tuple = ("notifications",)
    family: str = field(default="")

    def __post_init__(self):
        if not self.family:
            object.__setattr__(self, "family", self.key.split(".", 1)[0])

    @property
    def allowed(self):
        return tuple(dict.fromkeys(COMMON_FIELDS + tuple(self.fields)))


def _n(key, label, audience, default, fields=(), link=("notifications",)):
    return SmsNotice(key=key, label=label, audience=audience, default=default, fields=tuple(fields), link=link)


S = SHIFT_FIELDS
SMS_NOTICES = {item.key: item for item in (
    # Scheduling
    _n("shift.published", "Shift assigned", "Officer",
       "You're scheduled at {site} {when}. {link}", S, ("my_shifts",)),
    _n("shift.published.series", "Recurring shifts added", "Officer",
       "{count} recurring shifts at {site} were added to your schedule, starting {date}. {link}",
       ("site", "client", "date", "count"), ("my_shifts",)),
    _n("shift.changed", "Shift changed", "Officer",
       "Your shift at {site} changed. It now runs {when}. {link}", S, ("my_shifts",)),
    _n("shift.cancelled", "Shift cancelled", "Officer",
       "Your shift at {site} {when} was cancelled. {note}", S + ("note",), ("my_shifts",)),
    _n("shift.open", "Open shift available", "Qualified officers",
       "Open shift: {site} {when}. Ask for it here: {link}", S, ("open_posts",)),
    _n("shift.claim_requested", "Officer asked for an open shift", "Dispatch",
       "{officer} asked to take the open shift at {site} {when}. Review: {link}",
       S + ("officer", "note"), ("shift_requests", "shift_id")),
    _n("shift.claim_approved", "Open-shift request approved", "Officer",
       "Approved: you're scheduled at {site} {when}. {link}", S + ("note",), ("my_shifts",)),
    _n("shift.claim_declined", "Open-shift request declined", "Officer",
       "Your request for the open shift at {site} {when} was declined. {note}", S + ("note",), ("open_posts",)),
    _n("shift.coverage_gap", "Cancellation left a gap", "Dispatch",
       "{site} loses {hours}h of coverage after the {when} shift was cancelled. Find relief: {link}",
       S + ("hours", "note"), ("schedule",)),
    _n("shift.series_blocked", "Recurring shift could not be placed", "Dispatch",
       "A recurring shift at {site} {when} was not scheduled: {note} {link}", S + ("note",), ("schedule",)),
    _n("shift.swap_offered", "Asked to cover a shift", "Officer asked to cover",
       "{officer} asked you to cover {site} {when}. Accept or decline: {link}", S + ("officer", "note"),
       ("my_shifts",)),
    _n("shift.swap_offered.officer", "Manager arranging cover", "Officer giving up the shift",
       "A manager asked {other} to cover your shift at {site} {when}. You keep it until it's approved.",
       S + ("other",), ("my_shifts",)),
    _n("shift.swap_agreed", "Swap needs approval", "Dispatch",
       "Swap needs approval: {other} will cover {officer}'s shift at {site} {when}. {link}",
       S + ("officer", "other"), ("swaps",)),
    _n("shift.swap_agreed.requester", "Swap accepted by colleague", "Officer giving up the shift",
       "{officer} accepted your swap for {site} {when}. Waiting on a manager.", S + ("officer",), ("my_shifts",)),
    _n("shift.swap_declined", "Swap declined by colleague", "Officer giving up the shift",
       "{officer} can't cover your shift at {site} {when}. You're still scheduled. {link}",
       S + ("officer",), ("my_shifts",)),
    _n("shift.swap_withdrawn", "Swap offer withdrawn", "Officer asked to cover",
       "{officer} no longer needs {site} {when} covered. Nothing for you to do.", S + ("officer",), ("my_shifts",)),
    _n("shift.swap_approved", "Swap approved (new officer)", "Officer taking the shift",
       "Swap approved: you're scheduled at {site} {when}. {link}", S + ("note",), ("my_shifts",)),
    _n("shift.swap_approved.requester", "Swap approved (original officer)", "Officer giving up the shift",
       "Swap approved: {officer} now covers your shift at {site} {when}.", S + ("officer",), ("my_shifts",)),
    _n("shift.swap_refused", "Swap not approved", "Both officers",
       "The swap for {site} {when} was not approved. {note}", S + ("note",), ("my_shifts",)),
    _n("shift.swap_closed", "Swap offer closed", "Officer asked to cover",
       "The swap offer for {site} {when} closed: {note}", S + ("note",), ("my_shifts",)),
    _n("shift.swap_expired", "Swap offer expired", "Both officers",
       "The swap offer for {site} {when} expired before it was answered. The shift stays as scheduled.",
       S, ("my_shifts",)),
    _n("shift.exchange_proposed", "Trade proposed", "Officer asked to trade",
       "{officer} wants to trade their shift at {site} {when}. Pick one of yours to trade: {link}",
       S + ("officer", "note"), ("my_shifts",)),
    _n("shift.exchange_proposed.officer", "Manager arranging a trade", "Officer whose shift is traded",
       "A manager proposed trading your shift at {site} {when} with {other}. You keep it until it's approved.",
       S + ("other",), ("my_shifts",)),
    _n("shift.exchange_declined", "Trade declined", "Officer who proposed it",
       "{officer} declined your trade. You keep {site} {when}. {link}", S + ("officer",), ("my_shifts",)),
    _n("shift.exchange_agreed", "Trade needs approval", "Dispatch",
       "Trade needs approval: {officer} and {other} agreed to trade shifts. {link}",
       S + ("officer", "other"), ("swaps",)),
    _n("shift.exchange_agreed.initiator", "Trade accepted by colleague", "Officer who proposed it",
       "{officer} accepted your trade and put in {site} {when}. Waiting on a manager.",
       S + ("officer",), ("my_shifts",)),
    _n("shift.exchange_withdrawn", "Trade withdrawn", "Officer asked to trade",
       "{officer} withdrew the trade for {site} {when}. Nothing has moved.", S + ("officer",), ("my_shifts",)),
    _n("shift.exchange_approved", "Trade approved", "Both officers",
       "Your shift trade with {other} was approved. See your updated schedule: {link}",
       S + ("officer", "other"), ("my_shifts",)),
    _n("shift.exchange_refused", "Trade not approved", "Both officers",
       "Your shift trade was not approved. You both keep your shifts. {note}",
       S + ("officer", "other", "note"), ("my_shifts",)),
    _n("shift.exchange_closed", "Trade closed", "Both officers",
       "A shift trade closed: {note}", ("officer", "other", "note"), ("my_shifts",)),
    _n("shift.exchange_expired", "Trade expired", "Both officers",
       "Your shift trade with {other} expired before it was approved. Both of you keep your shifts.",
       S + ("officer", "other"), ("my_shifts",)),
    # Time off
    _n("timeoff.requested", "Time off requested", "Managers",
       "{officer} asked for time off {dates}. Review: {link}", ("officer", "dates", "note"), ("time_off",)),
    _n("timeoff.approved", "Time off approved", "Officer",
       "Your time off {dates} was approved. {note}", ("dates", "note"), ("my_time_off",)),
    _n("timeoff.declined", "Time off declined", "Officer",
       "Your time off request for {dates} was declined. {note} {link}", ("dates", "note"), ("my_time_off",)),
    # Timekeeping
    _n("punch.exception", "Timecard exception", "Dispatch",
       "Timecard exception: {officer} {kind} at {time}. Review: {link}", ("officer", "kind", "time", "note"),
       ("time_review",)),
    _n("punch.missing", "Missing punch", "Officer and dispatch",
       "Missing {gaps} for {officer} at {site} {when}. Fix the timecard: {link}", S + ("officer", "gaps"),
       ("notifications",)),
    _n("punch.late_arrival", "Late arrival: no clock-in recorded", "Officer and scoped dispatch / supervisors",
       "No clock-in recorded for {officer} at {site} after {time}. Confirm arrival and coverage: {link}",
       S + ("officer", "time", "minutes"), ("attendance_detail", "case_id")),
    _n("punch.overdue_departure", "Overdue departure: no clock-out recorded", "Officer and scoped dispatch / supervisors",
       "No clock-out recorded for {officer} at {site} after {time}. Confirm status and arrange relief; do not assume departure: {link}",
       S + ("officer", "time", "minutes"), ("attendance_detail", "case_id")),
    _n("punch.correction_requested", "Time correction requested", "Time reviewers",
       "{officer} asked to move a {kind} to {time}. Review: {link}", ("officer", "kind", "time", "note"),
       ("time_review",)),
    _n("punch.correction_approved", "Time correction approved", "Officer",
       "Your {kind} correction was approved for {time}.", ("kind", "time", "note"), ("clock",)),
    _n("punch.correction_rejected", "Time correction declined", "Officer",
       "Your {kind} correction was declined. {note}", ("kind", "time", "note"), ("clock",)),
    # Payroll
    _n("payroll.exported", "Payroll exported", "Payroll",
       "Payroll for {period} was exported by {actor}.", ("period", "actor", "count"), ("payroll",)),
    _n("payroll.locked", "Payroll approved and locked", "Payroll",
       "Payroll for {period} was approved and locked by {actor}.", ("period", "actor", "count"), ("payroll",)),
    _n("payroll.reopened", "Payroll reopened", "Payroll",
       "Payroll for {period} was reopened by {actor}: {note}", ("period", "actor", "note"), ("payroll",)),
    # Credentials and training
    _n("credential.reminder", "Credential renewal reminder", "Officer and compliance",
       "{officer}'s {item} expires {due} ({days} days). Please renew it. {link}",
       ("officer", "item", "due", "days")),
    _n("credential.reminder.state", "Credential needs attention", "Officer and compliance",
       "{officer}'s {item} is {status}. Please take care of it. {link}", ("officer", "item", "status")),
    _n("credential.reminder.no_expiry", "Credential has no expiry date", "Officer and compliance",
       "{officer}'s {item} has no expiry date on file. {link}", ("officer", "item", "status")),
    _n("credential.escalated", "Credential renewal escalated", "Escalation roles",
       "Escalated: {officer}'s {item} expires {due} ({days} days) and is still not renewed. {link}",
       ("officer", "item", "due", "days"), ("compliance",)),
    _n("credential.missing", "Required credential missing", "Officer and compliance",
       "{officer} has no {item} on file, which their role requires. {link}", ("officer", "item")),
    _n("credential.registry_adverse", "Registry check flagged a credential", "Officer and compliance",
       "Registry check: {officer}'s {item} came back {status}. {link}", ("officer", "item", "status")),
    _n("training.reminder", "Training renewal reminder", "Officer and compliance",
       "{officer}'s {item} training expires {due} ({days} days). {link}", ("officer", "item", "due", "days")),
    # Onboarding
    _n("onboarding.assigned", "Onboarding steps assigned", "New hire",
       "Welcome! You have {count} onboarding steps to finish. Start here: {link}", ("count", "due"),
       ("my_onboarding",)),
    _n("onboarding.overdue", "Onboarding step overdue", "Step owner",
       "{item} for {officer} was due {due} and is still open. {link}", ("officer", "item", "due", "days")),
    _n("onboarding.done", "Onboarding step completed", "New hire",
       "{item} is marked complete.", ("item", "note"), ("my_onboarding",)),
    _n("onboarding.waived", "Onboarding step waived", "New hire",
       "{item} has been waived for you. {note}", ("item", "note"), ("my_onboarding",)),
    _n("onboarding.signature_requested", "Document ready to sign", "Signer",
       "Please sign {item}: {link}", ("item",), ("my_onboarding",)),
    _n("onboarding.signed", "Document signed and filed", "Signer",
       "{item} is signed and filed. Thank you.", ("item",), ("my_onboarding",)),
    # Records
    _n("retention.disposition_requested", "Disposal needs a second approval", "Owners and admins",
       "Second approval needed to {action} {item}. {link}", ("item", "action", "actor", "note"),
       ("retention_review",)),
    _n("retention.hold_changed", "Legal hold changed", "Records staff",
       "Legal hold {status} on {item} by {actor}.", ("item", "status", "actor"), ("retention_review",)),
    _n("retention.disposition_executed", "Disposal carried out", "Requester",
       "Your request to {action} {item} was approved by {actor} and carried out.", ("item", "action", "actor"),
       ("retention_review",)),
    _n("import.completed", "Import finished", "Person who ran it",
       "Import finished: {count} {item} rows applied.", ("count", "item"), ("imports",)),
)}
del S

FALLBACK = SmsNotice(key="", label="Any other notice", audience="Recipient", default="{subject} {link}")

FAMILY_LABELS = {
    "shift": "Scheduling", "timeoff": "Time off", "punch": "Timekeeping", "payroll": "Payroll",
    "credential": "Credentials", "training": "Credentials", "onboarding": "Onboarding",
    "retention": "Records", "import": "Records",
}


def notice_for(key):
    return SMS_NOTICES.get(key)


# ── Templates ─────────────────────────────────────────────────────────────────

TOKEN = re.compile(r"\{([^{}]*)\}")


def validate_template(notice, text):
    """Errors for one proposed wording, as sentences an owner can act on."""
    errors = []
    text = (text or "").strip()
    if not text:
        errors.append("Write the message, or press Reset to default.")
        return errors
    if len(text) > MAX_TEMPLATE_LENGTH:
        errors.append(f"Keep the wording under {MAX_TEMPLATE_LENGTH} characters.")
    stripped = TOKEN.sub("", text)
    if "{" in stripped or "}" in stripped:
        errors.append("A brace is unmatched. Placeholders look like {site}.")
    unknown = sorted({name for name in TOKEN.findall(text) if name not in notice.allowed})
    if unknown:
        errors.append("Not available for this notice: " + ", ".join("{" + name + "}" for name in unknown)
                      + ". Use " + ", ".join("{" + name + "}" for name in notice.allowed) + ".")
    return errors


def fill(template, values):
    return TOKEN.sub(lambda match: str(values.get(match.group(1), "")), template)


def tidy(text):
    text = plain_text(text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +([.,;:!?])", r"\1", text)
    text = re.sub(r"\.\.(?=\s|$)(?<!\.\.\.)", ".", text)
    text = re.sub(r":\s*$", ".", text.strip())
    return text.strip()


# "Review: {link}" with no address configured would end a text on a dangling label.
LINK_LABEL = re.compile(r"(?:(?:(?<=[.!?] )|^)[A-Z][^.!?{}:\n]*:\s*)?\{link\}")


def drop_link(template):
    return LINK_LABEL.sub("", template)


def company_prefix(organization):
    name = (getattr(organization, "display_name", "") or getattr(organization, "legal_name", "") or "").strip()
    return f"{plain_text(name)}: " if name else ""


def _name(value):
    if hasattr(value, "get_full_name"):
        return (value.get_full_name() or "").strip() or str(value)
    return getattr(value, "full_name", None) or str(value or "")


def context_values(organization, context, subject=""):
    """Turn the objects a call site hands over into the strings a template can use."""
    zone = zone_for(organization)
    context = dict(context or {})
    values = {"subject": subject or ""}
    shift = context.pop("shift", None)
    if shift is not None:
        site = getattr(shift, "site", None)
        values.update({
            "site": getattr(site, "name", "") or "",
            "client": getattr(getattr(site, "client", None), "name", "") or "",
            "post": getattr(shift, "post_name", "") or "",
            "date": day_label(shift.starts_at, zone),
            "start": clock_label(shift.starts_at, zone),
            "end": clock_label(shift.ends_at, zone) if shift.ends_at else "",
            "when": window_label(shift.starts_at, shift.ends_at, zone),
            "shift_id": shift.pk,
        })
    site = context.pop("site", None)
    if site is not None:
        values["site"] = getattr(site, "name", None) or str(site)
        values.setdefault("client", getattr(getattr(site, "client", None), "name", "") or "")
    starts = context.pop("starts_at", None)
    if starts is not None:
        ends = context.pop("ends_at", None)
        values.update({"date": day_label(starts, zone), "start": clock_label(starts, zone),
                       "when": window_label(starts, ends, zone)})
        if ends is not None:
            values["end"] = clock_label(ends, zone)
    at = context.pop("at", None)
    if at is not None:
        values["time"] = f"{day_label(at, zone)}, {clock_label(at, zone)}"
    for key in ("officer", "other", "actor"):
        if key in context:
            values[key] = _name(context.pop(key))
    due = context.pop("due", None)
    if due is not None:
        values["due"] = day_label(due, zone, weekday=False) if isinstance(due, (date, datetime)) else str(due)
    dates = context.pop("dates", None)
    if dates:
        values["dates"] = date_span(organization, *dates)
    period = context.pop("period", None)
    if period:
        first, last = period
        values["period"] = date_span(organization, first, last)
    for key, value in context.items():
        values[key] = "" if value is None else str(value)
    return values


def wording_for(organization, notice):
    """The organization's own wording for ``notice`` if it has one, else the built-in default."""
    if organization is not None and getattr(organization, "pk", None) and notice.key:
        custom = organization.sms_templates.filter(notice_key=notice.key).values_list("body", flat=True).first()
        if custom:
            return custom
    return notice.default


def render_sms(organization, key, context=None, subject="", first_name="", template=None):
    """The exact text that will be sent: company prefix + wording with placeholders filled."""
    notice = SMS_NOTICES.get(key) or FALLBACK
    context = {name: value for name, value in (context or {}).items() if name != "notice"}
    values = context_values(organization, context, subject)
    values["first_name"] = first_name or ""
    values["link"] = build_link(organization, notice, values)
    wording = template if template is not None else wording_for(organization, notice)
    if not values["link"]:
        wording = drop_link(wording)
    return company_prefix(organization) + tidy(fill(wording, values))


def sample_link(organization, notice, *, base_url=None):
    base = public_base_url(organization) if base_url is None else base_url
    if not base:
        return ""
    path = reverse(notice.link[0]) if notice.link and len(notice.link) == 1 else reverse("notifications")
    return base + path


def preview_sms(organization, notice, template=None, *, base_url=None):
    """A sample rendering with made-up values, for the settings page."""
    values = dict(SAMPLE_VALUES)
    values["link"] = sample_link(organization, notice, base_url=base_url)
    wording = template if template is not None else wording_for(organization, notice)
    if not values["link"]:
        wording = drop_link(wording)
    return company_prefix(organization) + tidy(fill(wording, values))


def resolve_key(event_type, sms):
    """Which wording a notice uses: the call site's explicit choice, else its event type."""
    key = (sms or {}).get("notice") or event_type
    return key if key in SMS_NOTICES else event_type
