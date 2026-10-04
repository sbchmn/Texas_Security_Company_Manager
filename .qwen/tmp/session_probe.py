import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, "/app")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

from django.conf import settings
from django.contrib.sessions.models import Session
from importlib import import_module

Engine = import_module(settings.SESSION_ENGINE)
SessionStore = Engine.SessionStore
from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.account.internal.flows.reauthentication import STATE_SESSION_KEY
from allauth.mfa.totp.internal.auth import SECRET_SESSION_KEY

print("now", int(time.time()), datetime.now(timezone.utc).isoformat())
print("REAUTHENTICATION_TIMEOUT", settings.ACCOUNT_REAUTHENTICATION_TIMEOUT
      if hasattr(settings, "ACCOUNT_REAUTHENTICATION_TIMEOUT") else "attr-missing")
from allauth.account import app_settings as acc
print("allauth REAUTH_TIMEOUT", acc.REAUTHENTICATION_TIMEOUT)
print("account sessions:", Session.objects.count())
for s in Session.objects.order_by("-expire_date")[:15]:
    try:
        d = SessionStore(session_key=s.session_key).load() or {}
    except Exception as exc:
        print("  ", s.session_key[:8], "UNDECODEABLE", type(exc).__name__)
        continue
    auth = d.get(AUTHENTICATION_METHODS_SESSION_KEY)
    st = d.get(STATE_SESSION_KEY)
    secret = d.get(SECRET_SESSION_KEY)
    print("  ", s.session_key[:8], "user", d.get("_auth_user_id"),
          "expires", s.expire_date.isoformat(timespec="seconds"))
    print("      keys", sorted(d.keys()))
    if auth:
        print("      auth_methods", auth)
    if secret:
        print("      totp_secret_present len", len(secret), "first4", secret[:4])
    if st:
        print("      reauth_state", {k: (str(v)[:120]) for k, v in st.items()})
