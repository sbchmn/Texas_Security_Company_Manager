"""Restricted personnel data, kept out of normal profile history and exports."""
import json
import re

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ValidationError
from django.views.decorators.debug import sensitive_variables

from .models import Membership

PRIVATE_PERSONNEL_ROLES = (Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR)
PRIVATE_FIELDS = ("ssn", "driver_license_number", "driver_license_state")


@sensitive_variables()
def cipher():
    keys = getattr(settings, "PERSONNEL_ENCRYPTION_KEYS", ())
    if not keys:
        raise ValidationError("Restricted personnel storage requires PERSONNEL_ENCRYPTION_KEYS. Ask the deployment administrator to configure it.")
    try:
        return MultiFernet([Fernet(key.encode("ascii")) for key in keys])
    except (ValueError, UnicodeError) as exc:
        raise ValidationError("PERSONNEL_ENCRYPTION_KEYS contains an invalid encryption key.") from exc


@sensitive_variables()
def normalize_ssn(value):
    digits = re.sub(r"[- ]", "", value.strip())
    if not re.fullmatch(r"\d{9}", digits, flags=re.ASCII):
        raise ValidationError("Enter nine SSN digits, optionally separated by hyphens.")
    if digits[:3] in ("000", "666") or int(digits[:3]) >= 900 or digits[3:5] == "00" or digits[5:] == "0000":
        raise ValidationError("That SSN contains an invalid area, group, or serial number.")
    return digits


@sensitive_variables()
def encrypt_details(person, values):
    payload = {"person": str(person.pk), "organization": str(person.organization_id),
               "values": {key: values.get(key, "") for key in PRIVATE_FIELDS}}
    return cipher().encrypt(json.dumps(payload).encode("utf-8")).decode("ascii")


@sensitive_variables()
def decrypt_details(person, record):
    if record is None or not record.encrypted_payload:
        return dict.fromkeys(PRIVATE_FIELDS, "")
    try:
        payload = json.loads(cipher().decrypt(record.encrypted_payload.encode("ascii")).decode("utf-8"))
    except (InvalidToken, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("Restricted personnel data cannot be decrypted. Restore the correct encryption key; do not overwrite the record.") from exc
    if (not isinstance(payload, dict) or payload.get("person") != str(person.pk)
            or payload.get("organization") != str(person.organization_id)
            or not isinstance(payload.get("values"), dict)):
        raise ValidationError("Restricted personnel data does not belong to this personnel record.")
    values = payload["values"]
    if any(not isinstance(values.get(key, ""), str) for key in PRIVATE_FIELDS):
        raise ValidationError("Restricted personnel data has an invalid format.")
    return {key: values.get(key, "") for key in PRIVATE_FIELDS}
