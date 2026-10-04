"""Probe the migration 0053 guards on a real MySQL 8.4, with the controls that make them mean anything.

Two nullable foreign keys, both deliberately nullable:

  * ``core_messageconsent.person_id`` — the ledger has to outlive the account it was about, because the
    complaint arrives after the guard has left. A guard written without ``IS NOT NULL`` would refuse
    every such row (and every row written by a person-bound import before the link exists), which is the
    0045 defect and the 0051 control in the same shape again.
  * ``core_deliveryevent.notification_id`` — a callback that cannot be tied to one of our notices is
    still a callback worth retaining.

The refusals are only interesting if the matching statement actually names a row, so every check prints
the rowcount it got; an "expected refusal, rowcount=0" means the WHERE matched nothing and the trigger
never ran, which the last probe mistook for a missing guard.
"""
import json
import os
import sys
import uuid

sys.path.insert(0, os.getcwd())
os.environ.setdefault("DOTENV_PATH", os.path.join(os.environ.get("TEMP", "/tmp"), "tscm-no-such-dotenv"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

sys.argv = ["probe", "test"]
django.setup()

from datetime import timedelta  # noqa: E402
from django.utils import timezone  # noqa: E402
from django.db import connection  # noqa: E402
from core.models import (AuditEvent, ClockKiosk, Client, DeliveryEvent, MessageConsent, Notification,  # noqa
                         Organization, Person, Site)

results = []


def key(value):
    if hasattr(value, "pk"):
        value = value.pk
    return value.hex if isinstance(value, uuid.UUID) else str(value).replace("-", "")


def now_sql(value=None):
    value = value or timezone.now()
    return value.astimezone(timezone.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def probe(statement, params, expect_error=None, label=""):
    params = [key(p) if (hasattr(p, "pk") or isinstance(p, uuid.UUID)) else p for p in params]
    with connection.cursor() as cursor:
        try:
            cursor.execute(statement, params)
        except Exception as exc:
            message = str(exc)
            passed = bool(expect_error) and expect_error in message
            results.append(passed)
            print(f"{'ok  ' if passed else 'FAIL'} {label}: "
                  f"{'refused as expected' if passed else ('wrong error' if expect_error else 'unexpected')}: {message[:110]}")
            return
    if expect_error:
        results.append(False)
        print(f"FAIL {label}: expected refusal but got rowcount={cursor.rowcount} — a statement that"
              " matches no row never reaches the trigger")
        return
    results.append(True)
    print(f"ok   {label}: rowcount={cursor.rowcount}")


print("vendor:", connection.vendor)
suffix = uuid.uuid4().hex[:8]
home = Organization.objects.create(legal_name="Home Msg", display_name="Home", slug=f"home-msg-{suffix}")
elsewhere = Organization.objects.create(legal_name="Else Msg", display_name="Else", slug=f"else-msg-{suffix}")
client = Client.objects.create(organization=home, name="Hillcrest")
site = Site.objects.create(organization=home, client=client, name="North Gate", address="1 Gate")
ana = Person.objects.create(organization=home, first_name="Ana", last_name="Del", mobile_phone="+12145550142")
bob = Person.objects.create(organization=elsewhere, first_name="Bob", last_name="Oth", mobile_phone="+12145550177")
notice = Notification.objects.create(organization=home, channel="sms", event_type="punch.exception",
                                     subject="x", body="y", destination="+12145550142")
foreign_notice = Notification.objects.create(organization=elsewhere, channel="sms", event_type="punch.exception",
                                             subject="x", body="y", destination="+12145550177")

CONSENT_INSERT = ("INSERT INTO core_messageconsent (id, organization_id, person_id, channel, destination,"
    " state, source, wording, evidence, created_at, decided_at) VALUES (%s,%s,%s,'sms',%s,'granted',"
    "'profile','wording','{}',%s,%s)")
CONSENT_UPDATE = "UPDATE core_messageconsent SET person_id=%s WHERE id=%s"
EVENT_INSERT = ("INSERT INTO core_deliveryevent (id, organization_id, provider, channel, destination, kind,"
    " notification_id, message_reference, detail, occurred_at, applied, verified, raw, created_at)"
    " VALUES (%s,%s,'twilio','sms',%s,'delivered',%s,'SM1','ok',%s,0,0,'{}',%s)")
EVENT_UPDATE = "UPDATE core_deliveryevent SET notification_id=%s WHERE id=%s"
stamp = now_sql()

print("\n== core_messageconsent guards (0053) ==")
own = uuid.uuid4()
probe(CONSENT_INSERT, [own, home, ana, ana.mobile_phone, stamp, stamp], label="consent about its own officer")
orphan = uuid.uuid4()
probe(CONSENT_INSERT, [orphan, home, None, "+12145550199", stamp, stamp],
      label="CONTROL consent with no person (the ledger outlives the account)")
probe(CONSENT_INSERT, [uuid.uuid4(), home, bob, bob.mobile_phone, stamp, stamp],
      expect_error="cross-tenant message consent reference", label="consent about another company's officer refused")
probe(CONSENT_UPDATE, [bob.pk, own], expect_error="cross-tenant message consent reference",
      label="re-pointing a consent at a foreign officer refused")
probe(CONSENT_UPDATE, [None, own], label="CONTROL clearing the person link allowed")
probe(CONSENT_UPDATE, [ana.pk, orphan], label="CONTROL adopting an orphan row to its own officer allowed")

print("\n== core_deliveryevent guards (0053) ==")
event = uuid.uuid4()
probe(EVENT_INSERT, [event, home, notice.destination, notice, stamp, stamp], label="callback for its own notice")
loose = uuid.uuid4()
probe(EVENT_INSERT, [loose, home, "+12145550199", None, stamp, stamp],
      label="CONTROL callback that matches no notice (retained anyway)")
probe(EVENT_INSERT, [uuid.uuid4(), home, foreign_notice.destination, foreign_notice, stamp, stamp],
      expect_error="cross-tenant delivery event reference", label="callback pinned to another company's notice refused")
probe(EVENT_UPDATE, [foreign_notice.pk, event], expect_error="cross-tenant delivery event reference",
      label="re-pinning a callback at a foreign notice refused")
probe(EVENT_UPDATE, [None, event], label="CONTROL releasing the link allowed")

print("\n== core_suppression: no child reference, so no guard by design ==")
probe("INSERT INTO core_suppression (id, organization_id, channel, destination, kind, reason, provider,"
      " source_reference, since) VALUES (%s,%s,'sms',%s,'unsubscribe','Asked not to','twilio','x',%s)",
      [uuid.uuid4(), home, ana.mobile_phone, stamp], label="suppression row for its own company")
probe("INSERT INTO core_suppression (id, organization_id, channel, destination, kind, reason, provider,"
      " source_reference, since) VALUES (%s,%s,'sms',%s,'manual','Blocked by an admin','','',%s)",
      [uuid.uuid4(), elsewhere, ana.mobile_phone, stamp],
      label="same number blocked by a DIFFERENT company (tenant scope is the organization column)")
from core.models import Suppression  # noqa: E402
rows = list(Suppression.objects.select_related("organization").values_list("organization__slug", "kind"))
print("   stored:", rows)
results.append(len({(org, kind) for org, kind in rows}) == 2 and len(rows) == 2)

print("\n== the guards that must still be there ==")
with connection.cursor() as cursor:
    cursor.execute("SELECT TRIGGER_NAME, EVENT_MANIPULATION FROM information_schema.TRIGGERS "
                   "WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE IN "
                   "('core_messageconsent','core_deliveryevent','core_auditevent','core_clockkiosk') "
                   "ORDER BY EVENT_OBJECT_TABLE, ACTION_ORDER")
    found = cursor.fetchall()
for table, events in (("core_messageconsent", {"INSERT", "UPDATE"}), ("core_deliveryevent", {"INSERT", "UPDATE"}),
                      ("core_clockkiosk", {"INSERT", "UPDATE"}), ("core_auditevent", {"UPDATE", "DELETE"})):
    got = {row[1] for row in found if row[0].startswith(table)}
    ok = events.issubset(got)
    results.append(ok)
    print(f"{'ok  ' if ok else 'FAIL'} {table}: {sorted(got)}")
with connection.cursor() as cursor:
    cursor.execute("SELECT COUNT(*) FROM core_auditevent")
    audit_rows = cursor.fetchone()[0]
probe("DELETE FROM core_auditevent WHERE organization_id=%s", [home],
      expect_error="audit events are immutable", label="0052's seal-aware audit guard still refuses an unnamed delete")
probe("UPDATE core_auditevent SET action=%s WHERE organization_id=%s AND 1=0", ["x", home],
      expect_error="audit events are immutable", label="0018's UPDATE guard intact")
print(f"   audit rows untouched: {audit_rows} (expected 0 for these orgs)")

print(f"\nchecks={len(results)} passed={sum(1 for item in results if item)}")
print("PROBE", "OK" if all(results) else "FAILED")
