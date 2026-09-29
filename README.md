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
docker compose up --build -d
docker compose exec web python manage.py bootstrap_admin
docker compose exec web python manage.py test
```

The production image is shared by Compose and DigitalOcean App Platform. The example App
Platform specification is located at `.do/app.yaml`; production deployments must supply
secrets, configure a production MySQL database, persistent/object media storage, backups,
and an allowed hostname.
