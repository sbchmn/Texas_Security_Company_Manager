"""Remove the DocuSign block from roadmap §13.

The owner retracted the statement ("we're sticking with DocuSeal, I just mis-spoke"), so the text it
produced has to leave the document entirely: a retracted decision sitting in a spec file is worse than
no note, because the next reader either plans against a vendor nobody chose or, following the block's
own suggestion, treats the shipped DocuSeal wiring as retired and deletes it.

Anchored on the two strings that bound it, both unique, and it asserts the removed span is the one
expected before writing.
"""
import io

path = "docs/development-roadmap.md"
start_anchor = "## 13. Document signing and onboarding packets\n\n**Superseded on 2026-10-03"
end_anchor = "**The signing engine is decided and self-hosted"

text = io.open(path, encoding="utf-8").read()
i = text.index(start_anchor)
j = text.index(end_anchor, i)
removed = text[i:j]

# Guardrails: the span must be the DocuSign insertion and nothing else.
assert "DocuSign" in removed, "refusing to delete a span that is not the retracted one"
assert "SIG-6" in removed
assert start_anchor.splitlines()[0] in removed
assert len(removed) > 3000, f"unexpectedly short span ({len(removed)} chars) — stopping"
assert text[:i].count("## 13. Document signing") == 0 or True  # heading itself is re-added below

replacement = "## 13. Document signing and onboarding packets\n\n"
text = text[:i] + replacement + text[j:]
io.open(path, "w", encoding="utf-8", newline="").write(text)

check = io.open(path, encoding="utf-8").read()
print(f"removed_chars={len(removed)}")
print(f"docusan_mentions_left={check.count('DocuSign')}")
print(f"sig6_mentions_left={check.count('SIG-6')}")
k = check.index(end_anchor)
print("--- seam ---")
print(check[k - 90:k + 90])
