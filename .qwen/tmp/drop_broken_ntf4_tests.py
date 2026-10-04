"""Drop the broken NTF-4 test class so it can be rewritten.

The class was written in one pass and carries the defects a second pair of eyes catches immediately:
three `x if False else y` expressions that make an assertion compare a value against itself, bare
names with no import in the test that uses them, notices whose destination resolves to the recipient's
own address while the suppression was created against a different one, and a trailing comma that turns
an assertEqual into a tuple literal. Each of those is worse than no test, because it reports a pass.
"""
import io

PATH = "core/tests.py"
MARKER = "\n\nclass MessageConsentAndDeliveryLifecycleTest(TestCase):"

text = io.open(PATH, encoding="utf-8").read()
index = text.index(MARKER)
removed = len(text) - index
# The tail we are removing must be the class we wrote and nothing after it.
assert "MessageConsentAndDeliveryLifecycleTest" in text[index:index + 200]
assert text.count("class MessageConsentAndDeliveryLifecycleTest") == 1
io.open(PATH, "w", encoding="utf-8", newline="").write(text[:index] + "\n")
print(f"removed_chars={removed} kept_lines={len(text[:index].splitlines())}")
print("tail now:")
print(text[:index][-260:])
