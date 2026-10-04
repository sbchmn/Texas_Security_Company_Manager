"""Probe the two MySQL guards REC-4 and CLK-2 install, on a real 8.4 server.

The hermetic leg never runs this SQL, so this is the only evidence that the triggers do what the
migrations claim:

  * ``core_clockkiosk_tenant_insert/update`` (0051) — refuse a station planted at another company's
    post, and ALLOW the ordinary shapes. ``site_id`` is nullable, and that is the part with history:
    migration 0045 shipped the same clause without its ``IS NOT NULL`` guard and refused every insert,
    which no sqlite run could see. So the controls matter as much as the refusals here.
  * ``core_audit_no_delete`` (0052) — refuse every delete the session has not put under a named seal,
    and keep refusing rows that seal's period does not cover even when a seal *is* named. Otherwise
    "seal, then purge" degrades to "set one variable, delete anything", which is the hole the seal
    exists to close. The 0018 UPDATE guard must still hold on the same table.

Ids are passed as ``.hex`` because these columns are char(32); passing a model instance instead made
the WHERE clause match nothing and the trigger never fired, which the first run of this probe mistook
for a missing guard.
"""
import json
import os
import sys

sys.path.insert(0, os.getcwd())
os.environ.setdefault("DOTENV_PATH", os.path.join(os.environ.get("TEMP", "/tmp"), "tscm-no-such-dotenv"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

sys.argv = ["probe", "test"]  # settings.IS_TEST is computed from argv at import time
django.setup()

import uuid  # noqa: E402
from datetime import timedelta  # noqa: E402
from django.utils import timezone  # noqa: E402
from django.db import connection  # noqa: E402
from core.models import AuditEvent, ClockKiosk, Client, Organization, Site, audit_event_hash  # noqa: E402
from core.services import purge_sealed_audit, seal_audit_period, verify_audit_chain  # noqa: E402

results = []


def key(value):
    """A column value for raw SQL: model → its pk → hex, uuid → hex, anything else unchanged."""
    if hasattr(value, "pk"):
        value = value.pk
    return value.hex if isinstance(value, uuid.UUID) else str(value).replace("-", "")


def stamp(value):
    return value.astimezone(timezone.get_default_timezone()).strftime("%Y-%m-%d %H:%M:%S.%f")


def probe(statement, params=(), expect_error=None, label=""):
    params = [key(p) if (hasattr(p, "pk") or isinstance(p, uuid.UUID)) else p for p in params]
    with connection.cursor() as cursor:
        try:
            cursor.execute(statement, params)
        except Exception as exc:
            message = str(exc)
            passed = bool(expect_error) and expect_error in message
            results.append(passed)
            verdict = "refused as expected" if passed else ("wrong error" if expect_error else "unexpected")
            print(f"{'ok  ' if passed else 'FAIL'} {label}: {verdict}: {message[:110]}")
            return cursor
    if expect_error:
        results.append(False)
        print(f"FAIL {label}: expected refusal, got rowcount={cursor.rowcount} (a WHERE that matches"
              " nothing means the trigger never ran — check the params, not the guard)")
        return cursor
    results.append(True)
    print(f"ok   {label}: rowcount={cursor.rowcount}")
    return cursor


def plant(organization, action, when, metadata=None):
    """One audit event at a chosen instant, hashed the way the live writer hashes it."""
    pk = uuid.uuid4()
    head = AuditEvent.objects.filter(organization=organization).order_by("occurred_at", "id").last()
    previous = head.event_hash if head else ""
    digest = audit_event_hash(id=pk, organization=organization.pk, actor=None, action=action,
        target_type="probe", target_id=action, metadata=metadata or {}, previous_hash=previous)
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO core_auditevent (id, organization_id, actor_id, action, target_type,"
            " target_id, metadata, previous_hash, event_hash, occurred_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [pk.hex, key(organization), None, action, "probe", action, json.dumps(metadata or {}),
             previous, digest, stamp(when)])
    return AuditEvent.objects.get(pk=pk)


print("vendor:", connection.vendor)
suffix = uuid.uuid4().hex[:8]
home = Organization.objects.create(legal_name="Home Gate", display_name="Home", slug=f"home-{suffix}")
elsewhere = Organization.objects.create(legal_name="Else Gate", display_name="Else", slug=f"else-{suffix}")
home_client = Client.objects.create(organization=home, name="Hillcrest")
home_site = Site.objects.create(organization=home, client=home_client, name="North Gate", address="1 Gate")
else_client = Client.objects.create(organization=elsewhere, name="Other Care")
else_site = Site.objects.create(organization=elsewhere, client=else_client, name="Yard", address="9 Yard")

print("\n== clock kiosk guards (0051) ==")
now = timezone.now()
probe("INSERT INTO core_clockkiosk (id, name, active, created_at, last_seen_at, site_id, organization_id, created_by_id)"
      " VALUES (%s,%s,1,%s,NULL,%s,%s,NULL)", [uuid.uuid4(), "Guard shack 2", stamp(now), home_site, home],
      label="station at its own post")
probe("INSERT INTO core_clockkiosk (id, name, active, created_at, last_seen_at, site_id, organization_id, created_by_id)"
      " VALUES (%s,%s,1,%s,NULL,NULL,%s,NULL)", [uuid.uuid4(), "Mobile pad", stamp(now), home],
      label="CONTROL station with no post at all (the 0045 failure mode)")
probe("INSERT INTO core_clockkiosk (id, name, active, created_at, last_seen_at, site_id, organization_id, created_by_id)"
      " VALUES (%s,%s,1,%s,NULL,%s,%s,NULL)", [uuid.uuid4(), "Stolen tablet", stamp(now), else_site, home],
      expect_error="cross-tenant clock kiosk reference", label="station at another company's post refused")
live = ClockKiosk.objects.filter(organization=home, site__isnull=False).first()
probe("UPDATE core_clockkiosk SET site_id=%s WHERE id=%s", [else_site, live],
      expect_error="cross-tenant clock kiosk reference", label="moving a station to a foreign post refused")
probe("UPDATE core_clockkiosk SET name=%s WHERE id=%s", ["Renamed", live],
      label="CONTROL ordinary rename allowed")
print("   kiosks in:", ClockKiosk.objects.count(), "(expect 2)",
      "| no-post rows:", ClockKiosk.objects.filter(site__isnull=True).count(), "(expect 1)")

print("\n== audit delete guard (0052) ==")
months = [timezone.now() - timedelta(days=days) for days in (400, 399, 398)]
planted = [plant(home, f"probe.{index}", when) for index, (when) in enumerate(months)]
print("   chain before any sealing:", verify_audit_chain(home) or "no errors")
probe("UPDATE core_auditevent SET occurred_at=%s WHERE id=%s", [stamp(now), planted[0]],
      expect_error="audit events are immutable", label="UPDATE guard still holds after 0052")
probe("DELETE FROM core_auditevent WHERE organization_id=%s", [home],
      expect_error="audit events are immutable", label="bare DELETE refused with no session seal")
probe("DELETE FROM core_auditevent WHERE id=%s", [planted[0]],
      expect_error="audit events are immutable", label="single row refused with no session seal")

start, end = months[0] - timedelta(days=1), months[-1] + timedelta(days=1)
seal = seal_audit_period(home, start, end, actor=None)
print(f"   sealed {seal.event_count} event(s) head={seal.last_hash[:12]}… archive={seal.archive_bytes} bytes")
results.append(seal.event_count == len(months))

print("\n   -- a named seal does not unlock what its period does not cover --")
outside = AuditEvent.objects.create(organization=home, actor=None, action="probe.outside",
    target_type="probe", target_id="outside", metadata={})          # occurred_at = now, after `end`
foreign = AuditEvent.objects.create(organization=elsewhere, actor=None, action="probe.foreign",
    target_type="probe", target_id="foreign", metadata={})
with connection.cursor() as cursor:
    cursor.execute("SET @audit_purge_seal = %s", [seal.pk.hex])
probe("DELETE FROM core_auditevent WHERE id=%s", [outside],
      expect_error="audit events are immutable", label="row after the sealed period refused")
with connection.cursor() as cursor:
    cursor.execute("SET @audit_purge_seal = %s", [seal.pk.hex])
probe("DELETE FROM core_auditevent WHERE id=%s", [foreign],
      expect_error="audit events are immutable", label="another tenant's row refused under this seal")
with connection.cursor() as cursor:
    cursor.execute("SET @audit_purge_seal = %s", [uuid.uuid4().hex])
probe("DELETE FROM core_auditevent WHERE id=%s", [planted[0]],
      expect_error="audit events are immutable", label="a made-up seal id refuses")
with connection.cursor() as cursor:
    cursor.execute("SET @audit_purge_seal = %s", ["not-a-uuid"])
probe("DELETE FROM core_auditevent WHERE id=%s", [planted[0]],
      expect_error="audit events are immutable", label="a malformed session value refuses")

print("\n   -- the sanctioned trim, through the service --")
purge_sealed_audit(seal, actor=None)
seal.refresh_from_db()
live_home = AuditEvent.objects.filter(organization=home).order_by("occurred_at", "id")
# Three survivors, not one: the row created after the sealed period, plus the `audit.sealed` and
# `audit.purged` events the two acts of retention write themselves. Expecting one was this probe
# misreading its own bookkeeping, not the trim leaving rows behind.
print("   seal status:", seal.status, "| live rows for home:", live_home.count(),
      "| actions:", [row.action for row in live_home])
results.append(seal.status == "purged" and live_home.count() == 3)
errors = verify_audit_chain(home)
results.append(errors == [])
print(f"{'ok  ' if errors == [] else 'FAIL'} verify_audit_chain after the purge: {errors or 'no errors'}")
head = live_home.first()
results.append(head.previous_hash == seal.last_hash)
print(f"{'ok  ' if head.previous_hash == seal.last_hash else 'FAIL'} the surviving event continues the seal head")

print("\n   -- and a real event written after the purge joins the chain rather than restarting it --")
fresh = AuditEvent.objects.create(organization=home, actor=None, action="probe.after",
    target_type="probe", target_id="after", metadata={"n": 1})
errors = verify_audit_chain(home)
results.append(errors == [] and fresh.previous_hash != "")
print(f"{'ok  ' if errors == [] else 'FAIL'} chain still verifies with the new event: {errors or 'no errors'}"
      f"; it continues a live row rather than the seal ({fresh.previous_hash != ''})")

print("\n   -- seal continuity is refused across a gap, accepted when contiguous --")
# A fresh tenant, walked forward in time: `plant` can only append to the end of a chain, and inserting
# a row into the middle of one that already exists is itself a break — the verifier caught exactly that
# when this probe first tried it here, which is reassuring but not the check being asked for.
gap_org = Organization.objects.create(legal_name="Gap Co", display_name="Gap", slug=f"gap-{suffix}")
for index, days in enumerate((300, 260, 220, 180)):
    plant(gap_org, f"gap.{index}", timezone.now() - timedelta(days=days))
first_start = timezone.now() - timedelta(days=301)
first_end = timezone.now() - timedelta(days=261)
second_end = timezone.now() - timedelta(days=221)
seal_one = seal_audit_period(gap_org, first_start, first_end, actor=None)
try:
    seal_audit_period(gap_org, first_end + timedelta(days=40), second_end + timedelta(days=40), actor=None)
    results.append(False)
    print("FAIL a period that does not continue the last seal was accepted")
except Exception as exc:
    passed = "continue the period" in str(exc)
    results.append(passed)
    print(f"{'ok  ' if passed else 'FAIL'} gap refused: {str(exc)[:130]}")
seal_two = seal_audit_period(gap_org, first_end, second_end, actor=None)
results.append(seal_two.first_previous == seal_one.last_hash)
print(f"ok   the contiguous period seals; first_previous matches the previous head:"
      f" {seal_two.first_previous == seal_one.last_hash} ({seal_two.event_count} event(s))")
purge_sealed_audit(seal_one, actor=None)
purge_sealed_audit(seal_two, actor=None)
errors = verify_audit_chain(gap_org)
results.append(errors == [])
print(f"{'ok  ' if errors == [] else 'FAIL'} two chained seals, both trimmed, still verify:"
      f" {errors or 'no errors'} | live rows: {AuditEvent.objects.filter(organization=gap_org).count()}")

print(f"\nchecks={len(results)} passed={sum(1 for item in results if item)}")
print("PROBE", "OK" if all(results) else "FAILED")
