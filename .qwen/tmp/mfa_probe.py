import time

from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import (
    format_hotp_value,
    hotp_value,
    validate_totp_code,
    yield_hotp_counters_from_time,
)
from allauth.mfa import app_settings
from django.contrib.auth import get_user_model

print("TOTP_PERIOD", app_settings.TOTP_PERIOD)
print("TOTP_DIGITS", app_settings.TOTP_DIGITS)
print("TOTP_TOLERANCE", app_settings.TOTP_TOLERANCE)
print("now_epoch", int(time.time()))
print("users", get_user_model().objects.count())
for auth in Authenticator.objects.order_by("id"):
    print(
        "AUTH id=%s user=%s type=%s created=%s last_used=%s data_keys=%s"
        % (
            auth.id,
            auth.user.username or auth.user.pk,
            auth.type,
            auth.created_at.isoformat(),
            auth.last_used_at.isoformat() if auth.last_used_at else None,
            sorted(auth.data.keys()),
        )
    )
    if auth.type == Authenticator.Type.TOTP:
        secret = auth.data["secret"]
        counters = list(yield_hotp_counters_from_time())
        print("  server_step_counter", counters[0])
        window = []
        for c in range(counters[0] - 2, counters[0] + 3):
            window.append((c, format_hotp_value(hotp_value(secret, c))))
        print("  codes_c-2..c+2", window)
        # What the app would accept right now:
        cur = format_hotp_value(hotp_value(secret, counters[0]))
        print("  accepted_now_current_step", cur)
        print("  accepted_now_wide_t1", [format_hotp_value(hotp_value(secret, c)) for c in (counters[0]-1, counters[0], counters[0]+1)])
