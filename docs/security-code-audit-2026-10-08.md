# Employee-data security code audit

**Date:** October 8, 2026  
**Repository baseline:** `db72ecad3bb6a3d379bad813b8654c678bb2f1ac`, including the working-tree changes present during review.  
**Method:** Static code review only. No application was started, database queried, endpoint contacted, penetration-testing tool used, or dependency advisory service consulted. Local installed framework source was read to verify the admin authentication behavior. No runtime secrets or employee records were inspected. No application code was changed.

## Assessment

The application has meaningful safeguards, but several authorization paths undermine its protection of employee data. The most consequential finding is that the Django administration login can bypass an enrolled user's MFA challenge. Other findings allow escalation from Administrator to Owner, widen manager authority when its last grant is removed, expose document metadata across companies, and allow dispatchers to download employee photographs outside their assigned scope.

These findings establish behavior from source, not successful exploitation of a running deployment. Network exposure, configured source-address restrictions, actual roles, and stored records determine the practical impact. Address the high-impact authentication and authorization findings before entrusting this version with production employee data.

### Scoring

Scores below are **consequence ratings on a 1–10 scale**, not CVSS scores or exploitation probabilities. They summarize the data exposure and authority an attacker obtains once the stated prerequisites are met: 9–10 means potentially platform-wide compromise; 7–8 means substantial company-level exposure or privilege escalation; 4–6 means bounded disclosure or impersonation. Prerequisites are described separately so a restricted network path is not mistaken for an unauthenticated Internet exploit.

| ID | Finding | Consequence | Classification |
| --- | --- | ---: | --- |
| F01 | Django admin login bypasses the MFA challenge | 9/10 | Authentication flaw |
| F02 | Administrator can mint an Owner invitation | 8/10 | Privilege escalation |
| F03 | Removing the final scope grant grants whole-company access | 8/10 | Authorization design weakness |
| F04 | Clock-photo download and preview omit manager scope | 7/10 | Object authorization flaw |
| F05 | Operational managers can read and change sensitive personnel fields | 7/10 | Least-privilege design weakness |
| F06 | Offline credentials survive employee-access revocation | 7/10 | Revocation flaw |
| F07 | Offline identity persists across accounts and companies | 6/10 | Client identity isolation flaw |
| F08 | My documents leaks other companies' document metadata | 5/10 | Tenant isolation flaw |
| F09 | Access logs retain bearer credentials in URL paths | 7/10 | Configuration/data-handling weakness |
| F10 | Invitation-only signup setting does not close registration | 5/10 | Registration policy flaw |
| F11 | Bootstrap grants Owner to a preexisting, unverified account identity | 8/10 | Conditional privilege escalation |

## Detailed findings

### F01 — Django admin login bypasses the MFA challenge

**Evidence:** `config/urls.py:7` mounts the standard `admin.site.urls`. `core/middleware.py:166–170` requires staff/superusers to have MFA enabled, but calls only `is_mfa_enabled(request.user)`. It never establishes that the current session completed a second-factor challenge. No custom admin login or MFA-aware admin site was found.

The locally installed Django source, `django/contrib/admin/sites.py:415`, uses `LoginView` with `AdminAuthenticationForm`. The locally installed allauth adapter, `allauth/mfa/adapter.py:124–133`, answers the enrollment query by checking whether an `Authenticator` row exists. Enrollment is therefore enough to pass the middleware after a password-only admin login.

**Prerequisites and attack path:** An attacker knows a staff account's password and can reach `/admin/login/` from a source permitted by `ADMIN_ALLOWED_IPS` (or the restriction is disabled for development). If the account already has MFA enrolled, password authentication creates a session and the subsequent admin request passes the enrollment check without a TOTP/recovery-code challenge.

**Attacker impact:** Access to whatever Django model permissions the account holds. For a superuser this includes employee profiles, documents, notifications, membership management, and all companies' registered records. User/admin management also provides a route to persistence. This is not direct proof of SSN decryption: the restricted-personnel model is not registered in this admin, and its ciphertext needs the separate key.

**Recommendation:** Route admin authentication through a flow that actually challenges the second factor, or use an MFA-aware admin implementation. Require trustworthy proof of MFA completion in the current session before granting admin access. Retain the source-address restriction as an additional control. Test an already-enrolled staff user logging in with only a password; testing enrollment redirects alone misses this flaw.

### F02 — Administrator can mint an Owner invitation

**Evidence:** `core/views.py:343–363` admits both Owner and Administrator to `team()`, builds `MembershipInvitationForm` without the actor's role, and issues its selected role. `core/forms.py:9–11` offers both Owner and Administrator. `core/views.py:499–552` accepts the invitation and creates that membership. In contrast, `MembershipAccessForm` at `core/forms.py:31–44` and `membership_access()` at `core/views.py:437–466` explicitly reserve Owner grants/changes for an Owner.

**Prerequisites and attack path:** A tenant Administrator invites an account they control with `role=owner`, then accepts that invitation. A configured approved-domain rule can restrict which mailbox they use, but does not enforce the actor's right to grant Owner.

**Attacker impact:** An Administrator obtains Owner authority and can change an existing Owner's access, subject to the last-active-owner safeguard. This enables takeover/persistence and defeats the intended separation between the roles. Administrator already has extensive employee-data access; the incremental consequence is control over ownership and privileged access.

**Recommendation:** Make every role-grant path use one server-side authorization policy. Remove Owner from Administrator invitation choices and reject it at issuance regardless of the submitted form. Preserve the same check at acceptance for stale invitations, and invalidate pending grants that the issuer no longer has authority to make.

### F03 — Removing the final scope grant grants whole-company access

**Evidence:** `core/scope.py:52` sets `restricted = bool(self.rows)`. With no grants, `permits_person()` at lines 93–94 returns true and the queryset filters return the whole tenant. `core/views.py:425–434` deletes a grant without distinguishing the final grant. `core/tests.py:2531` explicitly asserts that revoking the last grant restores visibility of both branches.

**Prerequisites and attack path:** An Owner/Administrator removes a scheduler/supervisor's final grant, or provisions that role without any grants. The affected manager then uses ordinary personnel and scheduling routes to access the whole company. A bounded manager cannot directly delete their own grants through this route; this is a fail-open authorization design, not an unaided self-escalation exploit.

**Attacker impact:** The affected manager gains company-wide personnel visibility and operational authority. The word “revoke” can lead an operator to unintentionally expand a malicious or compromised user's access. The field-level permissions in F05 magnify the resulting employee-data exposure.

**Recommendation:** Represent whole-company authority explicitly. For bounded roles, zero grants should mean zero scope; revocation must only reduce access. Migrate existing intentionally unbounded managers to an explicit grant and update the test that currently requires access broadening.

### F04 — Clock-photo download and preview omit manager scope

**Evidence:** `core/models.py:1143` gives Scheduler access to biometric document types. `record_readable()` at line 1194 and `core/views.py:72–80` check role/subject sensitivity, not branch/client/site authority. `document_download()` at `core/views.py:3784–3795` and `document_preview()` at lines 3799–3839 fetch the document within the company and call this sensitivity check, but never apply `scope_for(request)` to its person.

**Prerequisites and attack path:** A Scheduler restricted to one branch/site obtains the UUID of another employee's biometric document in the same company, for example from a previously available link or a cooperating insider. They request its download or preview directly. Guessing a random UUID is not assumed feasible.

**Attacker impact:** Readable clock photographs of employees outside the Scheduler's assigned authority. The download serves actual file bytes; the preview can stream or issue a temporary signed storage URL. Tenant scoping still blocks documents belonging to another company.

**Recommendation:** Centralize a combined tenant, role, subject, sensitivity, and manager-scope decision for documents. Preserve employees' own permitted records and applicable workforce documents. Require scoped staff access to the document's person before serving bytes or issuing signed URLs. Add a direct-route test for an out-of-scope Scheduler with a known photo UUID.

### F05 — Operational managers can read and change sensitive personnel fields

**Evidence:** `MANAGERS` in `core/views.py:37–40` includes Scheduler and Supervisor. `_profile_person()` at lines 702–717 allows those roles to open an in-scope personnel record. `templates/core/person_detail.html:13–35` displays date/place of birth, home/mailing address, emergency-contact details, and hourly rate without field-level restrictions. `person_edit()` at `core/views.py:869–885` uses the complete `PersonForm`, including those fields and employment decisions (`core/forms.py:243–277`). It also exposes and updates every active custom field, including fields flagged sensitive. Separately, `personnel_file_bundle()` at `core/services.py:3833` includes `person_snapshot()` in the dossier; its `sensitive_fields` switch restricts custom fields, not the base identity/address/pay fields, so a read-only Auditor can obtain those base values via `person_export()`.

**Prerequisites and attack path:** A Scheduler/Supervisor opens an employee within their existing scope, reads the profile, or submits the profile-edit form. An Auditor exports a personnel file. No bypass or forged identifier is necessary: the permissions themselves are broad.

**Attacker impact:** Operational users can extract personal identity/contact information and wages, and modify identity, pay rate, employment status, and sensitive custom data. The dedicated SSN/driver's-license store remains separately restricted. This is a least-privilege finding; the desired field policy requires a product decision, rather than treating every currently allowed read as an unintended route bug.

**Recommendation:** Define field-level read/write permissions. Give dispatchers and supervisors the minimum scheduling/contact/qualification information, reserve HR identity and employment changes for HR/privileged roles, and limit pay data to approved audiences. Apply the policy to forms, profile responses, history, custom fields, and ZIP exports. Hiding fields in HTML alone is insufficient.

### F06 — Offline credentials survive employee-access revocation

**Evidence:** `clock_device_enroll()` at `core/views.py:3069–3076` issues a signed token. `offline_punch_sync()` at lines 3177–3197 accepts it for up to 30 days and checks only that the matching device is active. It does not check `device.user.is_active`, an active membership, the employee's current status, or that the personnel link still matches the user. `membership_access()` changes membership state without revoking devices. `record_punch()` at `core/services.py:1714` checks tenant/person/shift consistency and evidence but does not impose these access-revocation checks. No device revocation hook was found.

**Prerequisites and attack path:** An employee enrolls a browser, retains its token, then has their membership or user disabled. They submit new offline punches using that token while the device remains active. The endpoint is deliberately session-independent, so logging out does not revoke this capability. Existing shift/credential/payroll rules can still refuse particular submissions.

**Attacker impact:** Continued impersonation of the former employee for timekeeping and alteration of time/payroll evidence. This endpoint does not return a personnel dossier; its primary impact is unauthorized writes and misleading audit attribution. The 12-hour event-age limit restricts old punch timestamps, not the token's ability to authorize a fresh event.

**Recommendation:** Recheck user, membership, employment eligibility, and personnel ownership on every sync. Revoke devices when access is disabled or links change; provide deliberate device revocation and token rotation. Test a fresh punch after membership deactivation using a previously issued token.

### F07 — Offline identity persists across accounts and companies

**Evidence:** `static/js/app.js:96–104` stores one `device` and encryption key in origin-wide IndexedDB. `init()` reuses the device without comparing its user or company to the current clock page. `flush()` submits queued items with their saved bearer credentials. `templates/core/service_worker.js:5–8,37–39` clears document caches on logout/company selection, but not IndexedDB credentials or its queue.

**Prerequisites and attack path:** Employee A prepares offline clocking. Employee B later uses the same browser profile on the same application origin, or the user switches companies on the platform hostname. B's page reuses A's stored device. Offline submissions are authenticated by that saved device rather than the currently displayed account. A shift-ID mismatch can cause rejection, but a permissible shiftless in/out submission can still be attributed to A.

**Attacker impact:** Exposure of A's queued time/location data to another browser user who can access the same-origin storage, and possible punches under A's identity. Encryption does not isolate accounts when the browser retains both the key and the credentials together. This does not assume an attacker can extract a non-exportable key: same-origin code can use it to decrypt.

**Recommendation:** Partition device state and queues by company/user and confirm ownership against the authenticated page before reuse or synchronization. Clear active credentials on logout. Preserve pending evidence in an account-bound queue with an explicit recovery policy, rather than silently flushing another account's queue or deleting unsent work.

### F08 — My documents leaks other companies' document metadata

**Evidence:** `my_documents()` at `core/views.py:3843–3854` uses the global `PersonDocument.objects.filter(...)` without `organization=request.organization`. Its OR condition includes `_workforce_documents()`, which has no company restriction (`core/views.py:725–732`). `_record_visibility()` also imposes no tenant condition; workforce records without a person are readable by any member. `templates/core/my_documents.html:1` renders record-type names, original filenames, a hash prefix, expiry dates, and links containing document UUIDs.

**Prerequisites and attack path:** Any active member opens `/my-documents/`. Clean, active, current workforce records from other companies match the global query even if the member has no linked personnel record.

**Attacker impact:** Cross-company document metadata disclosure. Filenames and record-type labels can expose employee names or sensitive business subjects when those values are present. The ordinary download/preview/acknowledgment routes separately scope their lookup to the current company and reject the foreign records; full document-content access is not established by this finding.

**Recommendation:** Start with `request.organization.person_documents` or include the tenant condition outside the OR expression. Test two companies with distinct workforce records and assert that neither foreign metadata nor links appear for a linked or unlinked member.

### F09 — Access logs retain bearer credentials in URL paths

**Evidence:** `Caddyfile:39–42` and `73–76` enable access logs to stderr without URI redaction. `core/urls.py` embeds invitation tokens in `/invitations/<token>/` and messaging credentials in `/webhooks/<provider>/<token>/`. `core/views.py:499` uses the invitation token to authorize acceptance and `provider_callback()` at line 4640 resolves webhook tokens. The application and signing host both have access logs enabled.

**Prerequisites and attack path:** An attacker can read access logs collected from the proxy or its log destination, and finds a still-valid invitation visited before acceptance. For a new account, possession of that invitation permits setting its password and accepting its role. Invitations for an existing account additionally require login to that account. Accepted/expired invitations cannot be reused. Webhook impact depends on provider-signature requirements; token possession is not asserted to bypass an independently required signature.

**Attacker impact:** A lower-trust log reader can acquire an unused privileged invitation and gain access to employee records. Webhook bearer values are also disclosed to log readers. Whether logs are externally accessible or retained after token expiry was not assessed.

**Recommendation:** Redact credential-bearing paths before emission at every proxy/application collector, limit log access and retention, and avoid recording tokenized URLs in ancillary analytics. Rotate exposed callback tokens and revoke still-pending invitations when log exposure is suspected. Keep invitation expiry, single use, and provider-signature checks.

### F10 — Invitation-only signup setting does not close registration

**Follow-up review:** Identified while explicitly tracing uninvited account creation after the initial report. This was missed in the initial review.

**Evidence:** `config/settings.py:224` sets `ACCOUNT_SIGNUP_ENABLED = False`, but there is no configured `ACCOUNT_ADAPTER` implementing an invitation gate. `config/urls.py` includes the allauth URLs, including `/accounts/signup/`. The locally installed allauth account adapter at `allauth/account/adapter.py:304` returns `True` from `is_open_for_signup()`, and `allauth/account/mixins.py:146` uses that adapter method to decide whether registration is open. The installed account settings source contains no `SIGNUP_ENABLED` setting consumed for this decision. Its default social adapter delegates the same decision to the account adapter.

**Prerequisites and attack path:** An unauthenticated visitor reaches the application on an allowed host and submits the ordinary account signup form without an invitation. The purported signup-disable setting does not close that route. Configured social-provider registration may provide another creation route under the same policy; provider operation was not tested.

**Attacker impact:** Creation of an authenticated application account outside the invitation workflow, username reservation, and access to login-only resources such as the dependency/version information in `about()`. Registration alone does **not** create a company membership, staff flag, or employee-data access: `membership_required()` refuses an account without membership. Account precreation does, however, enable the conditional bootstrap escalation in F11.

**Recommendation:** Implement a supported account-adapter signup policy that denies public registration while retaining the explicit invitation acceptance path. Apply the equivalent social-account policy. Verify direct local/social registration cannot create an account without the intended authorization; a hidden signup link is insufficient.

### F11 — Bootstrap grants Owner to a preexisting, unverified account identity

**Follow-up review:** Identified together with F10; this is a conditional deployment/provisioning attack, not an unconditional remote Owner grant.

**Evidence:** `core/management/commands/bootstrap_admin.py:15–16` finds or creates the bootstrap user by `username=email`. It sets the operator-supplied password only when the user is newly created. At lines 22–24 it verifies the existing user's email and grants that user an active Owner membership without requiring their stored email to match `BOOTSTRAP_EMAIL`, verifying provenance, or refusing a preexisting nonprivileged user. `entrypoint.sh:13–14` can run this command when `RUN_BOOTSTRAP=1`; it can also be invoked manually.

**Prerequisites and attack path:** Before bootstrap runs for the relevant company, an attacker creates an ordinary account whose username equals the anticipated bootstrap email address. Its email may be the attacker's own mailbox. If the operator subsequently runs bootstrap for that username, the command retains the attacker's password and account identity and grants it Owner. F10 provides a public precreation route. The attack requires timing and knowledge/guessing of the bootstrap identifier; normal deployments that finish bootstrap before public exposure and never rerun it against an attacker-controlled account are not shown vulnerable to this sequence.

**Attacker impact:** Company Owner access, including employee records and the ability to manage privileged company access. The command may also mark the attacker's email verified. Existing MFA policy still applies; this finding does not independently bypass a previously established second factor. On a newly created company the model's required-MFA role list defaults to empty.

**Recommendation:** Treat bootstrap as security-sensitive provisioning. Fail closed if the identifier belongs to an existing account unless an explicit, authenticated operator recovery procedure establishes that account's provenance and authority. Never silently elevate a public account by username matching. Check identity consistency and keep bootstrap disabled after initial provisioning. Test a preexisting nonprivileged account with the target username and a different email; bootstrap must refuse to grant or verify it.

## Existing protections and boundaries

- Most reviewed business routes start from a company-related queryset, then apply role and object checks. The cross-company document-content routes reviewed remain scoped.
- SSNs and driver's-license details use a separate encrypted payload with person/company binding, dedicated roles, audited reveal, sensitive-variable handling, and a noncacheable response (`core/personnel_private.py`, `core/personnel_private_views.py`). These controls do not protect the other plaintext profile fields in F05.
- File ingestion has type/size validation, scanning, generated storage names, and image metadata removal. Downloads use attachment/no-sniff/no-store controls; previews limit verified content types.
- CSRF protection, verified-host middleware, production HTTPS/cookie settings, and admin source restrictions materially reduce attack exposure. They do not replace the missing authorization checks above.
- Tests cover substantial tenant/role behavior, but the last-scope test currently codifies F03, and MFA enrollment tests do not establish that admin login challenges an enrolled account.

No conclusion about a running system, object-storage ACLs, encryption of disks/backups, current dependency CVEs, actual log readership, or compromise history is supported by this review. Source-level storage settings are not evidence that a deployed bucket is private. The existing `SECURITY.md` contains historical control claims; those should be reconciled with these findings after remediation.

## Recommended order and verification

1. Close F01, F02, F10, and F11: require an MFA challenge for admin sessions, centralize privileged role grants, close uninvited signup, and reject bootstrap account preclaiming. Verify an enrolled staff user's password-only login cannot open admin, an Administrator cannot issue or accept a newly minted Owner grant, and an uninvited/preexisting account cannot obtain provisioning authority.
2. Close F03, F04, and F08: make scope fail closed and unify document authorization. Verify last-grant removal reduces access, a known out-of-scope photo UUID is refused, and foreign workforce metadata is absent.
3. Close F06 and F07: enforce revocation and partition offline identities. Verify disabled users/members cannot sync and browser account/company changes cannot reuse another identity.
4. Approve and enforce a narrower field policy for F05; redact bearer paths for F09. Verify denied fields never appear in forms/responses/exports and synthetic token values do not appear in captured logs.

These were the recommended follow-up checks at the time of the original code-only audit.

## Remediation status — October 9, 2026

The repository changes below address only the six findings explicitly approved for
remediation. Status reflects code review and the targeted tests listed here; it is not a
claim about any deployed environment or historical exposure.

| Finding | Status | Implementation and targeted verification |
| --- | --- | --- |
| F01 | Remediated | An enrolled staff session entering Django admin without a latest allauth MFA authentication record is routed through allauth's MFA reauthentication flow. Regression tests cover password-only admin authentication and a valid TOTP challenge. Existing `ADMIN_ALLOWED_IPS` behavior is retained. |
| F02 | Remediated | Only an Owner can issue an Owner invitation; acceptance invalidates Owner/Administrator invitations whose issuer no longer has the required active role. Tests cover forged Administrator issuance, valid Owner issuance, and a demoted Owner's stale invitation. |
| F08 | Remediated | `my_documents` constrains its complete person/workforce query to the active organization. Tests check filenames, metadata, and links for linked and unlinked users. |
| F09 | Remediated | Caddy redacts invitation/webhook token path segments in access logs on both site blocks and in default proxy-error logs. Django masks request messages, formatted exception tracebacks, and completed administrator error emails; Gunicorn omits request URIs. Synthetic-token tests cover Django emission and emails. Actual requests to an isolated Caddy container verified that both access and upstream-failure logs omit the synthetic bearer values. |
| F10 | Remediated | The installed allauth account and social adapters deny uninvited account creation while the invitation acceptance flow and existing linked social-account login continue to work. |
| F11 | Remediated | Bootstrap refuses unproven existing identities and does not reset passwords. Tests cover mismatched and unproven preclaims, initial provisioning, and a provenance-verified rerun. No real/local business database was used for provisioning validation. |

Targeted tests passed:

- `python manage.py test core.tests.AdminMfaChallengeTest`
- `python manage.py test core.tests.FirstVerticalSliceTest core.tests.MyDocumentsTenantIsolationTest`
- `python manage.py test core.test_logging.CredentialUrlLogRedactionTest core.test_security_fixes core.tests.ProvisionedAccountMfaTest`
- `python manage.py test core.test_logging.ProductionLoggingTest core.test_logging.CredentialUrlLogRedactionTest core.tests.FirstVerticalSliceTest`

`docker compose config -q` and `caddy validate` in the configured Caddy container passed.
The final combined targeted suite passed **26 tests** on both the host virtualenv and
the Linux container runtime. Linux `gunicorn --check-config` also passed. Runtime
log checks exposed and closed a separate Caddy proxy-error logging path that was
not protected by site access-log filters alone.
The full project suite was run: **1,074 tests, with 4 failures and 1 error**. A focused
rerun reproduced the same five unrelated failures:

- `core.test_form_ui.WorkflowFormUITest.test_native_dates_and_times_use_valid_html_initial_values_in_all_forms` — error: `PersonnelAssignmentsForm` requires an `organization` argument.
- `core.test_email_wording.EmailWordingTest.test_email_override_is_personalized_and_does_not_change_sms_or_in_app` — SMS body mismatch.
- `core.test_time_workflow.TimeWorkflowTest.test_open_punch_blocker_links_to_all_evidence_not_empty_pending_queue` — expected payroll link differs from timesheets link.
- `core.tests.NotificationEventFamiliesTest.test_locking_and_reopening_payroll_reach_the_roles_that_stood_behind_the_number` — expected notification absent.
- `core.tests.OpenPostClaimTest.test_an_unfilled_post_can_be_published_and_reaches_only_the_officers_who_qualify` — redirect includes `?week=1`.

These failures are outside the six approved findings; their source/tests were not changed
by this remediation and they were not repaired within its scope. The project system check
reported no issues.

F03, F04, F05, F06, and F07 were not approved for this change and were not modified or
validated as remediated. Their findings and recommendations above remain in force.
