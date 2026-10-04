import os
import sys

sys.path.insert(0, "/app")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.base_session import AbstractBaseSession
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator

User = get_user_model()
print("SESSION_ENGINE", settings.SESSION_ENGINE)
print("SESSION_COOKIE_NAME", settings.SESSION_COOKIE_NAME)
print("SESSION_COOKIE_SECURE", settings.SESSION_COOKIE_SECURE)
print("CSRF_COOKIE_SECURE", settings.CSRF_COOKIE_SECURE)
print("REAUTH_TIMEOUT", getattr(settings, "ACCOUNT_REAUTHENTICATION_TIMEOUT", "unset"))
print("users:")
for u in User.objects.all()[:8]:
    print("  ", u.pk, u.username, "staff", u.is_staff, "super", u.is_superuser, "last_login", u.last_login)
print("authenticators:")
for a in Authenticator.objects.all()[:20]:
    keys = sorted((a.data or {}).keys())
    print("  ", a.pk, "user", a.user_id, a.type, a.created_at, "data_keys", keys)
print("emails:")
for e in EmailAddress.objects.all()[:10]:
    print("  ", e.pk, "user", e.user_id, e.email, "verified", e.verified, "primary", e.primary)
print("sessions (newest 12):")
for s in AbstractBaseSession.objects.order_by("-expire_date")[:12]:
    print("  ", s.session_key[:6], "expires", s.expire_date)
