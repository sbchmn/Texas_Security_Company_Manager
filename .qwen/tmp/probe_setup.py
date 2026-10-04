import time

from django.contrib.auth import get_user_model
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import (
    format_hotp_value,
    hotp_value,
)

SECRET = "JBSWY3DPEHPK3PXP"  # RFC-prescribed demo secret, throwaway account only
User = get_user_model()

user, created = User.objects.get_or_create(
    username="probe-totp",
    defaults=dict(email="probe-totp@example.invalid", is_staff=False, is_superuser=False),
)
user.set_password("Probe-2fa-2026-x")
user.is_active = True
user.save()
if not EmailAddress.objects.filter(user=user).exists():
    EmailAddress.objects.create(user=user, email="probe-totp@example.invalid", verified=True, primary=True)

Authenticator.objects.filter(user=user).delete()
Authenticator.objects.create(user=user, type=Authenticator.Type.TOTP, data={"secret": SECRET})

counter = int(time.time()) // 30
codes = {
    "prev": format_hotp_value(hotp_value(SECRET, counter - 1)),
    "current": format_hotp_value(hotp_value(SECRET, counter)),
    "next": format_hotp_value(hotp_value(SECRET, counter + 1)),
}
print("PROBE_CREATED" if created else "PROBE_EXISTS", user.pk, user.username)
print("EPOCH", int(time.time()))
print("CODES", codes)
