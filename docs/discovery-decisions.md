# Round 2 discovery decisions

**Status:** Accepted discovery input; detailed acceptance criteria still required  
**Decision owner:** Product owner

This document records the product owner's answers to questions 17–40 and the final
follow-up confirmations. Discovery decisions in this document are accepted inputs for
MVP requirements and acceptance criteria.

## HCRM, records, and retention

- HCRM means **human capital/resource management**.
- Day-one records include documents required for federal employment compliance and
  Texas private-security company/personnel compliance, plus administrator-defined
  document types, uploads, templates, and acknowledgment/signature requirements.
- Retention policies are configurable per document/record category and default to
  permanent retention.
- At the end of a configured retention period, an administrator selects the action:
  review, archive, delete, or preserve under legal hold.
- Only owner or administrator roles may authorize permanent deletion. Deletion must be
  separately authorized, auditable, and prevented by a legal hold.
- A shareholder is a company-ownership relationship, not an exclusive worker type. A
  person may simultaneously hold ownership, investigator, commissioned officer,
  personal protection officer, or other roles. Ownership records must support the
  Texas-specific training and compliance requirements applicable to owners/shareholders.

A configurable document catalog must not imply that every uploaded document is legally
required. Every built-in requirement needs a legal source, jurisdiction, applicability
condition, effective date, retention rule, and owner-approved interpretation.

## Compliance status and evidence

The intended default enforcement matrix is:

| Credential state | Schedule | Clock-in | Notify | Override |
| --- | --- | --- | --- | --- |
| Missing | Block* | Block* | Yes | Configurable, documented |
| Pending | Block* | Block* | Yes | Configurable, documented |
| Unverified | Block* | Block* | Yes | Configurable, documented |
| Expiring soon | Allow | Allow | Yes | Not applicable |
| Expired | Block* | Block* | Yes | Configurable, documented |
| Suspended | Block* | Block* | Yes | Configurable, documented |
| Revoked | Block* | Block* | Yes | Configurable, documented |

`*` The product owner confirmed that these states block both scheduling and clock-in by
default. Legally prohibited work must never become permissible merely because a software
override is enabled.

- Credential and evidence data is initially entered manually; no second-person review
  is required.
- Minimum evidence follows the applicable regulatory requirement. Owners/admins may
  require additional evidence by credential, person role, branch, client, or site.
- Notification recipients include configurable owner, manager, and employee audiences.
- Multiple reminder levels, lead times, repeat intervals, delivery channels, and
  escalations are configured globally, with narrower overrides where allowed.
- Compliance evaluations retain the rule version, inputs, result, time, actor/system,
  evidence, notices sent, and any override rationale.

## Sites, geofences, clocks, and offline capture

- A client site may contain multiple posts and multiple geofences.
- Mobile patrol routes require location-gated start/end punches and gated checkpoints.
- Owner, administrator, branch manager, and field supervisor roles may review and
  approve out-of-geofence punches within their assigned scope.
- Clock evidence is configurable at global, client, and site levels. Authorized client
  and site policies may strengthen or weaken the global baseline. The resolved effective
  policy, source scope, version, and authorizing actor must be visible and auditable.
- Supported evidence options include punch location, selfie/photo, shared kiosk PIN,
  QR checkpoints, NFC checkpoints, supervisor approval, and mock-location/spoofing-risk
  signals. Registered-device binding is not required.
- Location is collected at punch and checkpoint events only, not continuously.
- The time clock works offline. Offline events must be tamper-evident, retain device and
  client timestamps, synchronize within 12 hours, and be visibly flagged until accepted
  by an owner, administrator, authorized manager, or field supervisor.
- The server remains authoritative. It must detect duplicates, impossible sequences,
  stale credentials/rules, and altered device time rather than silently accepting an
  offline punch.

## Time calculation, reports, and corrections

- Pay periods, workweek boundary, timezone, overtime rules, rounding rules, payroll
  locks, columns, grouping, and export formats are configurable.
- Reports support CSV, XLSX, and PDF and may group by employee, branch, site, and pay
  code.
- Raw punches are immutable business evidence. Calculated/rounded values, the policy
  version used, manual adjustments, approvals, and exported results are separately
  retained so a payroll total is reproducible.
- Workers may request corrections. Owners, administrators, assigned managers, and field
  supervisors may approve within their authorization scope.
- Locked periods may be reopened when configuration allows it. Reopening requires a
  reason, privileged approval, and an audit event.
- Rounding supports configurable nearest 5-, 6-, and 15-minute increments, grace periods,
  and custom policies by company/client/site. For example, under a nearest-15-minute
  policy, an 8:53 AM punch rounds to 9:00 AM. Exact worked time with no rounding is the
  safe default when no approved policy is configured.
- Rounding is applied consistently to punches rather than selectively to favor the
  employer. The UI previews examples and the aggregate effect before activation.
- Default overtime policies remain subject to the applicable approved compliance rule;
  the system must not ship a silent default that obscures time actually worked.

## Scheduling

The first release includes recurring templates, open shifts, availability, time-off
requests, shift swaps, overtime warnings, credential-aware assignments, post orders,
schedule publication, and schedule-change notifications. Shift bidding is excluded.

Assignment validation combines personnel credentials, training, role, availability,
site/client requirements, overtime policy, and the effective date/time of the shift.

## Mobile delivery

The approved first milestone is a responsive, installable Progressive Web App that is
usable from phone through desktop and includes an encrypted offline time-clock workflow.
Native packaging remains a future option if capability testing or market needs justify
it.

All form factors use the same server-side API, permissions, compliance evaluation,
audit rules, and conflict-resolution contract.

## Email, text messaging, and templates

- Amazon SES and Mailjet are required email provider adapters.
- One outbound email provider is active per installation; automatic cross-provider
  failover is not required.
- Postmark is the approved third transactional-email adapter.
- SES is an email service; Amazon SNS, not SES, is the corresponding AWS option for SMS.
  Amazon SNS and Twilio are both approved SMS adapters, with one SMS provider configured
  per installation. Consent and messaging-policy requirements remain mandatory.
- Event-driven notification coverage must be explicitly cataloged across account,
  personnel, compliance, scheduling, timekeeping, payroll, document, import, and system
  workflows.
- Administrators can edit responsive HTML templates with safe variables and text
  fallbacks. Templates inherit configured company name, logo, colors, contact details,
  and legal footer.
- Delivery, bounce, complaint, unsubscribe, and suppression events are processed and
  retained. Security and mandatory operational messages must be categorized separately
  from optional marketing messages.

## Authentication and authorization

- Local email/password, Microsoft Entra ID, and Google OpenID Connect are supported.
- MFA policy is configurable by role/user level, with secure defaults for privileged
  roles.
- A company may restrict organizational roles to an approved Entra tenant while allowing
  officers to link approved personal Google identities.
- Initial roles are owner, administrator, HR/compliance manager, scheduler/dispatcher,
  payroll approver, site supervisor, officer, and read-only auditor.
- Authorization can be scoped by organization, branch, client, and site. Identity
  linking never grants a role merely because email addresses match.

## Documents and imports

- Documents require malware scanning, encryption, version history, browser preview,
  bulk export, soft deletion/recovery, and owner/admin approval for permanent deletion.
- Storage is configurable: a mounted persistent volume may serve a Docker deployment;
  an S3-compatible adapter supports object storage and DigitalOcean deployments.
- Allowed types and size limits follow the researched secure-upload baseline and remain
  owner-configurable only within deployment safety limits.
- CSV import covers people, branches, sites, credentials, training, and schedules. It
  includes templates/versioning, dry-run preview, row-level validation, downloadable
  errors, correction, authorization checks, duplicate detection, and idempotent retry.

## Capacity and recovery

The initial performance-validation profile is 100 guards, 6 active sites, 200 punches
per day, 30 concurrent users, and 15 years of retained records. These are validation
numbers, not subscription or hard application limits.

The accepted initial recovery objectives are an RPO of 5 minutes and an RTO of 1 hour.
The deployment and backup design must state covered failure modes and demonstrate these
targets through periodic restore tests. Docker restores will be operated and tested by
the product owner. These objectives are service targets, not a promise that every
catastrophic or external-provider event can be recovered within them.

## Audit history

Credentials, compliance decisions, evidence and documents, schedules, punches, timecard
calculations/edits, payroll approvals/exports, permission and configuration changes,
identity events, imports/exports, retention actions, and overrides require an append-only
history.

Owners/admins may initiate an authorized redaction or legally required purge, but cannot
erase the audit event itself. The approved model retains a tamper-evident tombstone
recording the requester, approver, reason, legal basis, scope, and time without retaining
personal data that was legally required to be removed.

## Priority

All nine proposed slices are important. That means no slice is removed from the desired
product, not that they can safely be built simultaneously. The delivery plan should use
vertical slices: identity/audit foundation first, then personnel plus compliance, then
schedule-to-punch-to-payroll, with notifications, imports, and mobile capabilities added
alongside the workflows they enable.
