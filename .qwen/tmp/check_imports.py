"""Static check: every name views.py imports from services/models must exist.

The interrupted PAY-2 slice left an ImportError in ``core.views``, and the traceback only names the
first missing symbol. This resolves the whole import list at once so the rest are found before the
test suite is paid for.
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGETS = {"services": ROOT / "core" / "services.py", "models": ROOT / "core" / "models.py"}


def exported(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def imported_from(view_path, module_leaf):
    tree = ast.parse(view_path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(module_leaf):
            out.extend(a.name for a in node.names)
    return out


views = ROOT / "core" / "views.py"
failures = 0
for leaf, path in TARGETS.items():
    have = exported(path)
    for name in imported_from(views, leaf):
        if name not in have:
            print(f"MISSING core.{leaf}.{name}")
            failures += 1
# tests.py imports too
tests = ROOT / "core" / "tests.py"
for leaf, path in TARGETS.items():
    have = exported(path)
    for name in imported_from(tests, leaf):
        if name not in have:
            print(f"MISSING (tests) core.{leaf}.{name}")
            failures += 1
print("ALL_IMPORTS_RESOLVE" if not failures else f"{failures} missing")
sys.exit(1 if failures else 0)
