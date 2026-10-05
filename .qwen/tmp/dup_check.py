"""Report any test method name defined more than once in core/tests.py, per class.

Exists because this suite is one 10,000-line file of adjacent TestCase classes: a bad insertion
anchor silently replaces another class's method, and Python lets a later `def` shadow an earlier
one, so nothing at edit time says "you just deleted existing tests".
"""
import ast
import collections
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "core/tests.py"
tree = ast.parse(open(path, encoding="utf-8").read())
duplicates = []
classes = 0
for node in ast.walk(tree):
    if not isinstance(node, ast.ClassDef):
        continue
    classes += 1
    counts = collections.Counter(
        child.name for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)))
    for name, count in counts.items():
        if count > 1:
            duplicates.append(f"{node.name}.{name} defined {count}x")

print(f"classes={classes} duplicate_method_names={len(duplicates)}")
for line in duplicates:
    print("  DUPLICATE:", line)
