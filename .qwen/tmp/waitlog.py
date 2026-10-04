"""Block until the authoritative full-suite run has written its summary, then print it.

Python block-buffers stdout when redirected, so the log shows only a handful of progress dots for most
of the run and the `Ran N tests` line appears in one flush at the end. Polling the file's tail during
that window proves nothing; this waits for the marker itself and prints the verdict lines verbatim.
"""
import io
import os
import re
import sys
import time

path = sys.argv[1] if len(sys.argv) > 1 else ".qwen/tmp/authoritative.log"
deadline = time.time() + float(sys.argv[2] if len(sys.argv) > 2 else 420)
seen = ""
while time.time() < deadline:
    if os.path.exists(path):
        with io.open(path, encoding="utf-8", errors="replace") as handle:
            seen = handle.read()
        if re.search(r"Ran \d+ tests", seen):
            break
    time.sleep(10)

summary = re.search(r"Ran \d+ tests[^\n]*\n[^\n]*\n?[^\n]*", seen)
print(summary.group(0).strip() if summary else "NO SUMMARY WITHIN THE WAIT WINDOW")
print("FAIL_BLOCKS", seen.count("FAIL:"), "ERROR_BLOCKS", seen.count("ERROR:"))
print("LOG_BYTES", len(seen))
