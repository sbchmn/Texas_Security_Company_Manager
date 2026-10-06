# Texas Security Company Manager

This repository is in **active foundation development** for a Texas-only workforce and
regulatory-compliance platform for private-security companies.

The initial deployment will serve one company while preserving a deliberate path to a
multi-tenant SaaS product. The current decisions and constraints are recorded in the
[product brief](docs/product-brief.md), the accepted Round 2 answers are in the
[decision log](docs/discovery-decisions.md), researched defaults are in
[research recommendations](docs/research-recommendations.md), and the approved
[branding/custom-domain](docs/branding-and-custom-domains.md) and
[implementation-readiness](docs/implementation-readiness.md) documents define the next
step. The current implementation boundary is explicit in the
[feature status](docs/feature-status.md), the remaining development items are sectioned by
domain in [development roadmap](docs/development-roadmap.md), and the baseline engineering
security review is recorded in [SECURITY.md](SECURITY.md).

## Workflow navigation

The sidebar groups work into Today, People, Schedule, Time & Payroll, Compliance & Records,
and Reports, filtered by the current membership's permissions. Timeclock and My shifts are
pinned quick links. Add person lives in People, rather than competing with the main menu.
Settings shares the menu's single scroll region and opens automatically on configuration pages;
account controls remain outside that region.

Today separates team actions from personal work, so an owner, dispatcher, or supervisor linked
to a personnel record can still clock in and complete their own onboarding. Supervisors see
Time review without company payroll; dispatchers reach coverage, moves, time-off requests, and
sites from Schedule. Employees reach their checklist (including document signing), documents,
availability, and time-off requests from Today. These links do not grant additional permissions:
existing role and authority-scope checks still apply at each destination.

Schedule starts with a paginated action queue for requests to fill open posts, hand-offs,
trades, and time off across all dates in the manager's authority scope. Moves waiting for
employee consent are distinguished from requests awaiting manager review. Today summarizes
the same queue, with a direct link to review it. Decisions remain on their existing review
pages, where eligibility, consent, and coverage effects are checked.

People starts with outstanding onboarding tasks and unissued checklists across active and
onboarding employees, with office/employee, evidence, and overdue filters. Personnel-record
staff can open Signing operations to explicitly send ready requests, reconcile uncertain
creation, resolve failures, or view signed-and-filed documents. No queue auto-sends documents,
waives evidence, or treats an unverified signature as complete.

Today summarizes scoped work by responsible party, age, and next action while each workspace
retains its authoritative decisions. Schedule separates published open posts, drafts to prepare,
and at-risk assignments; move and time-off queue links select the particular request. Compliance
provides evidence-resolution links, Reports leads with operational exceptions, and retention
decisions precede collapsed archive/deletion history. Settings includes a permission-filtered
setup checklist: configuration state is not live integration health or legal certification.

My shifts puts response-required offers ahead of the roster and exposes assigned-post briefs,
including available post orders and authorized site/contact details. Employee consent and
manager approval remain separate steps. Notifications provide record-specific next actions
only when the recipient can access the destination; marking a notice read does not resolve
the underlying task.

Time review starts with pending punches and correction requests, counted consistently on Today
and Time & Payroll. Each queue has independent pagination and explicit reviewed-history filters.
Payroll links select an exact company-bound run; its snapshot, blockers, review links, decisions,
and exports retain that period. Approval still checks current pending time evidence and snapshot
exceptions. An oldest-first, paginated draft-period queue keeps unfinished runs discoverable
beyond the recent-run preview. Partial lock controls are secondary to the selected-period workflow; open slices
continue to prevent export.

Workspace summary cards share equal heights across desktop rows and a bottom-aligned primary
action. Secondary links appear above that action in a dedicated footer; inline preview
actions remain beside their records. On narrow screens cards stack with content-driven
heights rather than leaving large blank spaces.

Forms share labeled controls, grouped workflow sections, help text, and linked validation
summaries that preserve entered values. Personnel choices show names and employee IDs;
tour choices show the site, local times, and assigned officer. Site pickers group options
under client headings while displaying only the site name as the selected value. Pickers
retain organization and authority-scope filtering.

## Deployment requirement

The application must support both a production Docker/Compose deployment and
DigitalOcean App Platform. Both modes should use the same application image and
configuration contract; worker and supporting services should only be added when a
measured workload requires them.

## Compliance note

The application can administer a compliance program but must not claim to guarantee
legal compliance. Regulatory controls must be based on current primary sources,
versioned, approved by the product owner, and reviewed by a qualified Texas legal or
Private Security Program professional before release.

## Running the first application slice

The repository now contains a Django-based responsive PWA foundation with organization-
scoped roles, branches, a personnel directory, brand settings, append-only audit events,
MySQL deployment configuration, and a public health check.

### Local development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# .env.example points at the compose MySQL host. Clear it for a SQLite-only workspace.
export MYSQL_DATABASE=
python manage.py migrate
BOOTSTRAP_EMAIL=owner@example.com \
BOOTSTRAP_PASSWORD='replace-this' \
python manage.py bootstrap_admin
DEBUG=true python manage.py runserver
```

Open <http://localhost:8000> and sign in with the bootstrap credentials. Without MySQL
environment variables, local development uses SQLite. This convenience fallback is not
the production database configuration. `config/settings.py` reads `.env` from the repository
root unless `DOTENV_PATH` names another file.

### Docker Compose

```bash
cp .env.example .env
# Edit every secret/password in .env before starting. Keep DEBUG=false, put the served
# hostname in ALLOWED_HOSTS (it must match TSCM_SITE_ADDRESS and every verified custom
# domain), and list the source ranges that may reach /admin/ in ADMIN_ALLOWED_IPS —
# with DEBUG=false and no entries, the admin is closed to every address.
docker compose -f compose.yaml up --build -d
docker compose -f compose.yaml run --rm web python manage.py bootstrap_admin
docker compose -f compose.yaml exec web python manage.py test
```

Sign in with the bootstrap account. It is a Django staff user, so the first request
redirects to TOTP enrollment before any screen is usable.

`docker-compose.yml` is not part of this repository; `-f compose.yaml` is written out
anyway so the commands behave the same on a host that has other stacks checked out nearby.

Compose exposes the application only through Caddy, which terminates TLS on ports 80
and 443 (overridable with `TSCM_HTTP_PORT`/`TSCM_HTTPS_PORT` when the host ports are
taken) and forwards `X-Forwarded-Proto` to gunicorn; the web service publishes no port
of its own. Open <https://localhost> — or the host configured by `TSCM_SITE_ADDRESS`
and `TSCM_HTTPS_PORT` in `.env` — after startup. A public domain gets an automatic Let's Encrypt certificate;
a LAN host name is served with a certificate from Caddy's internal CA, whose root clients
must trust: `docker compose -f compose.yaml exec caddy cat /data/caddy/pki/authorities/local/root.crt`.
To force that internal CA on a publicly resolvable name, set `CADDY_TLS_MODE=tls internal`.

**A bare IP address cannot be the only site address.** TLS clients send SNI for host names
but not for IP literals (RFC 6066), so Caddy has no name to select a certificate for and
aborts the handshake — the browser reports it as an invalid response and `curl` on Windows as
`SEC_E_INTERNAL_ERROR`. Give the site a name, or list several comma-separated addresses
(`TSCM_SITE_ADDRESS=10.12.1.40, localhost`) and add the same names to `ALLOWED_HOSTS`.

**`.env` is not authoritative over your shell.** Any variable that is also exported in your
shell or as a Windows system/user environment variable wins — in compose's `${VAR}`
interpolation, and in Django, because `load_dotenv` never overwrites a value that is already
in the environment. When an edit to `.env` appears to do nothing, compare the two:

```bash
docker compose -f compose.yaml config | findstr TSCM_   # what compose will use
set | findstr TSCM_                                     # what is exported
```
The web container sets `TRUST_PROXY_SSL_HEADER=true` because Caddy overwrites
client-supplied `X-Forwarded-Proto` values; any deployment topology where gunicorn is
reachable without such a proxy must leave that variable unset, in which case Django
ignores the header instead of trusting spoofable values. For the same reason,
`TRUSTED_PROXIES` (private docker ranges by default in this file) names the peers allowed
to supply `X-Forwarded-For`; without it the login throttle would treat every visitor as
the same client, because `REMOTE_ADDR` is the Caddy container.

Migrations are applied once, by the short-lived `migrate` service, before `web` and
`worker` start — so scaling either replica never races the schema. To migrate by hand:
`docker compose -f compose.yaml run --rm migrate`.

**Document signing is opt-in and separate from the application.** `docker compose -f compose.yaml
--profile signing up -d` starts DocuSeal (community edition) plus its own queue backend; the default
topology is unchanged without it, and it publishes no port — Caddy serves it on `TSCM_SIGN_ADDRESS`, a
second hostname on the same 443 listener, because DocuSeal has no subpath/relative-URL-root mode and a
proxy-stripped `/sign` prefix makes it emit asset and download URLs without that prefix. It gets its
own **database and user inside the existing `db` container** rather than a second database engine:
`docker/mysql-init-signing.sh` creates them while the data directory is still empty, so an installation
that enables signing later must apply the same statements once by hand
(`docker compose -f compose.yaml exec db mysql -uroot -p`). Forgetting that looks like a TLS fault, not
a missing schema — DocuSeal crash-loops on `trilogy_auth_recv: TRILOGY_PROTOCOL_VIOLATION`. The values
it reads are in `.env.example`; anything it must be allowed to leave *unset* (its `SECRET_KEY_BASE`, an
SMTP relay, `HOST`) belongs in a `docuseal.env` file next to `compose.yaml`, which the service ignores
when absent — a blank environment entry is a value to a Rails app, not an absence, and the first boot of
this service failed on exactly that. **No signing feature is wired into the application yet**: no API
token, no submission call, no webhook ingest, no packet model. See
[development-roadmap.md](docs/development-roadmap.md) §13.

Compose enables ClamAV scanning for the web service and relies on the official image's
built-in `clamdcheck.sh` health check. Initial signature loading can take several minutes;
inspect it with `docker compose -f compose.yaml logs -f clamav` if `web` remains pending.
Do not replace that health check with `clamdscan --ping 1`: the separated optional
argument may be interpreted as a scan target instead of a ping-attempt count.

The bundled MySQL service enables `log_bin_trust_function_creators` because application
migrations create deterministic tenant-integrity and append-only audit triggers. Without
that server option, MySQL with binary logging rejects the migration user with error 1419.
This setting applies only to the bundled database; managed MySQL deployments must allow
the migration principal to create triggers or run migrations with a separate privileged
schema account.

The production image is shared by Compose and DigitalOcean App Platform. The example App
Platform specification is located at `.do/app.yaml`; production deployments must supply
secrets, configure a production MySQL database, persistent/object media storage, backups,
and an allowed hostname. That file documents two platform-specific traps: the container
filesystem is ephemeral, so media must live in object storage, and there is no ClamAV sidecar
on App Platform, so uploads run in `basic` mode unless you point `CLAMAV_HOST` at a scanner
you run yourself.

### Health and operations

```bash
docker compose -f compose.yaml ps
curl -ksS https://localhost/readyz -o /dev/null -w '%{http_code}\n'   # 200 ready, 503 degraded
docker compose -f compose.yaml exec web python manage.py check --deploy --fail-level WARNING
docker compose -f compose.yaml logs --since 24h worker
```

`/healthz` answers without touching the database (container liveness); `/readyz` proves the
database and cache are reachable and is what the orchestrator should gate traffic on. Set
`DJANGO_CHECK_DEPLOY=1` to make a container refuse to start until the deployment check
passes. Every service has a restart policy and bounded `json-file` logs, so a host reboot
brings the stack back without filling the disk.

### Two-person controls

Destructive and privacy-sensitive actions require a second owner or administrator:

- a document disposition is executed by someone other than its requester;
- an audit-export redaction is approved by someone other than its requester, and only an
  approved redaction changes an export.

The audit event itself is never modified or removed by either action. An organization with a
single owner cannot complete these steps, which is deliberate.

### Backups

```bash
docker compose -f compose.yaml --profile backups up -d
docker compose -f compose.yaml logs --since 24h db-backup binlog-backup
```

An hourly logical dump plus continuous binary-log shipping land in the `mysql_backups`
volume. [docs/backup-and-restore.md](docs/backup-and-restore.md) records the recovery point
this configuration actually reaches, the restore sequence, and the quarterly restore test that
`docs/discovery-decisions.md` requires before any recovery claim is made.

### Tests and CI

```bash
python manage.py test                              # hermetic: sqlite, in-memory cache, basic scanner
TEST_LIVE_SERVICES=true python manage.py test      # against the MySQL/Redis in your environment
```

A test run ignores the service choices in a deployment `.env` unless `TEST_LIVE_SERVICES=true`,
so `manage.py test` works on any machine. CI runs both legs; the MySQL leg is what proves the
tenant-integrity and audit-immutability triggers install and that the database — not only
Python — refuses audit mutation.
