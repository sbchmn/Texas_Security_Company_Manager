"""Apply every migration outside a test database and report the trigger inventory.

CI's mysql leg runs `migrate` before `test`, and that ordering matters: the test runner builds a fresh
`test_tscm`, so a migration that only works inside a test DB — or one that fails on a server with
binary logging on — would still pass CI's test step unnoticed. This is the step that proves the guards
install on the database the product actually runs on, and prints the counts the docs quote.

    MYSQL_PASSWORD=... python scripts/verification/migrate_and_count_triggers.py

Prints: total triggers, the per-table guard inventory for the tables a new migration just touched, and
whether every tenant-linked table named in `GUARDED_TABLES` below has at least one guard. The list is
an inventory, not a claim of completeness: when a table gains a foreign key to a tenant-owned row, add
it here and add the guard in the same migration.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASSWORD = os.environ.get("MYSQL_PASSWORD")
if not PASSWORD:
    sys.exit("Set MYSQL_PASSWORD for the throwaway container; it is never read from argv.")

os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
os.environ["DOTENV_PATH"] = "N:/does-not-exist.env"
os.environ["MYSQL_HOST"] = os.environ.get("VERIFY_MYSQL_HOST", "127.0.0.1")
os.environ["MYSQL_PORT"] = os.environ.get("VERIFY_MYSQL_PORT", "13306")
os.environ["MYSQL_DATABASE"] = "tscm"
os.environ["MYSQL_USER"] = "root"
os.environ["MYSQL_PASSWORD"] = PASSWORD
os.environ["REDIS_URL"] = os.environ.get("VERIFY_REDIS_URL", "redis://127.0.0.1:16379/0")

# Tables whose rows reference something another tenant could own.
GUARDED_TABLES = ("core_persondocument", "core_punch", "core_dispositionrequest", "core_auditevent",
                  "core_notification", "core_shift", "core_credential", "core_payrollrun")

sys.argv = [sys.argv[0], "migrate", "--noinput"]
import django
from django.core.management import execute_from_command_line

django.setup()
execute_from_command_line(sys.argv)

from django.db import connection

with connection.cursor() as cursor:
    cursor.execute("SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema=DATABASE()")
    print("triggers_total =", cursor.fetchone()[0])
    placeholders = ",".join(["%s"] * len(GUARDED_TABLES))
    cursor.execute(f"SELECT EVENT_OBJECT_TABLE, COUNT(*) FROM information_schema.triggers "
                   f"WHERE trigger_schema=DATABASE() AND EVENT_OBJECT_TABLE IN ({placeholders}) "
                   f"GROUP BY EVENT_OBJECT_TABLE", list(GUARDED_TABLES))
    counts = dict(cursor.fetchall())
missing = [table for table in GUARDED_TABLES if not counts.get(table)]
for table in GUARDED_TABLES:
    print(f"  {table}: {counts.get(table, 0)} trigger(s)")
print("tables_without_any_guard =", missing or "none")
