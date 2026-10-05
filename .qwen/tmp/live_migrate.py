"""Apply every migration to a real MySQL 8.4 and report what the trigger inventory looks like after.

Run with `.qwen/tmp/live_leg.py`-style environment. This is the step CI does before the test leg:
migrations that install hand-written guards must apply on a server with binary logging enabled, not
only inside a test database.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
os.environ["DOTENV_PATH"] = "N:/does-not-exist.env"
os.environ["MYSQL_HOST"] = "127.0.0.1"
os.environ["MYSQL_PORT"] = "13306"
os.environ["MYSQL_DATABASE"] = "tscm"
os.environ["MYSQL_USER"] = "root"
os.environ["MYSQL_PASSWORD"] = "guard-root-pw"
os.environ["REDIS_URL"] = "redis://127.0.0.1:16379/0"

sys.argv = [sys.argv[0], "migrate", "--noinput"]

import django  # noqa: E402
from django.core.management import execute_from_command_line  # noqa: E402

django.setup()
execute_from_command_line(sys.argv)

from django.db import connection  # noqa: E402

with connection.cursor() as cursor:
    cursor.execute("SELECT COUNT(*) FROM information_schema.triggers "
                   "WHERE trigger_schema=DATABASE()")
    total = cursor.fetchone()[0]
    cursor.execute("SELECT EVENT_OBJECT_TABLE, GROUP_CONCAT(TRIGGER_NAME ORDER BY TRIGGER_NAME) "
                   "FROM information_schema.triggers WHERE trigger_schema=DATABASE() "
                   "AND EVENT_OBJECT_TABLE='core_dispositionrequest' GROUP BY EVENT_OBJECT_TABLE")
    rows = cursor.fetchall()
    cursor.execute("SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema=DATABASE() "
                   "AND EVENT_OBJECT_TABLE IN ('core_punch','core_persondocument')")
    punch_doc = cursor.fetchone()[0]
print(f"triggers_total={total} punch_and_document={punch_doc} disposition={rows}")
