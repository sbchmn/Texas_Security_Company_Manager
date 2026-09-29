"""Django settings for the Texas Security Company Manager."""
from pathlib import Path
import os
import sys

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
IS_TEST = "test" in sys.argv
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG or IS_TEST:
        SECRET_KEY = "unsafe-development-key-change-me"  # nosec B105
    else:
        raise ImproperlyConfigured("SECRET_KEY is required when DEBUG is false")

default_hosts = "localhost,127.0.0.1,testserver" if DEBUG or IS_TEST else ""
PLATFORM_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", default_hosts).split(",") if h.strip()]
ALLOWED_HOSTS = ["*"]
if not PLATFORM_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS is required when DEBUG is false")
CSRF_TRUSTED_ORIGINS = [u.strip() for u in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if u.strip()]

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
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"

if os.getenv("MYSQL_DATABASE"):
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.environ["MYSQL_DATABASE"],
        "USER": os.getenv("MYSQL_USER", "tscm"),
        "PASSWORD": os.getenv("MYSQL_PASSWORD", "tscm"),
        "HOST": os.getenv("MYSQL_HOST", "db"),
        "PORT": os.getenv("MYSQL_PORT", "3306"),
        "OPTIONS": {"charset": "utf8mb4"},
        "CONN_MAX_AGE": 60,
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
SECURE_HSTS_SECONDS = 0 if DEBUG else int(os.getenv("SECURE_HSTS_SECONDS", "3600"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
AUTH_RATE_LIMIT_MAX = int(os.getenv("AUTH_RATE_LIMIT_MAX", "5"))
AUTH_RATE_LIMIT_WINDOW = int(os.getenv("AUTH_RATE_LIMIT_WINDOW", "900"))

MALWARE_SCAN_MODE = os.getenv("MALWARE_SCAN_MODE", "basic" if DEBUG or IS_TEST else "clamav")
CLAMAV_HOST = os.getenv("CLAMAV_HOST", "")
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
ACCOUNT_SIGNUP_ENABLED = False
ACCOUNT_LOGIN_METHODS = {"email", "username"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "username*", "password1*", "password2*"]
MFA_SUPPORTED_TYPES = ["totp", "recovery_codes"]
MFA_TOTP_ISSUER = "Texas Security Company Manager"
SOCIALACCOUNT_LOGIN_ON_GET = False
SOCIALACCOUNT_EMAIL_AUTHENTICATION = False
SOCIALACCOUNT_PROVIDERS = {
    "google": {"APP": {"client_id": os.getenv("GOOGLE_OIDC_CLIENT_ID", ""), "secret": os.getenv("GOOGLE_OIDC_CLIENT_SECRET", ""), "key": ""}, "SCOPE": ["profile", "email"], "AUTH_PARAMS": {"access_type": "online"}},
    "microsoft": {"APP": {"client_id": os.getenv("MICROSOFT_OIDC_CLIENT_ID", ""), "secret": os.getenv("MICROSOFT_OIDC_CLIENT_SECRET", ""), "key": ""}, "TENANT": os.getenv("MICROSOFT_OIDC_TENANT", "common")},
}

REDIS_URL = os.getenv("REDIS_URL", "")
if not REDIS_URL and not (DEBUG or IS_TEST):
    raise ImproperlyConfigured("REDIS_URL is required in production for shared rate limiting and coordination")
CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}} if REDIS_URL else {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
AWS_QUERYSTRING_EXPIRE = 300
AWS_S3_FILE_OVERWRITE = False
AWS_DEFAULT_ACL = None
