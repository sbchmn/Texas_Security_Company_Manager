"""Throwaway live probe: does the server-side session keep the enrollment secret between
the enrollment GET and POST on the running compose stack? Submits a deliberately wrong code so
no authenticator is ever activated."""
import base64
import binascii
import json
import os
import pickle
import re
import subprocess
import sys

import requests
from dotenv import load_dotenv

load_dotenv(".env")
BASE = os.getenv("PROBE_BASE", "https://localhost:8443")
EMAIL = os.getenv("BOOTSTRAP_EMAIL", "").strip()
PASSWORD = os.getenv("BOOTSTRAP_PASSWORD", "")
DB = "texas_security_company_manager-db-1"
print("base:", BASE, "| login:", EMAIL, "| password set:", bool(PASSWORD))

SECRET_RE = re.compile(r'value="([A-Z2-7]{16,})"')


def sql(query):
    """Run a read-only query through the db container's own mysql client."""
    inner = f'mysql -N -B -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e {json.dumps(query)}'
    out = subprocess.run(["docker", "exec", DB, "sh", "-c", inner], capture_output=True, text=True, check=False)
    if out.returncode:
        print("   mysql error:", out.stderr.strip()[:300])
    return out.stdout


def session_payload(session_key):
    raw = sql(f"SELECT session_data FROM django_session WHERE session_key='{session_key}'").strip()
    if not raw:
        return None
    for candidate in (raw,):
        try:
            return pickle.loads(base64.b64decode(candidate.encode()))
        except (pickle.UnpicklingError, binascii.Error, EOFError, UnicodeDecodeError, ValueError):
            pass
    try:
        return json.loads(raw)
    except ValueError:
        return {"_unparsed_prefix": raw[:40]}


def shown_secret(response):
    found = SECRET_RE.findall(response.text)
    return found[0] if found else None


client = requests.Session()
client.verify = False
login_page = client.get(f"{BASE}/accounts/login/", timeout=20)
print("1. login page ->", login_page.status_code, "| themed:", "login-shell" in login_page.text)

try:
    landed = client.post(
        f"{BASE}/accounts/login/",
        {"login": EMAIL, "password": PASSWORD, "next": "/"},
        timeout=20,
        allow_redirects=True,
    )
except requests.TooManyRedirects:
    print("2. LOGIN -> TOO MANY REDIRECTS (the enrollment/reauthentication loop)")
    sys.exit(0)

print("2. after login ->", landed.status_code, landed.url)
print("   redirect chain:", [(r.status_code, r.headers.get("Location")) for r in landed.history])
print("   themed (app.css link):", "css/app.css" in landed.text, "| page has QR:", "totp" in landed.text.lower())

key = client.cookies.get("django_session")
payload = session_payload(key)
print("3. server session keys:", sorted((payload or {}).keys()))
print("   session secret:", str((payload or {}).get("mfa.totp.secret"))[:8])

secret_a = shown_secret(landed)
print("4. secret shown in the page:", secret_a[:8] if secret_a else None)
print("   page and session secret agree:", secret_a == (payload or {}).get("mfa.totp.secret"))

bad = client.post(landed.url, {"code": "000000", "csrfmiddlewaretoken": client.cookies.get("csrftoken")}, timeout=20, headers={"Referer": landed.url})
payload_after = session_payload(key)
secret_b = shown_secret(bad)
print("5. wrong-code POST ->", bad.status_code, bad.url)
print("   error 'incorrect code' present:", "incorrect" in bad.text.lower())
print("   session secret after:", str((payload_after or {}).get("mfa.totp.secret"))[:8])
print("   secret shown after:", secret_b[:8] if secret_b else None)
print("   DISCRIMINATOR: session survived the POST ->", (payload_after or {}).get("mfa.totp.secret") == secret_a)

print("6. authenticators on the account:", sql("SELECT type, LEFT(data,12) FROM allauth_mfa_authenticator") or "(none)")
sql(f"DELETE FROM django_session WHERE session_key='{key}'")
print("7. probe session row removed")
