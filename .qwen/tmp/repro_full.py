import re
import subprocess

import requests
import urllib3
from dotenv import dotenv_values

urllib3.disable_warnings()

BASE = "https://localhost:8443"
CONTAINER = "texas_security_company_manager-web-1"

env = dotenv_values(".env")
login_id = env["BOOTSTRAP_EMAIL"]
password = env["BOOTSTRAP_PASSWORD"]


def fresh_code():
    out = subprocess.run(
        ["docker", "exec", "-w", "/app", CONTAINER, "python", "/tmp/totp_code.py"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    code, remaining = out[0], int(out[1])
    if remaining < 6:
        out = subprocess.run(
            ["docker", "exec", "-w", "/app", CONTAINER, "python", "/tmp/totp_code.py"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        code, remaining = out[0], int(out[1])
    return code, remaining


def csrf_token(html):
    match = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', html)
    return match.group(1) if match else None


session = requests.Session()
session.verify = False

response = session.get(BASE + "/accounts/login/")
token = csrf_token(response.text)
response = session.post(
    BASE + "/accounts/login/",
    data={"csrfmiddlewaretoken": token, "login": login_id, "password": password},
    headers={"Referer": BASE + "/accounts/login/"},
)
print("password stage:", [(r.status_code, r.headers.get("Location")) for r in response.history], "->", response.url)
assert response.url.endswith("/accounts/2fa/authenticate/"), "expected 2FA screen"

code, remaining = fresh_code()
print("submitting server-derived code %s (window had %ss left)" % (code, remaining))
token = csrf_token(response.text)
response = session.post(
    response.url,
    data={"csrfmiddlewaretoken": token, "code": code},
    headers={"Referer": response.url},
)
print("2FA stage:", [(r.status_code, r.headers.get("Location")) for r in response.history], "->", response.url)
if "Incorrect code" in response.text:
    print("RESULT: server rejected its own current code")
else:
    title = re.search(r"<title>(.*?)</title>", response.text, re.S)
    print("RESULT: final page title:", title.group(1).strip() if title else response.status_code)
print("session cookie:", [(c.name, c.secure) for c in session.cookies])
