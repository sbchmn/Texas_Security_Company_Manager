"""Import every management command module.

Django discovers commands by filename, so a command whose imports are broken sits silently in
`manage.py help` until the worker loop runs it at 03:00. This session opened on exactly that class of
defect (a service function referenced by an import that no one had defined), and the worker loop calls
seven of these — so the check is worth the thirty seconds.
"""
import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("DOTENV_PATH", os.path.join(os.environ.get("TEMP", "/tmp"), "tscm-no-such-dotenv"))
import django  # noqa: E402
django.setup()

root = Path("core/management/commands")
failures = []
for path in sorted(root.glob("*.py")):
    name = path.stem
    try:
        importlib.import_module(f"core.management.commands.{name}")
        print(f"ok   {name}")
    except Exception as exc:
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
print(f"\n{len(failures)} command module(s) failed to import")
sys.exit(1 if failures else 0)
