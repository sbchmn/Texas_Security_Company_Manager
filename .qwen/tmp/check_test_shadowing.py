"""Guard for this repo's known trap: an appended class can silently shadow or delete methods.

core/tests.py is one huge file of adjacent TestCase classes, and a bad insertion anchor has before
swallowed an existing test while the new class still passed. This walks the AST and reports any
method name defined twice inside the same class, plus any class defined twice at module level.
"""
import ast
import io
import sys
from collections import Counter

path = "core/tests.py"
tree = ast.parse(io.open(path, encoding="utf-8").read())
problems = 0
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef):
        names = [item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for name, count in Counter(names).items():
            if count > 1:
                print(f"DUPLICATE METHOD {node.name}.{name} x{count}")
                problems += 1
top = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
for name, count in Counter(top).items():
    if count > 1:
        print(f"DUPLICATE CLASS {name} x{count}")
        problems += 1
classes = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
print(f"classes={len(classes)} methods_checked "
      f"tail={classes[-4:]}")
print("CLEAN" if not problems else f"{problems} problems")
sys.exit(1 if problems else 0)
