"""Run selected test labels against a real MySQL/Redis on high ports.

Harness notes that each caused a false alarm before (see project memory):
`IS_TEST` is computed from `sys.argv` at settings-import time, the developer `.env` must not be
loaded, and the `tscm` user cannot create `test_tscm` — so root, and argv before setup.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
os.environ["DOTENV_PATH"] = "N:/does-not-exist.env"
os.environ["TEST_LIVE_SERVICES"] = "true"
# The host shell already carries the compose stack's CLAMAV_HOST/MALWARE_SCAN_MODE/MYSQL_*/REDIS_URL,
# so every one of them is overwritten rather than left to a settings default. CI's mysql leg has no
# ClamAV either, and `IS_TEST` makes the mode default to basic there — the socket path is not part of
# this verification.
os.environ["MALWARE_SCAN_MODE"] = "basic"
os.environ["CLAMAV_HOST"] = ""
os.environ["MYSQL_HOST"] = "127.0.0.1"
os.environ["MYSQL_PORT"] = "13306"
os.environ["MYSQL_DATABASE"] = "tscm"
os.environ["MYSQL_USER"] = "root"
os.environ["MYSQL_PASSWORD"] = "guard-root-pw"
os.environ["REDIS_URL"] = "redis://127.0.0.1:16379/0"

labels = sys.argv[1:] or ["core.tests.DispositionTenantGuardTest"]
sys.argv = [sys.argv[0], "test", *labels, "--noinput", "-v", "2"]

import django  # noqa: E402
from django.core.management import execute_from_command_line  # noqa: E402

django.setup()
from django.conf import settings  # noqa: E402

print(f"vendor={settings.DATABASES['default']['ENGINE']} IS_TEST={settings.IS_TEST}")
execute_from_command_line(sys.argv)
