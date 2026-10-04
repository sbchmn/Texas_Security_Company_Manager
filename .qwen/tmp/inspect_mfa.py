import os
import sys

sys.path.insert(0, "/app")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.argv = ["inspect_mfa.py"]

import django

django.setup()

from django.contrib.auth import get_user_model
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import (
    TOTP,
    format_hotp_value,
    hotp_value,
    validate_totp_code,
    yield_hotp_counters_from_time,
)
from allauth.mfa import app_settings as mfa_settings

print("TOTP settings: period=%s digits=%s tolerance=%s" % (
    mfa_settings.TOTP_PERIOD, mfa_settings.TOTP_DIGITS, mfa_settings.TOTP_TOLERANCE,
))
print("supported types:", mfa_settings.SUPPORTED_TYPES)

User = get_user_model()
for user in User.objects.all().order_by("id"):
    print("user id=%s username=%r email=%r staff=%s superuser=%s last_login=%s" % (
        user.pk, user.username, user.email, user.is_staff, user.is_superuser, user.last_login,
    ))
    for auth in Authenticator.objects.filter(user=user):
        print("  authenticator id=%s type=%s created=%s last_used=%s" % (
            auth.pk, auth.type, getattr(auth, "created_at", None), getattr(auth, "last_used_at", None),
        ))
        if auth.type == Authenticator.Type.TOTP:
            wrapped = auth.wrap()
            secret = wrapped.instance.data.get("secret", "")
            print("  secret present=%s length=%s" % (bool(secret), len(secret)))
            counter = next(iter(yield_hotp_counters_from_time()))
            code = format_hotp_value(hotp_value(secret, counter))
            print("  server-side validate_totp_code(current code):", validate_totp_code(secret, code))
            print("  code-digit-count:", len(code))
