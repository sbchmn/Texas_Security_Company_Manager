"""MySQL probe for migration 0048's payroll-lock-segment guards.

The case this exists to check is the one migration 0045 got wrong: a company-wide segment has BOTH
branch_id and client_id NULL, and a `COUNT(*)` clause written without an `IS NOT NULL` guard refuses
exactly that row — the ordinary one. So the controls are not the interesting part of this file, they
are the part that makes the refusals mean anything.
"""
import os
import sys

sys.path.insert(0, os.getcwd())
os.environ.setdefault("DOTENV_PATH", os.path.join(os.environ.get("TEMP", "/tmp"), "tscm-no-such-dotenv"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402
django.setup()

import uuid  # noqa: E402
from datetime import timedelta  # noqa: E402
from django.db import connection  # noqa: E402
from django.utils import timezone  # noqa: E402
from core.models import Branch, Client, Organization, PayrollRun  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402


def hid(value):
    return value.hex if isinstance(value, uuid.UUID) else str(value).replace("-", "")


def probe(statement, params, expect_error=None, label=""):
    params = [hid(p) if isinstance(p, uuid.UUID) else p for p in params]
    with connection.cursor() as cursor:
        try:
            cursor.execute(statement, params)
            if expect_error:
                print(f"FAIL {label}: expected refusal, got rowcount={cursor.rowcount}")
                return False
            print(f"ok   {label}: rowcount={cursor.rowcount}")
            return True
        except Exception as exc:
            message = str(exc)
            if expect_error and expect_error in message:
                print(f"ok   {label}: refused — {message[:80]}")
                return True
            print(f"FAIL {label}: {'wrong error' if expect_error else 'unexpected error'}: {message[:140]}")
            return False


results = []
now = timezone.now().replace(microsecond=0)
run_tag = uuid.uuid4().hex[:8]
tenants = {}
for letter in ("A", "B"):
    org = Organization.objects.create(legal_name=f"Lock {letter} LLC", display_name=letter,
                                      slug=f"lock-{run_tag}-{letter.lower()}")
    branch = Branch.objects.create(organization=org, name=f"{letter} branch")
    client = Client.objects.create(organization=org, name=f"{letter} contract")
    payroll = PayrollRun.objects.create(organization=org, period_start=now - timedelta(days=3),
                                        period_end=now - timedelta(days=1), snapshot=[], exceptions=[],
                                        created_by=get_user_model().objects.create_user(
                                            username=f"lock-{run_tag}-{letter.lower()}", password="probe-only"))
    tenants[letter] = dict(org=org, branch=branch, client=client, run=payroll)
A, B = tenants["A"], tenants["B"]

INSERT = ("INSERT INTO core_payrolllocksegment (id, organization_id, run_id, branch_id, client_id, status,"
          " reason, decided_at) VALUES (%s, %s, %s, %s, %s, 'locked', 'probe', NOW(6))")

with connection.cursor() as cursor:
    cursor.execute("SELECT VERSION()")
    print(f"server={cursor.fetchone()[0]} vendor={connection.vendor}")

# THE control: a company-wide segment, both subject columns NULL. Unguarded COUNT() clauses die here.
first_id = uuid.uuid4()
results.append(probe(INSERT, [first_id, A["org"].pk, A["run"].pk, None, None],
                     label="control: company-wide segment (branch and client both NULL)"))
results.append(probe(INSERT, [uuid.uuid4(), A["org"].pk, A["run"].pk, A["branch"].pk, None],
                     label="control: own-tenant branch segment"))
results.append(probe(INSERT, [uuid.uuid4(), A["org"].pk, A["run"].pk, None, A["client"].pk],
                     label="control: own-tenant contract segment"))
results.append(probe(INSERT, [uuid.uuid4(), B["org"].pk, A["run"].pk, None, None],
                     expect_error="cross-tenant payroll lock segment reference",
                     label="segment on another tenant's payroll run"))
results.append(probe(INSERT, [uuid.uuid4(), A["org"].pk, A["run"].pk, B["branch"].pk, None],
                     expect_error="cross-tenant payroll lock segment reference",
                     label="segment naming another tenant's branch"))
results.append(probe(INSERT, [uuid.uuid4(), A["org"].pk, A["run"].pk, None, B["client"].pk],
                     expect_error="cross-tenant payroll lock segment reference",
                     label="segment naming another tenant's contract"))
# A second same-slice row is refused by the unique index, not the trigger — assert the index works and
# assert the guard is not blanket, by writing tenant B's own segment cleanly.
results.append(probe(INSERT, [uuid.uuid4(), B["org"].pk, B["run"].pk, B["branch"].pk, None],
                     label="control: tenant B's own segment (the guard is not blanket)"))
results.append(probe("UPDATE core_payrolllocksegment SET run_id = %s WHERE id = %s",
                     [B["run"].pk, first_id], expect_error="cross-tenant payroll lock segment reference",
                     label="UPDATE repointing a segment at another tenant's run"))

with connection.cursor() as cursor:
    cursor.execute("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
                   " AND EVENT_OBJECT_TABLE = 'core_payrolllocksegment'")
    print(f"guards_installed={cursor.fetchone()[0]} (expect 2)")
    cursor.execute("SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE()"
                   " AND TABLE_NAME = 'core_payrolllocksegment' AND INDEX_NAME = 'one_lock_segment_per_run_branch'")
    print(f"unique_index_columns={cursor.fetchone()[0]} (expect 2: the (run, branch) index spans both)")
    # What MySQL cannot enforce: a second company-wide segment. Both keys are NULL, and NULLs are
    # distinct inside a unique index — the reason set_payroll_lock_segment() dedups application-side.
    results.append(probe(INSERT, [uuid.uuid4(), A["org"].pk, A["run"].pk, None, None],
                         label="second company-wide segment (MySQL allows it; the service must not)"))

print(f"\n{sum(results)}/{len(results)} probes behaved as specified")
sys.exit(0 if all(results) else 1)
