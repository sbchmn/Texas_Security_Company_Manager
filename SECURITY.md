# Security posture and review

## Current conclusion

A baseline review has been performed against the
[Django security guidance](https://docs.djangoproject.com/en/6.0/topics/security/),
[Django deployment checklist](https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/),
and the [OWASP Top 10](https://owasp.org/www-project-top-ten/). This is an engineering
review of an early foundation—not a penetration test, independent audit, compliance
certification, or statement that the application is secure for production use.

The application must not hold real personnel, identity, location, payroll, or regulatory
evidence until the open high-priority controls below are implemented and production
infrastructure is reviewed.

## Controls currently present

- Django ORM is used for implemented database access; no raw user-composed SQL exists.
- Django template auto-escaping is retained; no tenant-controlled HTML is marked safe.
  Verified by grep: no `|safe`, `{% autoescape off %}`, or `mark_safe` in the repository.
- CSRF middleware protects state-changing browser forms. The device-token offline sync
  endpoint is the one deliberate exemption; it is authorised by a signed tenant/user/device
  credential rather than by the browser session.
- Role checks and organization-scoped queries protect implemented business views, including
  per-object lookups; a verified custom hostname pins the tenant so a membership fallback
  cannot resolve a different organization on that host.
- Production secure cookies, HTTPS redirect, HSTS (one year by default), MIME-sniffing
  protection, and frame denial are enabled; missing production secret, allowed-host, or
  Redis configuration fails closed at startup.
- The Django admin is a platform surface and is restricted by source range
  (`ADMIN_ALLOWED_IPS`), which fails closed when production declares no range. Both the
  tenant login form and `/admin/login/` are rate limited.
- Client identification is proxy-aware: `X-Forwarded-For` is honoured only from peers in
  `TRUSTED_PROXIES`, so a direct client cannot forge the address that keys its throttle
  bucket, and visitors behind one proxy do not share a bucket.
- MFA (TOTP plus recovery codes) is required for configured tenant roles and for any
  staff/platform account, which has no tenant membership of its own.
  Before enrollment, exact GET/HEAD requests for the tenant theme, logo, web manifest,
  and service-worker script may render the account shell; the theme and logo retain their
  membership/login checks. Business routes remain gated. These resources must not redirect
  to enrollment: each enrollment GET replaces the pending session secret and would
  invalidate the QR already displayed. Service-worker installation preloads refuse redirects
  so a gated clock request cannot rotate that secret or cache an enrollment page as the clock.
- A restrictive Content Security Policy, Referrer Policy, Permissions Policy, and
  Cross-Origin-Opener Policy are applied by the application. Tenant theme variables are
  served through an authenticated CSS resource and the brand logo through an authenticated
  image route, so inline styles and scripts remain disallowed; the policy adds the object
  storage origin only when one is configured.
- Passwords use Django's password hashing and built-in password validators.
- Audit events reject instance and queryset update/delete through the application ORM, are
  chained per tenant by SHA-256, and are protected by MySQL `BEFORE UPDATE`/`BEFORE DELETE`
  triggers that the CI MySQL leg asserts are installed and effective.
- Destructive and privacy-sensitive actions are two-person: a record disposition must be
  executed by a different owner/administrator than its requester, and an export redaction
  must be approved by someone other than its requester before it changes any export.
- Brand colors, slugs, image type, and a 5 MiB image limit are validated; logos are
  re-encoded to PNG with EXIF stripped.
- Personnel and clock-photo images are re-encoded before they are scanned, hashed, or stored, so the
  geotag, device make/model, and camera timestamp a phone writes into a photograph do not survive into
  the record — and therefore not into the disclosure ladder, the compliance queue, or the personnel
  export ZIP. The container is preserved (`image/jpeg` in, `image/jpeg` out) so the preview allowlist
  keeps telling the truth about the bytes, and an image no decoder can read is **refused** rather than
  stored with its metadata unproven. A PDF's XMP block is not cleared: its bytes are the record.
- Uploads are extension-allowlisted, signature-checked, size-capped, malware-scanned
  (ClamAV in production, EICAR-only in basic mode), stored under generated private keys, and
  downloaded only through an authorized view with `attachment`, `nosniff`, and
  `private, no-store`.
- Payroll CSV/XLSX cells neutralise spreadsheet formula prefixes for text a person controls.
- The container image runs as a non-root application user, owns its own media directory, and
  publishes a healthcheck. Compose applies restart policies, bounded log rotation, memory
  caps, service healthchecks, and a one-shot migrator.
- Secrets and runtime data are excluded from version control and from the build context.
- Signing credentials are encrypted per organization using a key derived from `SECRET_KEY`; keys
  and bearer signing links are not written to audit/error logs. Tenant administrators can select
  only operator-allowlisted HTTPS origins. API requests refuse redirects, file downloads do not
  send the API key and can follow only same-origin redirects, and responses have explicit bounds.
- Signing completion is re-read through the authenticated DocuSeal API and bound to the reserved
  request, signer, template and required signature fields. Signed PDFs plus an audit certificate
  must pass normal validation/scanning and be committed locally before an onboarding step closes.
  A browser return, generic upload or manual completion cannot substitute for this evidence.
  MySQL guards protect signing request/artifact tenant references. This is not a legal certification
  or proof of a qualified cryptographic PDF signature; I-9/DPS workflows remain excluded.

## Common-attack review

| Risk | Current assessment | Required next action |
| --- | --- | --- |
| Broken access control / IDOR | Tenant-scoped list views, per-object routes, and cross-tenant negative tests for person, document download, import apply, disposition, payroll, and audit routes | Add branch/client/site scope enforcement and its tests — roles are organization-wide today, so a supervisor can review any punch in the tenant |
| Cryptographic failures | HTTPS/secure-cookie policy exists; MFA and offline-token signing use Django's salted signing | Define key management, encryption at rest/object storage, rotation, backups, and sensitive-field classification |
| Injection | ORM and escaped templates reduce SQL/XSS exposure; spreadsheet exports are formula-neutralised | Add adversarial tests for every API/import/template feature; never permit raw tenant HTML/CSS/JS |
| Insecure design | Discovery and gated compliance decisions are documented | Produce threat models and abuse cases for identity linking, offline punches, geolocation, uploads, custom domains, and overrides |
| Security misconfiguration | Production secrets/hosts/Redis fail closed at startup; CI gates on `check --deploy --fail-level WARNING`; Compose and App Platform specs no longer diverge on scanner, media persistence, or migration ownership | Configure trusted proxies/origins, object storage, logging, monitoring, and a reviewed production environment per deployment |
| Vulnerable components | Python dependencies are pinned; CI runs `pip-audit`, `bandit`, and a blocking Trivy image scan for CRITICAL/HIGH; an SBOM is uploaded as a build artifact | Add secret scanning and infrastructure/config scanning, pin actions by digest, and define patch SLAs |
| Authentication failures | Local authentication, Google/Entra OIDC with explicit account linking, TOTP MFA with recovery codes, throttled tenant **and** admin login paths, proxy-aware client identification | Add secure recovery, suspicious-login alerting, session/device revocation UI, and reauthentication for privileged actions |
| Integrity failures | Audit mutation is blocked in application code, hash-chained per tenant, and blocked by MySQL triggers that the CI MySQL leg asserts | Add an external immutable sink, signed/verified builds, and protected CI branches |
| Logging/monitoring failures | A basic business audit stream exists | Add structured security logs, alerting, correlation IDs, redaction, operator access audit, retention, and incident response |
| SSRF | One outbound fetch exists: an operator's click confirming an Amazon SNS subscription. The stored address must be `https`, match `sns.<region>.amazonaws.com` as its **parsed host**, carry no port but 443, no path, and name `Action=ConfirmSubscription` — checked before the socket is opened, with redirects not followed and the reply read as text only. Domain verification still performs DNS TXT lookups only, and provider callbacks are inbound-only | The allowlist is a host pattern, so a compromised DNS answer for a genuine `sns.*` name could still walk the request: add post-resolution IP-range validation (refuse link-local, metadata, and reserved addresses), egress restrictions, and a response-size ceiling. The China partition is deliberately absent and refuses rather than fetches |
| File upload attacks | Extension allowlist, magic-byte check, size caps, generated storage keys, ClamAV scanning (fail-closed when the scanner is unreachable), JPEG/PNG re-encode that drops EXIF/XMP-derived metadata before the stored bytes are hashed, authorized attachment downloads with `nosniff` and `private, no-store` | Implement asynchronous quarantine, encryption at rest, version lineage, and isolated storage/origins for active content; clear PDF XMP only if a re-write that preserves the rendered page can be proven safe |
| Host-header/custom-domain attacks | The project's own `VerifiedHostMiddleware` rejects unknown hosts before any redirect or tenant resolution, backed by DNS-verified custom hostnames; `ALLOWED_HOSTS` is intentionally `["*"]` because that middleware is the gate | Cache the hostname lookup, and add regression tests for every response class and for pending/failed domain states |
| Clickjacking | `X-Frame-Options: DENY` and CSP `frame-ancestors 'none'`, also emitted by Caddy at the edge | Add regression tests for every response class and document any future embedding exception |

## Findings fixed in the 2026-09-29 full review

1. **Shift CSV import could never run.** `core.services` used `datetime.fromisoformat` while
   importing only `timedelta`, so a `shifts` preview raised `NameError` (HTTP 500) and apply
   was caught and reported as a validation failure. Regression-tested both ways now.
2. **The login throttle keyed every visitor to one bucket.** `REMOTE_ADDR` behind Caddy is the
   proxy, so five failed guesses from any client locked out the whole deployment. Replaced with
   `TRUSTED_PROXIES`-aware client resolution, and `/admin/login/` is now throttled too (it was
   previously unlimited).
3. **`/admin/` had no source restriction** despite being the platform surface. Added
   `ADMIN_ALLOWED_IPS` with fail-closed behaviour, and MFA is now enforced for staff accounts,
   which have no tenant role to key the previous rule on.
4. **Punch evidence was under-validated.** Clock-in compliance enforcement applied only to
   punches bound to a shift, geofencing likewise, future-dated device clocks were accepted, and
   neither an unclosed clock-in nor an orphan clock-out was flagged. All four are now enforced
   or flagged for review, and a naive timestamp is rejected instead of raising `TypeError`.
5. **Self-approval on irreversible actions.** A record disposition could be executed by its
   requester, and an audit-export redaction recorded the same user as both requester and
   approver while changing exports immediately. Both are now two-person.
6. **Offline punches could be lost permanently.** The browser drained its queue in
   `client_event_id` (UUID) order while the server required ascending device sequence, so the
   first out-of-order item wedged the queue and the sync status reported success anyway.
7. **The production image could not boot from this working tree** (`entrypoint.sh` CRLF in the
   build context) and the runtime user could not write `/app/media`. Both verified fixed in a
   real build; added `.gitattributes`, the media directory ownership, and a CI guard for CRLF.
8. **Deployments diverged.** Compose had one restart policy of six, no log rotation, no
   healthcheck on `web`, no ClamAV signature volume, a MySQL healthcheck that passed during
   first-boot initialisation, and migrations run from three different places. The `.do` spec
   selected the backup-less dev database tier, shipped no media persistence, and left
   `MALWARE_SCAN_MODE` at a value that fails every upload because App Platform has no scanner.
   Backups did not exist in either topology despite being an accepted recovery objective.
9. **Documentation described controls that were not implemented.** `README.md` and
   `docs/feature-status.md` now match the code, and `docs/feature-status.md` carries an
   explicit "Not yet implemented" ledger against the accepted first-release scope.

An earlier section of this file listed fixes from the initial foundation review (production
configuration failing open, queryset-level audit mutation, browser policy headers, and the
logo size cap); those remain in place.

## Release-blocking security work

Before a production pilot with real data:

1. establish tenant-safe object access helpers and expand negative authorization tests as every new workflow is added;
2. use a production secret manager and least-privilege MySQL, cache, scanner, and object-storage credentials;
3. configure encrypted backups, restore tests, alerting, and incident monitoring in the chosen production account;
4. review SAST, dependency, SBOM, container, and infrastructure scan results on every release;
5. threat-model custom domains, identity linking, provider webhooks, regulatory overrides, and support/operator access;
6. perform an independent penetration test before general availability; and
7. obtain legal/compliance review of the Texas control matrix separately from the cybersecurity review.

## Reporting vulnerabilities

Until a private security contact is configured, do not publish exploit details in a
public issue. Contact the repository owner privately with affected version, reproduction
steps, impact, and any suggested mitigation. A formal disclosure address and response
SLA must be established before public launch.
