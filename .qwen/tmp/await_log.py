"""Block until the background suite writes its summary line, then print it.

Polling the status file and grepping the log for the runner's own summary rather than tailing, because
Django's test runner writes its verdict at the end and the shell buffers stdout when it is not attached
to a TTY — an empty output file while the process is alive is normal, not a hang.
"""
import io
import re
import sys
import time

status = sys.argv[1]
output = sys.argv[2]
deadline = time.time() + float(sys.argv[3] if len(sys.argv) > 3 else 900)

pattern = re.compile(r"^(Ran \d+ tests|OK|FAILED|ERROR: \w|FAIL: \w)", re.M)
while time.time() < deadline:
    try:
        body = io.open(output, encoding="utf-8", errors="replace").read()
    except OSError:
        body = ""
    if pattern.search(body):
        print(body[-4000:])
        sys.exit(0)
    state = ""
    try:
        state = io.open(status, encoding="utf-8").read()
    except OSError:
        pass
    if '"status": "failed"' in state or '"status": "cancelled"' in state:
        print(f"runner state: {state}\n{body[-3000:]}")
        sys.exit(1)
    time.sleep(15)
print("TIMEOUT waiting for the summary line")
sys.exit(2)
