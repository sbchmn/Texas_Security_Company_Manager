import os
import sys
import time

sys.path.insert(0, "/app")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.argv = ["totp_code.py"]

import django

django.setup()

from django.contrib.auth import get_user_model
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import format_hotp_value, hotp_value

user = get_user_model().objects.get(username="owner@example.com")
auth = Authenticator.objects.get(user=user, type=Authenticator.Type.TOTP)
secret = auth.data["secret"]
counter = int(time.time()) // 30
code = format_hotp_value(hotp_value(secret, counter))
remaining = 30 - (int(time.time()) % 30)
print(code, remaining)
