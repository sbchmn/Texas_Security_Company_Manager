"""Throwaway diagnostic: walk the real TOTP enrollment flow through the middleware stack."""
import os
import sys
import time

sys.argv = ["probe", "test"]  # makes settings.IS_TEST true -> hermetic SQLite + LocMem cache
sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.test.utils import setup_test_environment, get_runner  # noqa: E402

setup_test_environment()
runner = get_runner(settings)()
old_config = runner.setup_databases()

from allauth.account.authentication import AUTHENTICATION_METHODS_SESSION_KEY  # noqa: E402
from allauth.mfa.totp.internal import auth as totp_auth  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402
from core.models import Membership, Organization  # noqa: E402

User = get_user_model()
URL = None


def code_for(secret):
    counter = next(totp_auth.yield_hotp_counters_from_time())
    return totp_auth.format_hotp_value(totp_auth.hotp_value(secret, counter))


def new_user(tag, staff=True):
    org, _ = Organization.objects.get_or_create(slug="probe", defaults={"legal_name": "Probe LLC", "display_name": "Probe"})
    return User.objects.create_user(
        username=f"{tag}@example.com", email=f"{tag}@example.com",
        password="probe-pass-1234", is_staff=staff,
    ), org


def login_client(user):
    client = Client()
    client.post(reverse("account_login"), {"login": user.username, "password": "probe-pass-1234"})
    return client


def walk(client, path, limit=8):
    """Follow redirects the way a browser would, printing each hop."""
    seen = []
    for _ in range(limit):
        response = client.get(path)
        seen.append((path, response.status_code, response.headers.get("Location")))
        path = response.headers.get("Location") or ""
        if not path:
            break
    for hop in seen:
        print("     ", hop)
    return seen


print("URL:", reverse("mfa_activate_totp"), "| reauth:", reverse("account_reauthenticate"))

print("\n=== A: fresh login, enroll with the displayed secret ===")
user, _ = new_user("fresh")
client = login_client(user)
page = client.get(reverse("mfa_activate_totp"))
secret = client.session["mfa.totp.secret"]
post = client.post(reverse("mfa_activate_totp"), {"code": code_for(secret)})
print("  POST ->", post.status_code, post.headers.get("Location"))
from allauth.mfa.models import Authenticator  # noqa: E402
print("  authenticators:", list(Authenticator.objects.filter(user=user).values_list("type", flat=True)))

print("\n=== B: login, let the 300s reauth window expire, then submit ===")
user, _ = new_user("aged")
client = login_client(user)
page = client.get(reverse("mfa_activate_totp"))
secret = client.session["mfa.totp.secret"]
print("  secret shown:", secret[:6])
session = client.session
records = session[AUTHENTICATION_METHODS_SESSION_KEY]
records[-1]["at"] = time.time() - 301
session[AUTHENTICATION_METHODS_SESSION_KEY] = records
session.save()
post = client.post(reverse("mfa_activate_totp"), {"code": code_for(secret)})
print("  POST ->", post.status_code, post.headers.get("Location"))
print("  browser follows that chain:")
walk(client, post.headers.get("Location").split("?")[0])
print("  authenticators:", list(Authenticator.objects.filter(user=user).values_list("type", flat=True)))

print("\n=== C: session authenticated without an allauth record (core login()/force_login) ===")
user, _ = new_user("norecord")
client = Client()
client.force_login(user)
page = client.get(reverse("mfa_activate_totp"))
print("  GET ->", page.status_code, page.headers.get("Location"))
print("  browser follows that chain:")
walk(client, page.headers.get("Location") or reverse("mfa_activate_totp"))
print("  authenticators:", list(Authenticator.objects.filter(user=user).values_list("type", flat=True)))

runner.teardown_databases(old_config)
