"""Are 0018's / 0052's audit guards present in this database, and do they refuse real statements?

The messaging-guard probe reported no triggers on `core_auditevent` and two refusals that got
`rowcount=0` — which is the same probe flaw it warned about: the orgs it created had no audit rows at
all, so a `WHERE` matched nothing and a trigger had nothing to guard. This asks the question directly:
list every trigger in the schema, write one real audit event, then try to update and delete it.
"""
import os
import sys

sys.path.insert(0, os.getcwd())
os.environ.setdefault("DOTENV_PATH", os.path.join(os.environ.get("TEMP", "/tmp"), "tscm-no-such-dotenv"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

sys.argv = ["probe", "test"]
django.setup()

import uuid  # noqa: E402
from django.db import connection  # noqa: E402
from core.models import AuditEvent, Organization  # noqa: E402

org = Organization.objects.create(legal_name="Trigger Probe", display_name="Probe",
                                  slug=f"trig-{uuid.uuid4().hex[:8]}")
with connection.cursor() as cursor:
    cursor.execute("SELECT DATABASE()")
    print("database:", cursor.fetchone()[0])
    cursor.execute("SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, EVENT_MANIPULATION, ACTION_TIMING "
                   "FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() "
                   "AND EVENT_OBJECT_TABLE IN ('core_auditevent','core_messageconsent','core_deliveryevent') "
                   "ORDER BY EVENT_OBJECT_TABLE, TRIGGER_NAME")
    rows = cursor.fetchall()
print("triggers on the three tables:")
for row in rows:
    print("   ", row)
if not any(row[1] == "core_auditevent" for row in rows):
    print("!! no guard on core_auditevent in this database")
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE()")
        print("   total triggers in schema:", cursor.fetchone()[0])
        cursor.execute("SELECT VERSION()")
        print("   server:", cursor.fetchone()[0])
        cursor.execute("SELECT migration, applied FROM django_migrations WHERE app='core' "
                       "AND name IN ('0018_audit_database_guards','0052_audit_seal','0053_messaging_consent_lifecycle')")
        print("   marker migrations:", cursor.fetchall())

event = AuditEvent.objects.create(organization=org, actor=None, action="probe.written",
    target_type="organization", target_id=str(org.pk), metadata={"why": "to have a real row"})
print("audit rows for this org:", AuditEvent.objects.filter(organization=org).count())
for label, statement, params in (
        ("UPDATE", "UPDATE core_auditevent SET action=%s WHERE id=%s", ("tampered", event.pk.hex)),
        ("DELETE", "DELETE FROM core_auditevent WHERE id=%s", (event.pk.hex,))):
    with connection.cursor() as cursor:
        try:
            cursor.execute(statement, params)
            print(f"FAIL {label} on a real audit row was ALLOWED, rowcount={cursor.rowcount}")
        except Exception as exc:
            print(f"ok   {label} on a real audit row refused: {str(exc)[:90]}")
print("audit row still there:", AuditEvent.objects.filter(pk=event.pk).exists())
