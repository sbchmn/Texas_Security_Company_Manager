import hashlib

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse


class LoginRateLimitMiddleware:
    """Bound local-login guessing without storing submitted identifiers in cache keys."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        key = None
        if request.method == "POST" and request.path == "/accounts/login/":
            submitted_login=request.POST.get("login") or request.POST.get("username", "")
            identity = f"{request.META.get('REMOTE_ADDR', '')}|{submitted_login.casefold()}"
            digest = hashlib.sha256(f"{settings.SECRET_KEY}|{identity}".encode()).hexdigest()
            key = f"login-attempt:{digest}"
            attempts = cache.get(key, 0)
            if attempts >= settings.AUTH_RATE_LIMIT_MAX:
                response = JsonResponse({"error": "Too many sign-in attempts. Try again later."}, status=429)
                response.headers["Retry-After"] = str(settings.AUTH_RATE_LIMIT_WINDOW)
                return response
            cache.set(key, attempts + 1, settings.AUTH_RATE_LIMIT_WINDOW)
        response = self.get_response(request)
        if key and 300 <= response.status_code < 400:
            cache.delete(key)
        return response


class RequiredMfaMiddleware:
    """Require configured roles to enroll a TOTP factor before business access."""

    def __init__(self, get_response): self.get_response=get_response

    def __call__(self, request):
        if request.user.is_authenticated and not request.path.startswith(("/accounts/2fa/", "/accounts/logout/", "/static/")):
            membership=request.user.organization_memberships.filter(active=True).select_related("organization").first()
            if membership and membership.role in membership.organization.mfa_required_roles:
                from allauth.mfa.utils import is_mfa_enabled
                if not is_mfa_enabled(request.user): return redirect(reverse("mfa_activate_totp"))
        return self.get_response(request)


class ResponseSecurityHeadersMiddleware:
    """Apply a conservative browser policy without relying on a front proxy."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; "
            "connect-src 'self'; manifest-src 'self'; worker-src 'self'",
        )
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(self), microphone=(), geolocation=(self), payment=(), usb=()",
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        return response
