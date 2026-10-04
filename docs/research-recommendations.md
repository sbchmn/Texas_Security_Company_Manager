# Research-backed recommendations

**Research date:** 2026-09-29  
**Purpose:** Discovery guidance, not legal advice

The links below are primary government guidance or official vendor/standards
documentation. Texas requirements must be converted into a separate, section-level
control matrix and approved before implementation. That matrix's remaining content work,
and every other outstanding item, is tracked in
[development-roadmap.md](development-roadmap.md).

## HCRM record catalog

Use a configurable record-type catalog rather than one generic “documents” folder.
Recommended initial families are:

1. **Identity and employment:** worker profile, contact/emergency contact, offer and job
   assignment, employment status/history, Form I-9 evidence/metadata, tax withholding,
   direct-deposit authorization, handbook/policy acknowledgments, and separation.
2. **Work and pay:** availability, assigned position/site, pay rate history, raw time
   events, computed timecards, adjustment reasons, leave, payroll exports, and approvals.
3. **Texas security compliance:** company licenses, individual registrations and
   commissions, role/ownership relationships, training and instructor evidence,
   renewals, status checks, assignment eligibility, incident/reportable-event evidence,
   and communications/overrides.
4. **Health/safety and sensitive records:** workers' compensation, accommodation/leave,
   drug/background-screening status, and investigation/discipline records, isolated by
   stricter permissions rather than exposed in the general personnel file.
5. **Custom records:** admin-defined schema, applicability, required evidence, signature
   or acknowledgment, renewal, reminder, retention, sensitivity, and access policy.

Retention must be calculated by record type and event (hire, creation, termination,
expiration, or supersession), not a single employee-folder date. Relevant baselines
include the [USCIS I-9 retention rule](https://www.uscis.gov/i-9-central/form-i-9-resources/handbook-for-employers-m-274/100-retaining-form-i-9),
[DOL FLSA recordkeeping guidance](https://www.dol.gov/agencies/whd/fact-sheets/21-flsa-recordkeeping),
[EEOC recordkeeping requirements](https://www.eeoc.gov/employers/recordkeeping-requirements),
and [IRS employment-tax recordkeeping guidance](https://www.irs.gov/businesses/small-businesses-self-employed/employment-tax-recordkeeping).
Legal holds override scheduled disposition.

Texas controls should begin with [Texas Occupations Code Chapter 1702](https://statutes.capitol.texas.gov/Docs/OC/htm/OC.1702.htm)
and the [Texas DPS Private Security Program](https://www.dps.texas.gov/section/private-security),
then trace each requirement to current administrative rules, DPS forms/guidance, role,
trigger, evidence, enforcement, notification, retention, and effective dates. “All
required documents” is not testable until that matrix exists.

## Payroll export baseline

A useful detail export should contain, where applicable:

- stable employee ID and display name;
- pay-period and workweek boundaries with timezone;
- branch, client, site, shift, job/pay code, and cost center;
- raw in/out events and provenance (online/offline, kiosk/mobile, location exception);
- regular, overtime, double-time, paid/unpaid break, leave, holiday, training, travel,
  differential, and other categorized hours;
- raw duration, rounded duration, rounding-policy/version, adjustments, and reasons;
- rate identifier and effective rate where the receiving payroll workflow permits it;
- totals by employee and accounting dimension;
- approval/lock/reopen status, approvers, timestamps, report ID/version, and export time;
- exception flags for missing punches, pending corrections, compliance issues, and
  unapproved time.

The [DOL FLSA recordkeeping guidance](https://www.dol.gov/agencies/whd/fact-sheets/21-flsa-recordkeeping)
lists core wage/hour records but does not prescribe a particular time-clock or export
format. Preserve actual time evidence and make configured rounding transparent; legal
review should approve every rounding and overtime policy.

## Secure document upload defaults

Follow the [OWASP File Upload Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html):
allowlist extensions, validate actual type/signature, generate storage names, cap sizes,
store outside the web root or in isolated object storage, authorize every operation,
scan content, and protect upload endpoints against CSRF and abuse.

Recommended starting policy, adjustable after workflow testing:

- allow PDF, PDF/A, PNG, JPEG, DOCX, XLSX, and plain CSV/TXT where a workflow needs them;
- reject executables, scripts, macro-enabled Office files, HTML/SVG active content, and
  encrypted archives by default;
- cap ordinary documents/images at 25 MiB per file and CSV imports at 50 MiB;
- quarantine uploads until asynchronous malware scanning succeeds;
- use private storage, short-lived signed downloads, encryption in transit and at rest,
  checksums, randomized object keys, version metadata, and tenant/scope authorization;
- render previews from sanitized derivatives, never by serving an active upload inline;
- make deployment-wide hard limits operator-controlled so a tenant admin cannot exhaust
  storage or memory.

These are product defaults, not universal standards; evidence video and large batch
imports should use separately authorized workflows and limits.

## Mobile delivery options

### Responsive web only

**Meaning:** the browser loads the site normally; nothing is installed.

**Pros:** fastest and cheapest delivery, one deployable UI, immediate updates, simple
support. **Cons:** weakest offline/background behavior, less app-like launch and push
experience, and browser limitations can affect reliable time-clock/device workflows.
It is not sufficient alone because offline punches are required.

### Installable Progressive Web App (recommended first mobile milestone)

**Meaning:** a responsive web app adds a manifest and service worker so supported devices
can install it and cache an offline time-clock shell. See the [MDN PWA overview](https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps).

**Pros:** shared code and deployment, installable experience, offline queueing, rapid
updates, and no mandatory app-store release for the initial pilot. **Cons:** capability
and background-execution differences across browsers/operating systems, additional
security/conflict work for offline data, and potentially less predictable push,
location, NFC, and kiosk behavior than native apps.

### Native iOS and Android

**Meaning:** separate packaged apps distributed through Apple/Google channels, backed by
the same API; these may use shared cross-platform code but still require platform builds.

**Pros:** strongest platform integration and the best path for reliable device APIs,
push, secure storage, NFC, background work, and polished app-store presence. **Cons:**
highest build/test/release overhead, two platform lifecycles, store review and account
operations, mandatory upgrade planning, and a larger security/support surface.

**Accepted direction:** deliver a responsive experience from phone through desktop plus
an installable PWA for the first pilot, with a deliberately small encrypted offline punch
queue and server-side conflict review. Validate iOS/Android behavior for geolocation,
camera, QR, NFC, kiosk, push, and 12-hour synchronization. Move to native/cross-platform
packaging when failed capability tests or commercial app-store expectations justify the
cost. The backend API must not depend on the choice.

## Email and SMS providers

Required email adapters are Mailjet and Amazon SES. Strong candidates for one additional
adapter are:

- [Postmark API](https://postmarkapp.com/developer), focused on transactional email;
- [Mailgun Email API](https://www.mailgun.com/products/send/email-api/); and
- [Twilio SendGrid Email API](https://sendgrid.com/en-us/solutions/email-api).

**Accepted direction:** add Postmark as the third adapter because the first-release need
is transactional messaging, but implement a strict provider interface so Mailgun or
SendGrid can be added without changing notification workflows. One configured provider
per installation matches the accepted requirement.

Amazon SES sends email; it does not provide the requested text-message channel. AWS
publishes SMS delivery through [Amazon SNS](https://docs.aws.amazon.com/sns/latest/dg/sns-mobile-phone-number-as-subscriber.html).
A common independent alternative is the [Twilio Messaging API](https://www.twilio.com/docs/messaging).
Both Amazon SNS and Twilio are approved adapters; a deployment configures one active SMS
provider. Treat SMS as a separate provider interface with consent, opt-out/help
processing, quiet-hour/timezone policy, number validation, delivery callbacks, rate/cost
controls, and message-category rules. Do not assume email unsubscribe rules and
mandatory operational SMS rules are identical.

## Notification event catalog

Create notifications from durable domain events rather than sending inside request
handlers. Initial categories should cover:

- invitations, identity linking, password/MFA recovery, suspicious access, and role
  changes;
- onboarding tasks, document requests/signatures, retention review, and legal holds;
- credential/training due dates, invalid states, evidence rejection, overrides, and
  escalations;
- schedule publication/change/cancellation, open shifts, swaps, time off, late/no-show,
  and post-order acknowledgment;
- punch confirmation/exceptions, offline-sync conflicts, missed punches, corrections,
  approval, pay-period lock/reopen, and payroll-export readiness;
- import completion/errors, storage or provider failures, backup/restore results, and
  security/administrative alerts.

Each event defines audience and scope, available channels, urgency, deduplication key,
retry policy, template/version, localization/timezone, acknowledgment needs, and audit
record. Administrators may brand and edit safe template content but cannot remove
mandatory facts or inject arbitrary executable HTML.

## Confirmed recommendation outcomes

- Invalid credential states block both scheduling and clock-in by default.
- Authorized client/site policies may strengthen or weaken the global clock-evidence
  baseline, with the effective policy recorded in the audit trail.
- Responsive PWA-first delivery is approved for phone-through-desktop use.
- Postmark is the third email adapter; Amazon SNS and Twilio are approved SMS adapters.
- Rounding is configurable, preserves exact punches, and defaults to no rounding. The
  product owner's reference example is the [Hubstaff time-clock rounding guide](https://hubstaff.com/time-tracking/time-clock-rounding);
  legal acceptance must follow the applicable official wage-and-hour rules rather than
  treating a vendor article as authority.
- Initial recovery targets are RPO 5 minutes and RTO 1 hour.
- Authorized personal-data redaction leaves a tamper-evident audit tombstone.
