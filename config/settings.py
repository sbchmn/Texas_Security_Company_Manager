"""Django settings for the Texas Security Company Manager."""
from pathlib import Path
import os
import re
import sys

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# The release number lives in one file so the image, the About page and any release tag agree.
APP_VERSION = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip() if (BASE_DIR / "VERSION").exists() else "unversioned"
APP_NAME = "Texas Security Company Manager"
APP_PUBLISHER = {"name": "Bachman Group, LLC", "address": "PO Box 4, Lancaster, TX 75146",
                 "url": "https://bachman.xyz", "copyright_since": 2026}
# DOTENV_PATH lets CI or a container point at a different env file; the test runner uses it
# to avoid importing a developer's deployment values.
load_dotenv(os.getenv("DOTENV_PATH") or BASE_DIR / ".env")
TIMESHEET_MAP_PROVIDER = os.getenv("TIMESHEET_MAP_PROVIDER", "disabled").lower()
GOOGLE_MAPS_BROWSER_KEY = os.getenv("GOOGLE_MAPS_BROWSER_KEY", "")
TIMESHEET_MAP_TILE_URL = os.getenv("TIMESHEET_MAP_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
TIMESHEET_MAP_ATTRIBUTION = os.getenv("TIMESHEET_MAP_ATTRIBUTION", "© OpenStreetMap contributors")
if TIMESHEET_MAP_PROVIDER not in ("disabled", "osm", "google"):
    raise ImproperlyConfigured("TIMESHEET_MAP_PROVIDER must be disabled, osm, or google.")
if TIMESHEET_MAP_PROVIDER == "google" and not GOOGLE_MAPS_BROWSER_KEY:
    raise ImproperlyConfigured("Google timesheet maps require GOOGLE_MAPS_BROWSER_KEY.")
if TIMESHEET_MAP_PROVIDER == "osm":
    from urllib.parse import urlsplit
    tile_origin = urlsplit(TIMESHEET_MAP_TILE_URL)
    if tile_origin.scheme != "https" or not tile_origin.hostname or tile_origin.username or tile_origin.password or "*" in tile_origin.netloc:
        raise ImproperlyConfigured("Timesheet tiles require an HTTPS URL without credentials or host wildcards.")
    if not all(part in TIMESHEET_MAP_TILE_URL for part in ("{z}", "{x}", "{y}")):
        raise ImproperlyConfigured("The tile URL must include {z}, {x}, and {y}.")
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "require_debug_false": {"()": "django.utils.log.RequireDebugFalse"},
        "credential_paths": {"()": "core.logging.CredentialPathRedactionFilter"},
    },
    "formatters": {
        "standard": {"()": "core.logging.CredentialPathRedactionFormatter",
                     "format": "{asctime} {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "level": "WARNING",
            "formatter": "standard",
            "filters": ["credential_paths"],
        },
        "mail_admins": {
            "class": "core.logging.CredentialPathRedactionEmailHandler",
            "level": "ERROR",
            "filters": ["require_debug_false", "credential_paths"],
        },
    },
    "loggers": {
        "django": {
            "handlers": ["console", "mail_admins"],
            "level": "INFO",
            "propagate": False,
        },
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
}
IS_TEST = "test" in sys.argv
# A test run must not depend on the live Redis/MySQL/ClamAV named in whatever .env happens to
# be present. Set TEST_LIVE_SERVICES=true (CI does) to exercise the real services instead.
HERMETIC_TEST = IS_TEST and os.getenv("TEST_LIVE_SERVICES", "").lower() != "true"
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG or IS_TEST:
        SECRET_KEY = "unsafe-development-key-change-me"  # nosec B105
    else:
        raise ImproperlyConfigured("SECRET_KEY is required when DEBUG is false")

default_hosts = "localhost,127.0.0.1,testserver" if DEBUG or IS_TEST else ""
PLATFORM_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", default_hosts).split(",") if h.strip()]
# Django's test client always presents the "testserver" host; the project allowlist has to
# agree with it even when ALLOWED_HOSTS was supplied explicitly.
if IS_TEST and "testserver" not in PLATFORM_HOSTS:
    PLATFORM_HOSTS.append("testserver")
ALLOWED_HOSTS = ["*"]
if not PLATFORM_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS is required when DEBUG is false")
CSRF_TRUSTED_ORIGINS = [u.strip() for u in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if u.strip()]
# Where links in text messages point (core.sms.public_base_url); a verified company domain wins.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
PWA_NAME = os.getenv("PWA_NAME", APP_NAME).strip()
PWA_SHORT_NAME = os.getenv("PWA_SHORT_NAME", "TSCM").strip()
ANDROID_PACKAGE_ID = os.getenv("ANDROID_PACKAGE_ID", "com.texaslibertycoalition.tscm").strip()
ANDROID_SHA256_FINGERPRINTS = [value.strip().upper() for value in
                             os.getenv("ANDROID_SHA256_FINGERPRINTS", "").split(",") if value.strip()]
if not PWA_NAME or not PWA_SHORT_NAME or len(PWA_SHORT_NAME) > 30:
    raise ImproperlyConfigured("PWA_NAME is required and PWA_SHORT_NAME must contain 1-30 characters.")
if not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", ANDROID_PACKAGE_ID):
    raise ImproperlyConfigured("ANDROID_PACKAGE_ID must be a reverse-DNS Android application ID.")
if any(not re.fullmatch(r"(?:[0-9A-F]{2}:){31}[0-9A-F]{2}", value) for value in ANDROID_SHA256_FINGERPRINTS):
    raise ImproperlyConfigured("ANDROID_SHA256_FINGERPRINTS requires comma-separated colon-delimited SHA-256 fingerprints.")
PERSONNEL_ENCRYPTION_KEYS = tuple(key.strip() for key in os.getenv("PERSONNEL_ENCRYPTION_KEYS", "").split(",") if key.strip())

INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "django.contrib.sites",
    "allauth", "allauth.account", "allauth.mfa", "allauth.socialaccount",
    "allauth.socialaccount.providers.google", "allauth.socialaccount.providers.microsoft",
    "core",
]
MIDDLEWARE = [
    "core.middleware.VerifiedHostMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "core.middleware.ResponseSecurityHeadersMiddleware",
    "core.middleware.AdminAccessMiddleware",
    "core.middleware.LoginRateLimitMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "core.middleware.TenantContextMiddleware",
    "core.middleware.RequiredMfaMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "core.context_processors.organization_brand",
        "core.context_processors.navigation",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"

if os.getenv("MYSQL_DATABASE") and not HERMETIC_TEST:
    from .db_tls import mysql_tls_options
    _mysql_options = {"charset": "utf8mb4", **mysql_tls_options()}
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.environ["MYSQL_DATABASE"],
        "USER": os.getenv("MYSQL_USER", "tscm"),
        "PASSWORD": os.getenv("MYSQL_PASSWORD", "tscm"),
        "HOST": os.getenv("MYSQL_HOST", "db"),
        "PORT": os.getenv("MYSQL_PORT", "3306"),
        "OPTIONS": _mysql_options,
        # A test run wraps each case in a transaction; an idle-connection expiry then closes
        # the connection from request_finished inside that transaction and poisons it.
        "CONN_MAX_AGE": 0 if IS_TEST else int(os.getenv("CONN_MAX_AGE", "60")),
    }}
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("TIME_ZONE", "America/Chicago")
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
if os.getenv("AWS_STORAGE_BUCKET_NAME"):
    STORAGES = {
        "default": {"BACKEND": "storages.backends.s3.S3Storage", "OPTIONS": {"bucket_name": os.environ["AWS_STORAGE_BUCKET_NAME"], "endpoint_url": os.getenv("AWS_S3_ENDPOINT_URL") or None, "region_name": os.getenv("AWS_S3_REGION_NAME") or None, "default_acl": "private", "querystring_auth": True, "file_overwrite": False}},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    }
else:
    STORAGES = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}, "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage" if (DEBUG or IS_TEST) else "whitenoise.storage.CompressedManifestStaticFilesStorage"}}
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"
# Trust the forwarded protocol only when a controlled TLS-terminating proxy is in
# front (compose Caddy, App Platform edge); unset fails closed for direct exposure.
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https")
    if os.getenv("TRUST_PROXY_SSL_HEADER", "").strip().lower() == "true"
    else None
)
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG and not IS_TEST
SECURE_HSTS_SECONDS = 0 if DEBUG else int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
AUTH_RATE_LIMIT_MAX = int(os.getenv("AUTH_RATE_LIMIT_MAX", "5"))
AUTH_RATE_LIMIT_WINDOW = int(os.getenv("AUTH_RATE_LIMIT_WINDOW", "900"))
# Peer CIDRs allowed to supply X-Forwarded-For (the deployment's TLS proxy or app edge).
# Left empty, no forwarded header is trusted and every client shares one rate-limit bucket,
# so a public deployment behind a proxy must set this.
TRUSTED_PROXIES = [item.strip() for item in os.getenv("TRUSTED_PROXIES", "").split(",") if item.strip()]
# Source ranges permitted to reach /admin/. With DEBUG=false and no entries the admin is
# closed to every address; with DEBUG=true the restriction is off for local development.
ADMIN_ALLOWED_IPS = [item.strip() for item in os.getenv("ADMIN_ALLOWED_IPS", "").split(",") if item.strip()]

MALWARE_SCAN_MODE = "basic" if HERMETIC_TEST else os.getenv("MALWARE_SCAN_MODE", "basic" if DEBUG or IS_TEST else "clamav")
CLAMAV_HOST = "" if HERMETIC_TEST else os.getenv("CLAMAV_HOST", "")
CLAMAV_PORT = int(os.getenv("CLAMAV_PORT", "3310"))

DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "")
MAILJET_API_KEY = os.getenv("MAILJET_API_KEY", "")
MAILJET_SECRET_KEY = os.getenv("MAILJET_SECRET_KEY", "")
POSTMARK_SERVER_TOKEN = os.getenv("POSTMARK_SERVER_TOKEN", "")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")
SITE_ID = 1
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend", "allauth.account.auth_backends.AuthenticationBackend"]
ACCOUNT_ADAPTER = "core.auth_adapters.InvitationOnlyAccountAdapter"
SOCIALACCOUNT_ADAPTER = "core.auth_adapters.InvitationOnlySocialAccountAdapter"
ACCOUNT_LOGIN_METHODS = {"email", "username"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "username*", "password1*", "password2*"]
MFA_SUPPORTED_TYPES = ["totp", "recovery_codes"]
MFA_TOTP_ISSUER = "Texas Security Company Manager"
# Operator-controlled destinations, not arbitrary URLs entered by a tenant administrator.
DOCUSEAL_ALLOWED_ORIGINS = [
    value.strip().rstrip("/") for value in os.getenv("DOCUSEAL_ALLOWED_ORIGINS", "").split(",") if value.strip()
]
# Optional PEM CA file trusted only for DocuSeal requests, e.g. Caddy's local root during testing.
# A hermetic test run ignores it: the path names a file inside the containers, not on a dev machine.
DOCUSEAL_CA_BUNDLE = "" if HERMETIC_TEST else os.getenv("DOCUSEAL_CA_BUNDLE", "").strip()
SOCIALACCOUNT_LOGIN_ON_GET = False
SOCIALACCOUNT_EMAIL_AUTHENTICATION = False
SOCIALACCOUNT_PROVIDERS = {
    "google": {"APP": {"client_id": os.getenv("GOOGLE_OIDC_CLIENT_ID", ""), "secret": os.getenv("GOOGLE_OIDC_CLIENT_SECRET", ""), "key": ""}, "SCOPE": ["profile", "email"], "AUTH_PARAMS": {"access_type": "online"}},
    "microsoft": {"APP": {"client_id": os.getenv("MICROSOFT_OIDC_CLIENT_ID", ""), "secret": os.getenv("MICROSOFT_OIDC_CLIENT_SECRET", ""), "key": ""}, "TENANT": os.getenv("MICROSOFT_OIDC_TENANT", "common")},
}

REDIS_URL = "" if HERMETIC_TEST else os.getenv("REDIS_URL", "")
if not REDIS_URL and not (DEBUG or IS_TEST):
    raise ImproperlyConfigured("REDIS_URL is required in production for shared rate limiting and coordination")
CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}} if REDIS_URL else {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
AWS_QUERYSTRING_EXPIRE = 300
AWS_S3_FILE_OVERWRITE = False
AWS_DEFAULT_ACL = None

# Object storage renders brand logos from the bucket host, so the CSP has to name it or the
# image is blocked; path-style and virtual-host-style addressing are both allowed.
def _storage_img_src():
    bucket = os.getenv("AWS_STORAGE_BUCKET_NAME", "")
    if not bucket:
        return []
    endpoint = os.getenv("AWS_S3_ENDPOINT_URL") or f"https://s3.{os.getenv('AWS_S3_REGION_NAME', 'us-east-1')}.amazonaws.com"
    host = endpoint.split("://", 1)[-1].split("/")[0]
    return [f"https://{host}", f"https://{bucket}.{host}"]

MEDIA_IMG_SRC = _storage_img_src()
