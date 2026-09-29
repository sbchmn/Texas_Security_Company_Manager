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
[feature status](docs/feature-status.md), and the baseline engineering security review is
recorded in [SECURITY.md](SECURITY.md).

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
python manage.py migrate
BOOTSTRAP_EMAIL=owner@example.com \
BOOTSTRAP_PASSWORD='replace-this' \
python manage.py bootstrap_admin
DEBUG=true python manage.py runserver
```

Open <http://localhost:8000> and sign in with the bootstrap credentials. Without MySQL
environment variables, local development uses SQLite. This convenience fallback is not
the production database configuration.

### Docker Compose

```bash
cp .env.example .env
# Edit every secret/password in .env before starting.
docker compose -f compose.yaml up --build -d
docker compose -f compose.yaml exec web python manage.py bootstrap_admin
docker compose -f compose.yaml exec web python manage.py test
```

Use `-f compose.yaml` explicitly if the checkout also contains a legacy
`docker-compose.yml`; Docker Compose otherwise warns and selects one file by precedence.
Remove or archive the legacy file once any local-only settings have been reconciled.

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
and an allowed hostname.
