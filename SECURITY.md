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
- CSRF middleware protects state-changing browser forms.
- Role checks and organization-scoped queries protect implemented business views.
- Production secure cookies, HTTPS redirect, HSTS, MIME-sniffing protection, and frame
  denial are enabled; missing production secret or allowed-host configuration fails
  closed at startup.
- A restrictive Content Security Policy, Referrer Policy, Permissions Policy, and
  Cross-Origin-Opener Policy are applied by the application. Tenant theme variables are served through an authenticated CSS resource, so inline styles and scripts remain disallowed.
- Passwords use Django's password hashing and built-in password validators.
- Audit events reject instance and queryset update/delete through the application ORM.
- Brand colors, slugs, image type, and a 5 MiB image limit are validated.
- The container image runs as a non-root application user.
- Secrets and runtime data are excluded from version control.

## Common-attack review

| Risk | Current assessment | Required next action |
| --- | --- | --- |
| Broken access control / IDOR | Basic role and tenant-scoped list/create paths tested | Centralize tenant context; test every object lookup and mutation; add branch/client/site scope tests before those features ship |
| Cryptographic failures | HTTPS/secure-cookie policy exists; no field-level sensitive-data design yet | Define key management, encryption at rest/object storage, rotation, backups, and sensitive-field classification |
| Injection | ORM and escaped templates reduce SQL/XSS exposure | Add static analysis and adversarial tests for every API/import/template feature; never permit raw tenant HTML/CSS/JS |
| Insecure design | Discovery and gated compliance decisions are documented | Produce threat models and abuse cases for identity linking, offline punches, geolocation, uploads, custom domains, and overrides |
| Security misconfiguration | Production secrets/hosts fail closed and deployment check passes | Execute container/IaC scanning, configure trusted proxies/origins, object storage, logging, monitoring, and production environment review |
| Vulnerable components | Dependencies are pinned | Automate dependency update and vulnerability scans; define patch SLAs and generate an SBOM |
| Authentication failures | Django local authentication exists | Add rate limiting, MFA, secure recovery, session/device controls, OIDC validation, reauthentication, and suspicious-login alerting following the [OWASP authentication guidance](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html) |
| Integrity failures | Audit mutation is blocked in application code | Add database permissions/triggers or external immutable sink, hash chaining/signing, verified builds, protected CI, and restore validation |
| Logging/monitoring failures | A basic business audit stream exists | Add structured security logs, alerting, correlation IDs, redaction, operator access audit, retention, and incident response |
| SSRF | No URL-fetching feature is implemented | Apply URL allowlists, DNS/IP validation, redirect limits, egress restrictions, and metadata-address blocking before webhooks/previews |
| File upload attacks | `ImageField` validation and size cap exist | Implement quarantine, signature/MIME checks, metadata stripping, image re-encoding, malware scanning, isolated storage, authorization, and safe download headers per the [OWASP file-upload guidance](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html) |
| Host-header/custom-domain attacks | Django host allowlist exists; custom routing is not live | Implement verified hostname mapping and stable trusted callback origins before enabling custom domains |
| Clickjacking | `X-Frame-Options: DENY` and CSP `frame-ancestors 'none'` | Add regression tests for every response class and document any future embedding exception |

## Review findings fixed in this change

1. **Production configuration failed open:** an unsafe default secret and development
   hosts could be used with `DEBUG=false`. Production startup now requires an explicit
   secret and allowed-host list.
2. **Audit immutability was incomplete:** model-instance methods did not prevent
   `QuerySet.update()` or `QuerySet.delete()`. The audit queryset now rejects both, with
   tests. Database administrators can still change rows, so database/external controls
   remain required.
3. **Browser policies were incomplete:** CSP, Permissions Policy, Referrer Policy, and
   Cross-Origin-Opener Policy are now emitted and tested. The policy follows the approach
   in the [OWASP CSP guidance](https://cheatsheetseries.owasp.org/cheatsheets/Content_Security_Policy_Cheat_Sheet.html).
4. **Logo size was unbounded:** brand images now have a 5 MiB application limit. This is
   only an early resource-abuse control, not the final secure-upload pipeline.

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
