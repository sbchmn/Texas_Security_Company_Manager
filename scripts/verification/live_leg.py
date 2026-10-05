"""Run selected test labels against a real MySQL 8.4 and Redis on high ports.

This is the local form of CI's `mysql` job, which is the only place the hand-written tenant-integrity
and audit-immutability triggers are ever executed: sqlite skips that SQL entirely, so a guard that was
never installed, or one that refuses the ordinary case, is invisible to the hermetic leg.

    docker run -d --rm -p 13306:3306 -e MYSQL_ROOT_PASSWORD=... -e MYSQL_DATABASE=tscm \
        mysql:8.4 --log-bin-trust-function-creators=1
    docker run -d --rm -p 16379:6379 redis:7.4-alpine
    MYSQL_PASSWORD=... python scripts/verification/live_leg.py core.tests.DispositionTenantGuardTest

Five harness rules, each learned from a failure that looked like a product bug:

* `--log-bin-trust-function-creators=1` is what lets the trigger migrations run with binary logging on
  (MySQL otherwise refuses `CREATE TRIGGER` for a non-SUPER user, error 1419).
* **Use root.** The `tscm` user cannot create `test_tscm`.
* **`DOTENV_PATH` must point at a file that does not exist**, and every service variable must be set
  *explicitly* here: the host shell often carries the compose stack's `CLAMAV_HOST` /
  `MALWARE_SCAN_MODE` / `MYSQL_*` / `REDIS_URL` in its own environment, and with
  `TEST_LIVE_SERVICES=true` those win over the settings defaults — which makes `malware_scan()` open a
  socket to a host that is not resolvable and every upload test die on `getaddrinfo failed`.
* **`--noinput`.** A `test_tscm` left by a killed run otherwise blocks the runner on the interactive
  "delete old database?" prompt and dies on `EOFError`.
* **`"test"` must be in `sys.argv` before `django.setup()`.** `config/settings.py` computes `IS_TEST`
  at import time; building argv only when calling the command yields `IS_TEST=False`, `PLATFORM_HOSTS`
  loses its test fallback, and `VerifiedHostMiddleware` answers 400 Unknown host to `testserver`.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASSWORD = os.environ.get("MYSQL_PASSWORD")
if not PASSWORD:
    sys.exit("Set MYSQL_PASSWORD to the throwaway container's root password. This script never takes "
             "it as an argument, so it cannot be copied into a commit by accident.")

os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
os.environ["DOTENV_PATH"] = "N:/does-not-exist.env"      # POSIX: use "/nonexistent.env"
os.environ["TEST_LIVE_SERVICES"] = "true"
os.environ["MALWARE_SCAN_MODE"] = "basic"                # CI's mysql leg has no ClamAV either
os.environ["CLAMAV_HOST"] = ""
os.environ["MYSQL_HOST"] = os.environ.get("VERIFY_MYSQL_HOST", "127.0.0.1")
os.environ["MYSQL_PORT"] = os.environ.get("VERIFY_MYSQL_PORT", "13306")
os.environ["MYSQL_DATABASE"] = "tscm"
os.environ["MYSQL_USER"] = "root"
os.environ["MYSQL_PASSWORD"] = PASSWORD
os.environ["REDIS_URL"] = os.environ.get("VERIFY_REDIS_URL", "redis://127.0.0.1:16379/0")

labels = sys.argv[1:] or ["core.tests.DispositionTenantGuardTest"]
sys.argv = [sys.argv[0], "test", *labels, "--noinput", "-v", "2"]

import django
from django.core.management import execute_from_command_line

django.setup()
from django.conf import settings

print(f"engine={settings.DATABASES['default']['ENGINE']} IS_TEST={settings.IS_TEST} "
      f"malware_mode={settings.MALWARE_SCAN_MODE}")
execute_from_command_line(sys.argv)
