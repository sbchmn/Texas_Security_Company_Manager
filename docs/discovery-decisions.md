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

**Ruled on 2026-10-05 — approval gates enforcement.** The table above describes a requirement the
company has *adopted*: a rule with its source, its section reference, its interpretation, and a named
approver on the row. An unapproved requirement is a draft, and a draft must not be the reason a guard
cannot clock in or cannot be assigned — the firm could not say where the refusal came from, and the
same word "approved" has to mean the same thing on both halves of the control matrix.

Consequence, and it is the part that had to be decided rather than assumed: **this does not apply
retroactively by switching existing rows off.** A requirement that has been refusing clock-ins since
before the approval columns meant anything keeps refusing, and is labelled **"grandfathered"** on the
matrix so the people who own the obligations can see the list and clear it — by approving each row or
by turning its enforcement flags off. Silently de-enforcing on upgrade day would have taken work
arrests away from firms relying on them, with no notice and no name attached. New requirements created
from the settings screen carry no such escape: they gate nothing until approved.

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

**Ruled on 2026-10-04 — the selfie (CLK-1).** The three questions this section carried as open were
answered by the owner the same day, and each answer has a build consequence:

1. **Retention is configurable by an owner or administrator, from permanent to a set number of days.**
   That is the shape `DocumentType.retention_days` already has — a nullable integer where blank means
   permanent, edited on the compliance settings page, which renders blank as "Permanent" and shows the
   reader list beside it. So this answer asks for no new field and no new screen; what it asks for is
   that the selfie's own record type be *there and editable*, like any other.
2. **A frame is owed at clock-in and clock-out, "if other methods weren't used", and not at breaks.**
   Two consequences. It is one boolean rather than a per-event matrix, because breaks are annotations on
   a tour somebody is already paid for, not arrivals anyone needs to witness. And the clause *if other
   methods weren't used* makes the requirement conditional on the absence of rival evidence: a punch
   already identified by a verified station PIN, or attested by a checkpoint scan, has its own answer to
   "who was there", and asking for a face on top of it is friction that proves nothing. So the rule is
   "selfie required unless another identity method covers this punch" — stated, not implied, and
   testable in both directions.
3. **Subject, owner, administrator, HR *and dispatcher* may open a stored selfie.** This is the answer
   that cannot be implemented by reusing an existing rung, and the reason is worth recording: the
   current `restricted` staff list is owner/administrator/HR, so putting biometrics there would either
   leave the dispatcher out or — if `restricted` were widened instead — silently disclose every existing
   workers'-compensation claim, accommodation file, leave record and screening result to dispatch too.
   Those are different disclosures decided by different people. The ruling therefore needs its own rung
   beside the other two, whose staff list is exactly the four roles named, and which the subject can
   still open because a biometric about you is not sealed from you.

Supervisor approval — the third unbuilt evidence option in the list above — is still open and is a
different thing: it needs a decision record, not a camera.

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
- **Approved leave is paid on the hours it displaced** *(ruled 2026-10-03)*. A leave row is
  priced on the officer's own scheduled post hours that fall inside the approved span, at
  that post's resolved pay rate and the firm's leave pay-category rule. The calendar span an
  absence covers is **not** a payable figure and must not be multiplied by a rate. Where the
  period carries no scheduled posts for that person, the row states that there is no basis
  rather than inventing one — the same rule that keeps a missing punch from becoming a claimed
  fact.

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

## Rulings taken on 2026-10-03

Four questions that were blocking roadmap items, answered by the product owner. Recorded here
because each one is a *requirement* now, not a preference, and each has a build consequence.

- **Payroll lock granularity — a partial-lock child object, not a per-branch run.** One run stays
  one period for the company (the run screen, the exports, the approval and the reopen path keep
  their current shape), and a lock segment is added beneath it per branch or contract, so a firm can
  lock the branches it has paid and keep working the ones it has not. Consequence: a run's status is
  no longer the whole answer to "is this period locked", so every gate that reads
  `PayrollRun.status` — `record_punch`, the correction approval, the export — has to consult the
  segment that actually covers the row it is touching, and reopening one segment must not silently
  unlock the others.
- **The Texas control matrix — drafted from the statutes, approved row by row.** I read §1702 and
  37 TAC Ch. 35 and enter each obligation with its proposed value, its authority URL and reference,
  and its interpretation text; **nothing becomes active until the owner approves that row**, which is
  the `approved_by` / `approved_at` mechanism the matrix already has. Consequence: the product still
  ships no jurisdiction's numbers unapproved, and the draft is reviewable work rather than encoded
  law. Legal review remains a production gate before any of it is relied on.
- **Document signing — self-hosted free build for a single company; more than one tenant means the
  licensed Pro edition, still on-premises.** The community edition is one DocuSeal account per
  installation, so a deployment serving more than one `Organization` licenses Pro rather than
  splitting one workspace between tenants. Consequence: the application-side integration must be
  written against a signing *backend* chosen per installation — token, base URL, template ids from
  tenant settings rather than environment alone — so that moving to Pro is a credential change and
  not a schema change. It does not license away the local system of record: signed bytes still come
  back here and are stored as a revision, because the secrecy ladder, the retention rules, the legal
  hold and the audit chain all live in this application.
- **Leave is paid on the hours it displaced** — recorded in §Time calculation above, since it is a
  pay rule rather than a scheduling one.
- **Signing MVP: templates authored in DocuSeal, explicit staff sending, one request per onboarding
  step.** The owner selected these three boundaries during implementation. Checklist provisioning
  never sends documents automatically. A single-signer template maps to one subject-readable
  personnel record type; its signed PDFs and audit certificate are retained locally and verified
  before the step completes. Combined multi-step packets, in-app template authoring, I-9 and DPS
  workflows are excluded from the first slice. Completion is authenticated polling, not an
  unverified webhook or browser-return assertion.

### Rulings taken later the same day

- **Clock evidence: selfie, shared kiosk with PIN, and the spoof-risk signal. Not NFC.** Of the seven
  options §Sites lists, the launch set is CLK-1, CLK-2 and CLK-4. The ordering consequence is real: a
  selfie is personal-biometric data, so it inherits the `DocumentType.sensitivity` ladder and the OWASP
  upload rules already in the codebase, while the kiosk PIN adds no storage at all and is the biggest
  friction reduction for a firm whose guards share one phone at the gate. NFC stays unbuilt because QR
  checkpoints already close the tour-verification promise end to end.
- **Audit retention: seal the period, then purge.** The chain head is computed and stored per period
  (with the row count and the digest), after which events older than `audit_retention_days` may be
  deleted without destroying verifiability — the seal is what proves what the chain said before the
  rows went. Growing storage forever was never the requirement; a purge that quietly broke the chain
  would have been worse.
- **SMS: build the provider ingest now, and capture consent at the account's own start.** Consent is
  taken **at sign-up, registration or first login**, and additionally from the user's own profile page
  so owners, HR and the rest can opt in afterwards. The record is per *person holding an account in
  this product*, scoped per organization, and it is a precondition on send rather than a preference
  flag: nothing goes to a number until that person has affirmatively opted in, and an opt-out ends the
  send path immediately. Bounce, complaint, unsubscribe and suppression callbacks are ingested and
  retained alongside it, since the opt-out arrives as a callback and nowhere else.
- **Browser preview: yes — permission to read is permission to view.** If the reader's own access rule
  (`record_readable`, the sensitivity ladder, the authority scope) lets them open a document, there is
  no reason to make them download it to read it; render it inline, or in a modal behind a thumbnail on
  the page they are already on. **This supersedes the standing "uploads are never served inline"
  posture**, which is why it is written down rather than left to whoever reads the code: it is a
  serving-model change, and the mitigations travel with it — `Content-Disposition: inline` only for a
  reader the rule already admits, never on a guessed or leaked URL; content-type allowlisting instead
  of sniffing; a sandboxed CSP for the previewed bytes with no scripting and no same-origin framing; and
  the same audit event the download writes. Treat it as a security-review item, because the reason
  inline serving was refused in the first place was XSS and drive-by-download risk on uploaded files.
