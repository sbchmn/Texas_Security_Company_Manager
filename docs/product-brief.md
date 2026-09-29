# Initial product brief

**Status:** Discovery draft; not yet an implementation specification  
**Market:** Texas private-security companies  
**Initial operator and compliance approver:** Product owner

## Product vision

Build a Texas-only workforce and compliance platform that meets or exceeds the useful
workforce-management capabilities of Homebase's Pro offering without copying its
implementation. The first installation serves one company, but the domain model,
authorization boundaries, configuration, and commercial metering must preserve a clean
path to a multi-tenant SaaS offering.

The initial deployment must not hard-code the first company's policies, branches, or
branding. Company-specific behavior belongs in configuration or effective-dated policy
records. Each organization can present a modern, professional, accessible identity from
phone through desktop using its company name, logo, controlled theme, platform subdomain,
and an optional verified custom hostname.

## Confirmed first-release outcomes

1. **Human capital/resource management (HCRM):** employee directory, personnel records,
   document retention, and Texas credential/compliance data for the company and its
   guards and other covered personnel.
2. **Time and attendance:** time capture, timesheets, multiple sites, location-gated
   clock-in/out, configurable standard and custom rounding policies, review/correction,
   approval, and weekly payroll reports for use in an external payroll system.
3. **Responsive web experience:** management and worker workflows usable across modern
   desktop and mobile browsers.
4. **Responsive PWA:** an installable, responsive client spanning phone through desktop,
   connected to the same authenticated API and authorization model, with an encrypted
   offline time-clock workflow. Native packaging remains a future option.
5. **Texas private-security compliance:** company- and worker-level credential,
   training, expiration, evidence, alert, and eligibility workflows. The product owner
   is the final business approver for interpretations, with primary-source research and
   qualified professional review retained as release safeguards.
6. **Outbound transactional email:** a provider-neutral email interface with Mailjet,
   Amazon SES, and Postmark. SMTP is not part of the required delivery path. Amazon SNS
   and Twilio provide the separate, configurable SMS adapters.
7. **Greenfield onboarding:** no legacy migration is required for the first customer;
   a documented CSV template and validated import workflow should support bulk setup.

## Covered personnel

- noncommissioned/unarmed security officers;
- commissioned/armed security officers;
- personal protection officers;
- private investigators; and
- company shareholders.

The precise Texas license, registration, endorsement, training, employment-association,
and renewal rules for each category must be captured in the compliance control matrix.

## Tenancy and scale direction

The near-term operating mode is **one company per deployment**. It must allow an
operationally unrestricted number of branches, guards, sites, and shifts, subject only
to documented technical capacity—not arbitrary product limits.

Future subscription tiers may meter those resources. Consequently:

- core records should be organization-scoped from the start;
- authorization must prevent cross-organization access even before shared SaaS hosting;
- product entitlements and quotas must be separate from business records;
- identifiers and uniqueness constraints must not assume one global company; and
- no first-release screen needs to expose multi-company administration unless required
  for testing or platform operations.

"Unlimited" is a commercial behavior, not a promise of infinite capacity. Measurable
load targets and graceful operational limits still need to be selected.

## Deployment and infrastructure direction

- Support a production Docker image and Compose topology operated by the product owner.
- Support the same application on DigitalOcean App Platform, with workers added when
  asynchronous workloads justify them.
- Prefer MySQL as the relational system of record.
- Support object storage through an S3-compatible abstraction when document workflows
  require it.
- Minimize mandatory services initially. Begin with the web/API process, MySQL, and
  required storage/email services; add cache, queue, search, and dedicated workers
  behind explicit interfaces only when load or reliability requirements justify them.
- Keep configuration environment-driven and portable between Compose and DigitalOcean.

The architecture decision record must still settle job execution, scheduled reminders,
virus scanning, report generation, backups, and local object storage; these cannot be
removed merely to reduce the service count.

## Identity and access direction

- Follow current application-security best practices and require auditable,
  least-privilege authorization.
- Plan OpenID Connect sign-in for Microsoft Entra ID and Google.
- Allow a person to link approved personal and organizational identities to one worker
  account without merging tenants or allowing an email-address match alone to confer
  access.
- Define local-account recovery, MFA, session, device, support-access, and privileged
  action policies before implementation.

## Explicitly deferred integrations

- Payroll calculation, tax filing, paystub generation, and check issuance are not in
  the first-release boundary.
- Weekly payroll reports are in scope.
- Paystub automation and a possible OnlineCheckWriter integration are research items;
  no integration should be promised until its current API, authentication, commercial
  terms, supported workflows, and sandbox are verified.
- Outbound email is the only confirmed launch integration.

## Product principles

1. **Configuration over customization:** support company policies without forks.
2. **Evidence over checkboxes:** compliance decisions retain source, actor, timestamp,
   rule version, supporting evidence, and override reason.
3. **Warn and block deliberately:** every compliance control explicitly defines
   whether it records, warns, blocks, or permits an audited override.
4. **Provider portability:** email, object storage, identity, and future payroll
   integrations sit behind narrow application-owned interfaces.
5. **Progressive infrastructure:** do not require distributed components before they
   solve a measured need, but preserve clean extraction points.
6. **API parity:** responsive web and future mobile clients enforce the same server-side
   business and authorization rules.

## Remaining detailed design work

- the exact Homebase Pro capability comparison and acceptance checklist;
- the complete Texas legal/control mapping and retention schedule;
- precise offline conflict-resolution and anti-spoofing acceptance tests;
- approved overtime and rounding policy definitions;
- client portals and fine-grained permission assignments;
- backup topology and evidence that RPO 5 minutes/RTO 1 hour are met; and
- subscription packaging and metering rules.
