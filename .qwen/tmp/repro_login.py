import re
import sys

import requests
import urllib3
from dotenv import dotenv_values

urllib3.disable_warnings()

CODE = sys.argv[1] if len(sys.argv) > 1 else None
BASE = "https://localhost:8443"

env = dotenv_values(".env")
login_id = env["BOOTSTRAP_EMAIL"]
password = env["BOOTSTRAP_PASSWORD"]

session = requests.Session()
session.verify = False


def csrf_token(html):
    match = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', html)
    return match.group(1) if match else None


def describe(label, response):
    links = re.findall(r'<link[^>]+href="([^"]+)"[^>]*>', response.text)
    title = re.search(r"<title>(.*?)</title>", response.text, re.S)
    print("--- %s ---" % label)
    print("url:", response.url)
    print("status:", response.status_code)
    print("title:", title.group(1).strip() if title else None)
    print("linked assets:", links[:8])
    if "Incorrect code" in response.text:
        print("PAGE CONTAINS: Incorrect code")
    if "too many" in response.text.lower():
        print("PAGE CONTAINS: rate-limit message")


response = session.get(BASE + "/accounts/login/")
print("history:", [r.status_code for r in response.history])
token = csrf_token(response.text)
describe("login page", response)
print("csrf token present:", bool(token))
print("cookies:", [(c.name, c.secure) for c in session.cookies])

data = {"csrfmiddlewaretoken": token, "login": login_id, "password": password}
headers = {"Referer": BASE + "/accounts/login/"}
response = session.post(BASE + "/accounts/login/", data=data, headers=headers)
print("login post history:", [(r.status_code, r.headers.get("Location")) for r in response.history])
describe("after password stage", response)

if "/accounts/2fa/authenticate/" not in response.url:
    print("did not land on the 2FA screen; stopping")
    sys.exit(0)

if CODE is None:
    print("no code supplied; stopping before the 2FA post")
    sys.exit(0)

token = csrf_token(response.text)
data = {"csrfmiddlewaretoken": token, "code": CODE}
headers = {"Referer": response.url}
response = session.post(response.url, data=data, headers=headers)
print("2fa post history:", [(r.status_code, r.headers.get("Location")) for r in response.history])
describe("after 2FA code", response)
print("cookies:", [(c.name, c.secure) for c in session.cookies])
