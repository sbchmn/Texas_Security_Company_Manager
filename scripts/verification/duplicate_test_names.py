"""Report any test method name defined more than once inside one class of a test file.

`core/tests.py` is one very large file of back-to-back `TestCase` classes, and inserting a new class by
replacing the *tail* of the neighbouring class's methods has silently deleted real tests here before:
Python lets a later `def` shadow an earlier one in the same class, so nothing at edit time complains,
and the loss surfaces many minutes later as one ERROR in a full run. This is the check that catches it
at edit time.

    python scripts/verification/duplicate_test_names.py core/tests.py
"""
import ast
import collections
import io
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "core/tests.py"
tree = ast.parse(io.open(path, encoding="utf-8").read())
classes = 0
duplicates = []
for node in ast.walk(tree):
    if not isinstance(node, ast.ClassDef):
        continue
    classes += 1
    counts = collections.Counter(child.name for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)))
    duplicates.extend(f"{node.name}.{name} defined {count}x"
                      for name, count in counts.items() if count > 1)
print(f"file={path} classes={classes} duplicate_method_names={len(duplicates)}")
for line in duplicates:
    print("  DUPLICATE:", line)
sys.exit(1 if duplicates else 0)
