# Feature implementation status

**Status date:** 2026-09-29

## First vertical slice

The recommended foundation slice is implemented end to end: idempotent bootstrap creates
an organization, immutable slug, owner, and branch; owners and administrators can issue
audited, single-use, 72-hour invitations; branding supports safe image processing,
contrast validation, preview, publication history, and rollback; branches and personnel
are tenant-scoped; custom personnel documents are validated, malware-scanned, stored
through Django's storage abstraction, and audited; audit records are append-only and
hash-chained; the responsive shell is installable and includes keyboard/reduced-motion
accessibility safeguards; and the same image/configuration contract is represented in
Compose and the DigitalOcean App Platform specification.

This is completion of the deliberately thin foundation slice, not completion of every
first-release workflow or any external production certification. The acceptance gates
below remain in force.

## Confirmed first-release outcomes

The seven confirmed software outcomes are now represented end to end:

1. HCRM includes personnel, custom fields, employment history, private records,
   acknowledgments, retention/disposition, credentials, and training.
2. Time and attendance includes online/offline capture, multiple geofenced sites,
   immutable correction evidence, configurable workweeks and rounding, approval/lock,
   and CSV, XLSX, and PDF payroll exports.
3. Management and worker workflows share a responsive, keyboard-accessible web shell.
4. The installable PWA uses an encrypted offline queue and the same server authorization
   and compliance enforcement as online punches.
5. Texas compliance configuration retains jurisdiction, primary-source URL/reference,
   effective dates, owner-approved interpretation, evidence policy, warning window, and
   separate scheduling/clock enforcement controls. Actual Texas rules must still be
   entered and approved under the legal gate below; the product does not invent them.
6. Transactional delivery supports Mailjet, Amazon SES, and Postmark for email and Amazon
   SNS or Twilio for SMS. Invitations are queued through the same durable notification
   path, including recipients who do not have an account yet.
7. Bulk setup supports downloadable templates, dry-run validation, downloadable row
   errors, duplicate-safe application, and all accepted entity types.

## Completed foundation capabilities

| Area | Implemented capability |
| --- | --- |
| Multi-tenancy | Explicit tenant selection with session rotation, membership enforcement, verified custom-host routing, platform superuser overview, tenant-scoped form/query paths, cross-tenant tests, model validation, and MySQL reference-integrity triggers for high-risk records. |
| Branding | WCAG-oriented contrast validation, scanned and normalized PNG uploads, immutable brand snapshots and rollback, automatic dark palette, DNS TXT hostname verification, and HTML email branding. |
| Personnel/HCRM | Extended employment/contact profile, typed custom fields, sensitive-field visibility controls, change history, private documents, signed acknowledgments bound to a document hash, imports, granular retention, legal holds, archive/delete approvals, and immutable disposition tombstones. |
| Audit | Application append-only controls, per-tenant SHA-256 hash chains, verification UI, MySQL update/delete rejection triggers, separately approved export-redaction tombstones, NDJSON export, and an organization retention floor. Audit events themselves are never purged. |
| PWA/offline clock | Responsive installable shell, IndexedDB queue encrypted with a non-extractable WebCrypto AES-GCM key, signed 30-day tenant/user/device credentials, monotonic replay protection, idempotent retries, 12-hour synchronization limit, and site-bound checkpoint/geofence validation. |
| Identity/security | Local auth, Google and Microsoft OIDC/account linking, TOTP/recovery-code MFA, configurable role MFA, login throttling, CSRF, restrictive browser headers, secure production settings, dependency/static/container scanning CI, Redis sessions/cache, and S3-compatible private media support. |

## Wider product capability already present

Clients/sites, credentials and training, credential-aware scheduling, post orders, punch review and corrections, configurable rounding, payroll approval/locking/export, provider-backed email/SMS notifications, automated compliance reminders, and validated CSV imports are implemented as first-release workflows.

## Production acceptance gates

“Implemented” does not equal an external certification. Before handling real employee or regulatory data, an operator must complete environment-specific TLS/custom-domain setup, provider webhook/credential setup, backup-and-restore and RPO/RTO exercises, MySQL trigger verification, accessibility and capacity testing, and an independent security/legal review. Texas compliance rules remain subject to owner-approved legal interpretation. Native iOS/Android packaging and subscription billing remain intentionally outside the PWA-first MVP.
