"""What this slice only proves on MySQL, never on sqlite.

Run: .qwen/tmp/mysqlmig.cmd   (starts a throwaway 8.4, migrates it, then runs this)

Each check is a thing the hermetic leg cannot see:

1. `ChannelRule`'s unique constraint is unconditional *specifically* so MySQL creates it. The form
   refusing a duplicate is proved on sqlite; that the database underneath also refuses is a MySQL fact,
   and it is the reason the app-side check is a courtesy rather than the gate.
2. `Punch.risk_flags` is JSON on a table that carries a BEFORE INSERT tenant trigger: MySQL must accept
   the list, hand it back, and leave the trigger installed.
3. The 0053 cross-tenant consent guard has to still refuse after 0054 ran. A migration that shadowed or
   dropped a trigger would be invisible on every other leg of the suite.
4. `escalate_after_days` + `escalate_to` round-trip, since the reminder pass re-reads them per rung.
"""
import os
import sys
import uuid

import django

# The runner `cd`s to the project root, so the root is whatever the process started in — not the
# directory this file sits in. Deriving it from __file__ looks tidier and fails with
# "No module named 'config'" because the script lives two levels down in .qwen/tmp.
sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.db import connection, transaction
from django.db.utils import IntegrityError, OperationalError
from django.utils import timezone

from core.models import (ChannelRule, CredentialType, MessageConsent, Organization, Person,
                         Punch, TimePolicy, TimePolicyOverride)

ok, bad = [], []
tag = uuid.uuid4().hex[:8]


def check(label, condition, detail=""):
    (ok if condition else bad).append(f"{label}{(' — ' + detail) if detail else ''}")


org = Organization.objects.create(legal_name=f"Probe {tag}", display_name="Probe", slug=f"probe-{tag}")
other = Organization.objects.create(legal_name=f"Probe2 {tag}", display_name="Probe2", slug=f"probe2-{tag}")

# 1 — MySQL must refuse the duplicate the form also refuses.
ChannelRule.objects.create(organization=org, audience="subject", family="credential",
                           event_type="", channels=["sms"])
try:
    with transaction.atomic():
        ChannelRule.objects.create(organization=org, audience="subject", family="credential",
                                   event_type="", channels=["email"])
    check("MySQL enforces the channel-rule unique", False, "a second identical row was accepted")
except IntegrityError as exc:
    check("MySQL enforces the channel-rule unique", True, str(exc).splitlines()[0][:60])

# Narrowing the same audience to one event type is a different key, not a collision.
try:
    ChannelRule.objects.create(organization=org, audience="subject", family="credential",
                               event_type="credential.escalated", channels=["sms"])
    check("event_type is part of the key, so an exact rule can sit beside its family rule", True,
          f"{ChannelRule.objects.filter(organization=org).count()} rows")
except IntegrityError as exc:
    check("event_type is part of the key, so an exact rule can sit beside its family rule", False,
          str(exc)[:60])

# 2 — JSON verdict column on a trigger-guarded table.
person = Person.objects.create(organization=org, first_name="Ana", last_name="Del",
                               status=Person.Status.ACTIVE, mobile_phone="+12145550142")
punch = Punch.objects.create(organization=org, person=person, client_event_id=uuid.uuid4(),
                             kind=Punch.Kind.IN, occurred_at=timezone.now(), latitude=31.0,
                             longitude=-98.0, risk_flags=["implausible_accuracy", "impossible_travel"])
again = Punch.objects.get(pk=punch.pk)
check("Punch.risk_flags round-trips through MySQL",
      again.risk_flags == ["implausible_accuracy", "impossible_travel"],
      f"{again.risk_flags!r} · labels={len(again.risk_labels)}")

# 3 — the consent guard from 0053 still fires.
foreign_person = Person.objects.create(organization=other, first_name="Else", last_name="Where",
                                       status=Person.Status.ACTIVE)
try:
    with transaction.atomic():
        MessageConsent.objects.create(organization=org, person=foreign_person,
                                      channel=MessageConsent.Channel.SMS, destination="+12145559999",
                                      state=MessageConsent.State.GRANTED)
    check("cross-tenant consent guard still fires after 0054", False, "MySQL accepted a foreign person_id")
except OperationalError as exc:
    check("cross-tenant consent guard still fires after 0054", True, str(exc).splitlines()[0][:60])

# 4 — escalation columns.
requirement = CredentialType.objects.create(organization=org, name="PSD", code=f"psd-{tag}",
                                            warning_days=90, reminder_days_before=[90, 30],
                                            escalate_after_days=30, escalate_to=["payroll", "ghost"])
fresh = CredentialType.objects.get(pk=requirement.pk)
check("escalation columns round-trip and drop an unknown role",
      fresh.escalate_to == ["payroll", "ghost"] and fresh.escalation_roles == ["payroll"],
      f"stored={fresh.escalate_to!r} usable={fresh.escalation_roles!r}")
check("escalation reads as missed only at the rung and below",
      fresh.escalates_at(30) and fresh.escalates_at(12) and not fresh.escalates_at(45),
      f"missed rungs at 30 days: {fresh.missed_rungs(30)}")

TimePolicy.objects.create(organization=org, flag_spoof_risk=False)
check("baseline default holds through MySQL",
      TimePolicy.objects.get(organization=org).flag_spoof_risk is False)
override = TimePolicyOverride.objects.create(organization=org)
check("override inherits the spoof switch as NULL",
      TimePolicyOverride.objects.get(pk=override.pk).flag_spoof_risk is None)

with connection.cursor() as cursor:
    cursor.execute("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE()")
    triggers = cursor.fetchone()[0]
check("tenant and audit triggers all still installed", triggers >= 40, f"{triggers} triggers")

print(f"database vendor: {connection.vendor}")
print(f"PASSED {len(ok)}")
for line in ok:
    print("  ok   " + line)
print(f"FAILED {len(bad)}")
for line in bad:
    print("  FAIL " + line)
sys.exit(1 if bad else 0)
