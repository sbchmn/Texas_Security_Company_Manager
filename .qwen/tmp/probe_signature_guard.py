"""Why did the Twilio signature guard not refuse an unsigned callback? No database needed.

Checks the two inputs the guard reads — the settings override's visibility and
`callback_is_signed` — and then re-evaluates the view's own condition expression, because the
failure under test was "expected 403, got 200", which can only mean one of those three.
"""
import os
import sys

sys.path.insert(0, os.getcwd())
os.environ.setdefault("DOTENV_PATH", os.path.join(os.environ.get("TEMP", "/tmp"), "tscm-no-such-dotenv"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

sys.argv = ["probe", "test"]
django.setup()

from django.conf import settings  # noqa: E402
from django.test import override_settings, RequestFactory  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from core import views  # noqa: E402
from core.services import callback_is_signed  # noqa: E402
from core.models import Organization  # noqa: E402

setup_test_environment()
factory = RequestFactory()
print("views.settings is django.conf.settings:", views.settings is settings)
print("before override:", repr(getattr(settings, "TWILIO_AUTH_TOKEN", "")))

with override_settings(TWILIO_AUTH_TOKEN="explicit-token"):
    print("inside override:", repr(getattr(settings, "TWILIO_AUTH_TOKEN", "")))
    request = factory.post("/webhooks/twilio/abc/", {"MessageStatus": "delivered", "To": "+12145550142"})
    params = {key: request.POST.getlist(key)[0] for key in request.POST}
    print("content_type:", repr(request.content_type))
    print("params:", params)
    print("signature header:", repr(request.headers.get("X-Twilio-Signature", "")))
    print("callback_is_signed('twilio'):", callback_is_signed("twilio", request, params))
    print("callback_is_signed(Org.SmsProvider.TWILIO):",
          callback_is_signed(Organization.SmsProvider.TWILIO, request, params))
    provider = "twilio"
    signed = callback_is_signed(Organization.SmsProvider.TWILIO, request, params)
    print("view's guard expression would refuse:",
          provider == "twilio" and bool(getattr(settings, "TWILIO_AUTH_TOKEN", "")) and not signed)
    print("CALLBACK_PROVIDERS keys:", list(views.CALLBACK_PROVIDERS))
    print("mapped value:", repr(views.CALLBACK_PROVIDERS.get("twilio")))
