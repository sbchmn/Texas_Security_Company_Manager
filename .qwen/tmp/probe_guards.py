"""MySQL probe for the 0043/0045 tenant guards: do they refuse, and does a matched row still pass?

Rows are built through the ORM so the probe cannot fail on a guessed column list, and the cross-tenant
statements go through a raw cursor because the guards live below the application. Two things make the
refusals mean something: a control insert with matched tenants must succeed (a trigger that rejects
everything looks identical to one that works), and every probe names a row that actually exists in the
other tenant, since a statement matching no rows refuses nothing.

UUID primary keys are CHAR(32) on MySQL, so every raw parameter goes through ``hid`` — passing the
dashed form raises 1406 "Data too long" and would look exactly like a guard firing.
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
from core.models import Client, Organization, PayCategory, Person, Shift, Site  # noqa: E402


def hid(value):
    """The CHAR(32) form of a uuid primary key."""
    return value.hex if isinstance(value, uuid.UUID) else str(value).replace("-", "")


def probe(statement, params, expect_error=None, label=""):
    params = [hid(p) if isinstance(p, uuid.UUID) else p for p in params]
    with connection.cursor() as cursor:
        try:
            cursor.execute(statement, params)
            if expect_error:
                print(f"FAIL {label}: expected refusal, got rowcount={cursor.rowcount}")
                return False
            if cursor.rowcount == 0 and "UPDATE" in statement:
                print(f"FAIL {label}: matched no rows, so this proved nothing")
                return False
            print(f"ok   {label}: rowcount={cursor.rowcount}")
            return True
        except Exception as exc:
            message = str(exc)
            if expect_error and expect_error in message:
                print(f"ok   {label}: refused — {message[:80]}")
                return True
            print(f"FAIL {label}: {'wrong error' if expect_error else 'unexpected error'}: {message[:150]}")
            return False


results = []
now = timezone.now().replace(microsecond=0)
tenants = {}
# A unique slug per run instead of a cleanup pass: Client.organization is PROTECT, so deleting the
# organizations from a previous run raises ProtectedError, and an ordered teardown would be probe
# machinery pretending to be product logic. Every count below is scoped to this run's two tenants.
run = uuid.uuid4().hex[:8]

for letter in ("A", "B"):
    org = Organization.objects.create(legal_name=f"Guard {letter} LLC", display_name=letter,
                                      slug=f"probe-{run}-{letter.lower()}")
    client = Client.objects.create(organization=org, name=f"Client {letter}")
    site = Site.objects.create(organization=org, client=client, name=f"Site {letter}", address=f"{letter} road")
    person = Person.objects.create(organization=org, first_name=f"P{letter}", last_name="Probe",
                                   status=Person.Status.ACTIVE)
    shift = Shift.objects.create(organization=org, site=site, officer=person, starts_at=now,
                                 ends_at=now + timedelta(hours=8), status=Shift.Status.PUBLISHED,
                                 post_name="Gate")
    category = PayCategory.objects.create(organization=org, kind="break", name=f"Break {letter}")
    tenants[letter] = dict(org=org, site=site, person=person, shift=shift, category=category)
    print(f"built tenant {letter}: org={hid(org.pk)} shift={hid(shift.pk)}")

A, B = tenants["A"], tenants["B"]
HOVER = ("INSERT INTO core_holdover (id, organization_id, shift_id, scheduled_ends_at, reason, note, created_at)"
         " VALUES (%s, %s, %s, NOW(6), 'relief_no_show', %s, NOW(6))")

with connection.cursor() as cursor:
    cursor.execute("SELECT VERSION()")
    print(f"server={cursor.fetchone()[0]} vendor={connection.vendor}")

# The control first: without it the refusals below prove nothing.
results.append(probe(HOVER, [uuid.uuid4(), A["org"].pk, A["shift"].pk, "control"],
                     label="control: hold-over on own tenant's post"))
results.append(probe(HOVER, [uuid.uuid4(), B["org"].pk, A["shift"].pk, "x"],
                     expect_error="cross-tenant hold-over reference",
                     label="hold-over filed against another tenant's post"))
results.append(probe("INSERT INTO core_holdover (id, organization_id, shift_id, relief_id, scheduled_ends_at,"
                     " reason, note, created_at) VALUES (%s, %s, %s, %s, NOW(6), 'relief_late', 'x', NOW(6))",
                     [uuid.uuid4(), A["org"].pk, A["shift"].pk, B["person"].pk],
                     expect_error="cross-tenant hold-over reference",
                     label="relief drawn from another tenant's roster"))
results.append(probe("UPDATE core_holdover SET relief_id = %s WHERE organization_id = %s",
                     [B["person"].pk, A["org"].pk],
                     expect_error="cross-tenant hold-over reference",
                     label="UPDATE repointing relief across tenants"))
results.append(probe("INSERT INTO core_shift (id, organization_id, site_id, officer_id, starts_at, ends_at,"
                     " status, post_name, post_orders, relief_for_id, created_at, updated_at)"
                     " VALUES (%s, %s, %s, %s, NOW(6), NOW(6) + INTERVAL 4 HOUR, 'published', 'Half', '',"
                     " %s, NOW(6), NOW(6))",
                     [uuid.uuid4(), B["org"].pk, B["site"].pk, B["person"].pk, A["shift"].pk],
                     expect_error="cross-tenant shift reference",
                     label="relief_for naming another tenant's tour (INSERT)"))
results.append(probe("UPDATE core_shift SET relief_for_id = %s WHERE id = %s",
                     [A["shift"].pk, B["shift"].pk],
                     expect_error="cross-tenant shift reference",
                     label="UPDATE repointing relief_for across tenants"))
# A pointer at a row that does not exist must not pass as a NULL. It is the TRIGGER that catches it
# first, not the foreign key — COUNT() is 0 for a missing parent, so the guard's own SQLSTATE 1644 is
# the answer. That is 0025's lesson working as intended, and the expected message here is the guard's.
results.append(probe("UPDATE core_shift SET relief_for_id = %s WHERE id = %s",
                     [uuid.uuid4(), A["shift"].pk],
                     expect_error="cross-tenant shift reference",
                     label="relief_for at a nonexistent row (the guard, not the FK)"))
# A plain post with no relief at all must still insert: this is the clause that broke on the first run.
results.append(probe("INSERT INTO core_shift (id, organization_id, site_id, officer_id, starts_at, ends_at,"
                     " status, post_name, post_orders, created_at, updated_at)"
                     " VALUES (%s, %s, %s, %s, NOW(6), NOW(6) + INTERVAL 4 HOUR, 'published', 'Ordinary', '',"
                     " NOW(6), NOW(6))",
                     [uuid.uuid4(), A["org"].pk, A["site"].pk, A["person"].pk],
                     label="control: ordinary post with relief_for left NULL"))
results.append(probe("UPDATE core_shift SET relief_for_id = %s WHERE id = %s",
                     [A["shift"].pk, A["shift"].pk],
                     label="control: same-tenant relief_for (trigger stays silent; the model owns self-relief)"))
# 0043's designation guard, re-probed because 0045 rewrote the shift chain around it. PayCategory's pk
# is an int, so this needs a real row from the other tenant rather than a made-up id — an invented id
# fails on the column type long before the guard is reached, which is not a proof of anything.
category_b = B["category"]
results.append(probe("INSERT INTO core_shifthourdesignation (id, organization_id, shift_id, category_id, hours,"
                     " reason, created_at) VALUES (%s, %s, %s, %s, 1.00, 'x', NOW(6))",
                     [uuid.uuid4(), A["org"].pk, A["shift"].pk, category_b.pk],
                     expect_error="cross-tenant hour designation reference",
                     label="designation pricing A's hours on B's multiplier"))
results.append(probe("INSERT INTO core_shifthourdesignation (id, organization_id, shift_id, category_id, hours,"
                     " reason, created_at) VALUES (%s, %s, %s, %s, 1.00, 'control', NOW(6))",
                     [uuid.uuid4(), A["org"].pk, A["shift"].pk, A["category"].pk],
                     label="control: designation on own tenant's post and category"))

with connection.cursor() as cursor:
    cursor.execute("SELECT EVENT_OBJECT_TABLE, COUNT(*) FROM information_schema.TRIGGERS"
                   " WHERE TRIGGER_SCHEMA = DATABASE() AND EVENT_OBJECT_TABLE IN"
                   " ('core_holdover','core_shift','core_shifthourdesignation','core_paycategory')"
                   " GROUP BY EVENT_OBJECT_TABLE")
    print(f"trigger_counts={dict(cursor.fetchall())}")
    cursor.execute("SELECT COUNT(*) FROM core_shift WHERE relief_for_id IS NOT NULL"
                   " AND organization_id IN (%s, %s)", [hid(A["org"].pk), hid(B["org"].pk)])
    print(f"relief_for_rows_this_run={cursor.fetchone()[0]} (expect 1: the same-tenant control)")
    cursor.execute("SELECT COUNT(*) FROM core_holdover WHERE organization_id IN (%s, %s)",
                   [hid(A["org"].pk), hid(B["org"].pk)])
    print(f"holdover_rows_this_run={cursor.fetchone()[0]} (expect 1: the control only)")
    cursor.execute("SELECT COUNT(*) FROM core_shift WHERE organization_id IN (%s, %s)",
                   [hid(A["org"].pk), hid(B["org"].pk)])
    print(f"shift_rows_this_run={cursor.fetchone()[0]} (expect 5: 2 built + ordinary-post control;"
          f" the cross-tenant relief_for INSERT must not have landed)")

print(f"\n{sum(results)}/{len(results)} probes behaved as specified")
sys.exit(0 if all(results) else 1)
