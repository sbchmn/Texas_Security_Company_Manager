# Feature implementation status

**Status date:** 2026-09-29 (active MVP build)

## Executive status

The product is **not feature complete**. The first foundation/vertical slice is runnable
and tested, but the workforce, regulatory-compliance, timekeeping, payroll-reporting,
notification, import, SSO, custom-domain, and offline-clock capabilities are not yet
complete. A visible placeholder is not an implemented feature.

## Implemented and exercised

| Area | Current capability | Evidence |
| --- | --- | --- |
| Application foundation | Django application, environment configuration, MySQL production connection, SQLite developer fallback | Deployment check and test suite |
| Deployment artifacts | Non-root production image, Compose web/MySQL topology, App Platform specification, health endpoint | Static validation; Docker runtime remains unverified in this environment |
| Identity | Local username/password sign-in and sign-out | Integration tests and browser smoke test |
| Tenant foundation | Organization-scoped memberships, roles, branches, people, brand records, and domains | Migrations and tenant-isolation test |
| Authorization | Owner/admin and manager role gates on implemented views | Authorization tests |
| Directory | List and create people with branch, status, and overlapping officer/PPO/PI/shareholder flags | Integration test and browser smoke test |
| Branches | Create a branch within the active organization | Audited transactional view |
| Branding | Company display name, logo, colors, support details, and timezone | Validation and audit integration test |
| Audit foundation | Append-only application model for audited implemented mutations | Instance and queryset mutation tests |
| PWA shell | Responsive navigation, manifest, icon, service-worker shell cache | Desktop and 390px browser smoke tests |
| Baseline browser security | CSRF middleware, template escaping, clickjacking denial, secure cookies in production, HTTPS/HSTS settings, restrictive response headers | Django deployment check and header test |

## Partially implemented

| Area | Present | Still required |
| --- | --- | --- |
| Multi-tenancy | Organization keys and scoped query paths | Tenant selection, shared-SaaS administration, verified-host routing, pervasive automated isolation tests, database defense in depth |
| Branding | Basic logo and color editing | Contrast enforcement, image transformation/scanning, versions/rollback, dark mode, custom hostname workflow, email branding |
| Personnel/HCRM | Basic directory fields | Full employee profile, custom fields, documents, signatures, acknowledgments, retention/legal hold, history, imports |
| Audit | Append-only ORM path and captured mutations | Database-level append-only controls, hash/tamper evidence, redaction tombstones, export/retention, coverage of every future mutation |
| PWA/offline | Installable shell and static fallback | Authenticated offline time-clock queue, encryption, replay/conflict controls, checkpoint/geofence workflows |
| Deployment | Definitions and production server | Executed image/Compose test, managed storage, backup/restore proof, RPO/RTO evidence, worker topology, observability |

## Not implemented yet

- organization invitations, provider-domain administration, and advanced session/device management (Google/Microsoft OIDC, TOTP/recovery-code MFA, account linking, and login throttling are implemented);
- clients, sites, posts, geofences, patrol routes, QR/NFC checkpoints, post orders, and
  client/site scoped configuration;
- schedules, recurring templates, availability, leave, open shifts, swaps, publication,
  notifications, overtime warnings, and credential-aware assignment;
- online/offline punches, kiosk PIN, selfie proof, spoofing-risk signals, corrections,
  approvals, rounding engine, timecards, pay-period locks, and payroll exports;
- the source-cited Texas compliance control matrix, credentials, training, evidence,
  expiry/status evaluation, enforcement, alerts, overrides, and inspection packets;
- HCRM document storage, malware scanning, preview, versions, e-signature/acknowledgment,
  granular retention, legal hold, and permanent-deletion workflow;
- provider delivery/bounce/complaint webhooks and SMS consent/suppression administration (CSV imports, notification outbox/retry, compliance reminders, and Mailjet/SES/Postmark/SNS/Twilio outbound adapters are implemented);
- custom hostname DNS verification, certificate lifecycle, allowed-host routing, and
  branded email-domain verification;
- subscriptions, entitlements, quotas, billing, and platform-operator tooling; and
- native application packaging (intentionally deferred behind the responsive PWA).

## Meaning of “tested”

Current tests cover the foundation only. They do not certify Texas regulatory
compliance, payroll correctness, production capacity, accessibility conformance,
penetration resistance, disaster recovery, or any unimplemented feature. Those require
dedicated traceable test suites and, where appropriate, independent professional review.
