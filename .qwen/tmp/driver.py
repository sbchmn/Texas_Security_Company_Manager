"""End-to-end 2FA login probe against the running compose stack."""
import base64
import hashlib
import hmac
import re
import struct
import sys
import time
import warnings

import requests
import urllib3

urllib3.disable_warnings(urllib3.InsecureRequestWarning)

BASE = "https://10.12.1.40:8443"
LOGIN = "probe-totp"
PASSWORD = "Probe-2fa-2026-x"
SECRET = "JBSWY3DPEHPK3PXP"
PERIOD = 30
DIGITS = 6


def hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", bytearray(digest[offset:offset + 4]))[0] & 0x7FFFFFFF
    return f"{value % 10**DIGITS:0{DIGITS}}"


def code(step_offset: int) -> str:
    return hotp(SECRET, int(time.time()) // PERIOD + step_offset)


def csrf(session, url):
    r = session.get(BASE + url, verify=False, timeout=15)
    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r.text)
    return r, (m.group(1) if m else None)


def main():
    session = requests.Session()
    r, token = csrf(session, "/accounts/login/")
    print("STEP get_login", r.status_code, "csrf_found", bool(token))

    r = session.post(
        BASE + "/accounts/login/",
        data={"csrfmiddlewaretoken": token, "login": LOGIN, "password": PASSWORD, "next": "/"},
        headers={"Referer": BASE + "/accounts/login/", "Origin": BASE},
        allow_redirects=False,
        verify=False,
        timeout=15,
    )
    print("STEP post_password", r.status_code, "location", r.headers.get("Location"))
    stage = r.headers.get("Location") or ""
    if "2fa" not in stage:
        print("RESULT no_mfa_stage_reached")
        return

    r, token = csrf(session, stage)
    print("STEP get_mfa_stage", r.status_code, "path", r.url)

    label = sys.argv[1] if len(sys.argv) > 1 else "run"
    for step, note in ((-1, "previous_step_code"), (0, "current_step_code")):
        submitted = code(step)
        r = session.post(
            BASE + stage,
            data={"csrfmiddlewaretoken": token, "code": submitted, "next": "/"},
            headers={"Referer": BASE + stage, "Origin": BASE},
            allow_redirects=False,
            verify=False,
            timeout=15,
        )
        body = r.text if r.status_code == 200 else ""
        err = re.findall(r"(Incorrect code|incorrect|too many|Cannot log in)", body, re.I)
        print(
            f"STEP post_{note} [{label}]",
            "sent", submitted,
            "server_now", int(time.time()),
            "step_now", int(time.time()) // PERIOD,
            "status", r.status_code,
            "location", r.headers.get("Location"),
            "errors", err[:3],
        )
        if r.status_code in (301, 302, 303):
            return
        # Re-read the CSRF token from the re-rendered stage page.
        m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', body)
        token = m.group(1) if token and m else token


if __name__ == "__main__":
    main()
