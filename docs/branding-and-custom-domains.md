# Branding and custom-domain requirements

**Status:** Accepted product direction with implementation defaults  
**Research date:** 2026-09-29

## Goal

Each company should experience the product as its own professional workforce portal
without weakening accessibility, security, upgradeability, or tenant isolation. Branding
is configuration scoped to an organization; it is not a fork of application code.

## Brand profile

An owner or authorized administrator can configure:

- legal company name and optional public/display name;
- square mark, horizontal logo, browser/app icon, and email-header logo;
- primary, secondary, accent, surface, and text colors;
- light and dark theme assets where supplied;
- support name, email, phone, website, mailing address, and legal/footer text;
- login-page welcome text and approved background/hero image;
- default locale, timezone, date/time format, and company terminology; and
- email sender display name, reply-to address, and verified sending-domain status.

The initial product name remains visible in required legal, security, or support contexts
unless a later subscription entitlement explicitly permits full white labeling.

## Theme system and guardrails

Use semantic design tokens rather than allowing arbitrary CSS. CSS custom properties are
a standards-based implementation option; see [MDN's custom-property guidance](https://developer.mozilla.org/en-US/docs/Web/CSS/Guides/Cascading_variables/Using_custom_properties).
The same token contract should drive the web/PWA shell, component states, printable
reports, and HTML email where client support permits it.

The brand editor must provide a phone/tablet/desktop and email preview, contrast checks,
validation, draft/publish, version history, rollback, and a reset to the safe default
brand. Published changes record actor and time in the audit stream.

Do not permit tenant-supplied JavaScript, raw CSS, arbitrary HTML, remote font code, or
unrestricted external asset URLs. Those capabilities create cross-site scripting,
tracking, performance, and support risks. Curated typefaces and bounded layout choices
can be added later.

Customization cannot override essential interaction states, obscure focus indicators,
remove required compliance/legal content, or make text unreadable. The UI target is
[WCAG 2.2 AA](https://www.w3.org/TR/WCAG22/), including contrast, keyboard access,
visible focus, reflow, target sizing, errors, and accessible authentication. Logos and
meaningful images require useful alternative text under the
[non-text content guidance](https://www.w3.org/WAI/WCAG22/Understanding/non-text-content.html).
Decorative brand art uses empty alternative text.

## Asset handling

Brand assets use the same secure upload pipeline as personnel documents but a separate
public-derivative workflow:

- accept PNG, JPEG, and non-animated WebP initially; accept SVG only after sanitization;
- enforce pixel, decoded-size, file-size, and aspect-ratio limits;
- strip metadata, scan the original, and create server-controlled derivatives;
- never serve the original upload as executable inline content;
- store a content hash and version, and use immutable cache keys for derivatives; and
- provide a crop/padding preview so logos remain legible at small sizes.

Suggested defaults are 5 MiB per source image, a 4096-by-4096 maximum, and generated
favicon, square, horizontal, email, and social/share variants. These are operational
defaults rather than regulatory standards.

## Company URLs

Support two levels:

1. a platform-provided URL using a unique, immutable organization slug, such as
   `company.platform.example`; and
2. an optional verified custom hostname, such as `portal.security-company.example`.

Display-name changes must not silently change the immutable slug. Aliases and redirects
are explicit, audited records. Reserved words, Unicode/confusable names, uniqueness,
and subscription entitlement are validated centrally.

Custom-domain activation follows a state machine: requested, verification pending,
verified, certificate provisioning, active, failing, suspended, and removed. Require DNS
ownership proof, show exact DNS instructions, periodically revalidate control, provision
TLS automatically, redirect HTTP to HTTPS, and alert before or after validation and
certificate failures. DigitalOcean App Platform supports custom domains and DNS-based
validation as described in its [domain-management documentation](https://docs.digitalocean.com/products/app-platform/how-to/manage-domains/).
Certificate automation should follow the operational guidance in the
[Let's Encrypt integration guide](https://letsencrypt.org/docs/integration-guide/).

The application must resolve an allowed hostname through a verified domain-to-
organization mapping. It must reject unknown hosts and must never grant tenant access
from an untrusted `Host` header, URL slug, logo, or email domain alone. Authorization
continues to require the authenticated person's organization membership and scoped role.

## Authentication implications

A custom hostname does not create a separate identity boundary. Google and Microsoft
OpenID Connect callbacks should terminate on a stable platform-controlled callback
origin wherever provider rules allow, then return the user to a validated organization
URL using short-lived, integrity-protected state. Provider issuer discovery follows the
[OpenID Connect Discovery specification](https://openid.net/specs/openid-connect-discovery-1_0.html).
Local and federated sign-in must follow defensive authentication practices such as those
summarized in the [OWASP Authentication Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html).

Never generate callback or password-reset destinations directly from an unvalidated
request host. Account linking still requires authenticated proof of both identities and
cannot infer membership from matching addresses or branding domains.

## Email branding and domains

HTML email inherits the published brand profile through safe, provider-neutral template
variables. It must include a plain-text alternative and remain usable when images are
blocked or dark mode changes colors.

A branded sender address requires provider-specific domain verification and authenticated
email setup. Verification state belongs to the selected outbound provider configuration;
it is distinct from verifying the portal's custom hostname. Until verification succeeds,
the deployment uses its approved default sender while retaining the company's display
name and reply-to policy.

## SaaS readiness

Brand profiles, assets, slugs, hostnames, verification challenges, certificate state,
email-domain state, and theme versions are organization-scoped records. Product
entitlements determine whether a company may configure custom domains or full white
labeling, but entitlement logic remains separate from the records themselves.

Platform operators need a cross-tenant domain-conflict view without receiving routine
access to personnel or compliance data. Removal, reassignment, and tenant offboarding
must revoke routing, certificates, cached assets, and provider verification safely.

## Acceptance baseline

A branding implementation is acceptable when:

- an authorized admin can preview, publish, roll back, and reset a valid theme;
- a bad color selection cannot publish if it violates required contrast;
- the UI reflows from a small phone viewport through desktop without loss of function;
- uploaded assets cannot execute active content and have accessible alternatives;
- an unknown or spoofed hostname cannot select or access an organization;
- a verified custom hostname receives automated HTTPS and routes only to its mapped
  organization;
- identity callbacks and password-reset links cannot be redirected through an attacker-
  controlled host;
- emails render with safe branding plus a usable text fallback; and
- configuration, domain, asset, and publication changes are auditable.
