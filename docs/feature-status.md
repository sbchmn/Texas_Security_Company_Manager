# Feature implementation status

**Status date:** 2026-09-29

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
