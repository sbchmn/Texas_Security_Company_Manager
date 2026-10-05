import hashlib
import ipaddress
import re

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse


def client_ip(request):
    """Return the client address, honouring X-Forwarded-For only from configured proxy peers.

    Behind Caddy or the App Platform edge, REMOTE_ADDR is the proxy itself; without this the
    login throttle would key every visitor of the deployment to one shared bucket.
    """
    peer = (request.META.get("REMOTE_ADDR") or "").strip()

    def trusted(value):
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return False
        return any(address in ipaddress.ip_network(network, strict=False) for network in settings.TRUSTED_PROXIES)

    if settings.TRUSTED_PROXIES and peer and trusted(peer):
        for hop in reversed((request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")):
            hop = hop.strip()
            if hop and not trusted(hop):
                return hop
    return peer


class LoginRateLimitMiddleware:
    """Bound local-login guessing without storing submitted identifiers in cache keys."""

    LOGIN_PATHS = ("/accounts/login/", "/admin/login/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        key = None
        if request.method == "POST" and request.path.rstrip("/") in (path.rstrip("/") for path in self.LOGIN_PATHS):
            submitted_login=request.POST.get("login") or request.POST.get("username", "")
            identity = f"{client_ip(request)}|{submitted_login.casefold()}"
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


class AdminAccessMiddleware:
    """Restrict the Django admin to operator source ranges; it is a platform surface, not a
    tenant one. Fail closed when production has not declared any range."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/admin" or request.path.startswith("/admin/"):
            allowed = settings.ADMIN_ALLOWED_IPS
            if not allowed and not settings.DEBUG:
                return HttpResponseForbidden("The administration console is not enabled for any source address.")
            if allowed:
                address = client_ip(request)
                for network in allowed:
                    try:
                        if ipaddress.ip_address(address) in ipaddress.ip_network(network, strict=False):
                            break
                    except ValueError:
                        continue
                else:
                    return HttpResponseForbidden("The administration console is restricted by source address.")
        return self.get_response(request)


class VerifiedHostMiddleware:
    """Reject unknown hosts before any redirect or tenant resolution occurs."""

    # Orchestrator and container health probes present the pod/container address, never a
    # tenant hostname. These two routes expose no tenant data, no redirects, and no session,
    # so they are answered before the host check; every other path stays strict.
    HOST_EXEMPT_PATHS = ("/healthz", "/readyz")

    def __init__(self,get_response): self.get_response=get_response
    def __call__(self,request):
        if request.path in self.HOST_EXEMPT_PATHS:
            # Answer probes before both the host allowlist and the HTTPS redirect: an
            # orchestrator or container healthcheck probes the plain-HTTP loopback address,
            # which is never a tenant hostname and can follow no redirect. These routes carry
            # no cookies, no tenant data, and no absolute URIs, so pretending the scheme is
            # https only suppresses the 301 a prober would never be able to traverse.
            request.META["wsgi.url_scheme"] = "https"
            request.normalized_host=""
            return self.get_response(request)
        raw=(request.META.get("HTTP_HOST") or request.META.get("SERVER_NAME","")).split(":",1)[0].rstrip(".").lower()
        if not re.fullmatch(r"[a-z0-9.-]{1,253}",raw): return HttpResponseBadRequest("Invalid host")
        configured={item.lower().lstrip(".") for item in settings.PLATFORM_HOSTS}
        known=raw in configured or any(raw.endswith("."+host) for host in configured)
        if not known:
            from .models import OrganizationDomain
            known=OrganizationDomain.objects.filter(hostname=raw,verified=True,status=OrganizationDomain.Status.VERIFIED).exists()
        if not known: return HttpResponseBadRequest("Unknown host")
        request.normalized_host=raw
        return self.get_response(request)


class TenantContextMiddleware:
    """Resolve the active organization from a verified host or explicit user selection."""
    def __init__(self,get_response): self.get_response=get_response
    def __call__(self,request):
        request.membership=None;request.organization=None
        if request.user.is_authenticated:
            from .models import Membership,OrganizationDomain
            memberships=Membership.objects.filter(user=request.user,active=True).select_related("organization")
            host=getattr(request,"normalized_host","");domain=OrganizationDomain.objects.filter(hostname=host,verified=True,status=OrganizationDomain.Status.VERIFIED).select_related("organization").first()
            selected=request.session.get("active_organization_id")
            if domain:
                membership=memberships.filter(organization=domain.organization).first()
            else:
                membership=memberships.filter(organization_id=selected).first() if selected else None
                # No explicit choice yet: land on the earliest membership so the default
                # tenant does not depend on row insertion order from the database.
                membership=membership or memberships.order_by("created_at","id").first()
            if membership:
                request.membership=membership;request.organization=membership.organization
                request.session["active_organization_id"]=str(membership.organization_id)
        return self.get_response(request)


class RequiredMfaMiddleware:
    """Require configured roles to enroll a TOTP factor before business access."""

    # allauth only accepts the enrollment POST for a recently authenticated session, and sends
    # anyone past ACCOUNT_REAUTHENTICATION_TIMEOUT (300s) to /accounts/reauthenticate/ first.
    # Gating that page back to enrollment closes the loop -- activate redirects to reauthenticate
    # which redirects to activate -- and the browser ends on the enrollment page with a fresh,
    # different secret on every load, so the factor could never be added.
    ENROLLMENT_PATHS = ("/accounts/2fa/", "/accounts/reauthenticate/", "/accounts/logout/", "/static/")
    # Background resource redirects also GET enrollment and replace the session secret
    # behind the visible QR. Exempt only these shell reads; their view guards still apply.
    ENROLLMENT_RESOURCE_PATHS = frozenset({
        "/theme.css", "/logo", "/manifest.webmanifest", "/service-worker.js",
    })

    def __init__(self, get_response): self.get_response=get_response

    def __call__(self, request):
        enrollment_resource = (
            request.method in ("GET", "HEAD") and request.path in self.ENROLLMENT_RESOURCE_PATHS
        )
        if request.user.is_authenticated and not enrollment_resource and not request.path.startswith(self.ENROLLMENT_PATHS):
            membership=getattr(request,"membership",None)
            # Platform accounts (Django admin) carry no tenant membership, so the tenant role
            # list alone would leave the most privileged surface without a second factor.
            privileged=request.user.is_staff or request.user.is_superuser
            required=privileged or bool(membership and membership.role in membership.organization.mfa_required_roles)
            if required:
                from allauth.mfa.utils import is_mfa_enabled
                if not is_mfa_enabled(request.user): return redirect(reverse("mfa_activate_totp"))
        return self.get_response(request)


class ResponseSecurityHeadersMiddleware:
    """Apply a conservative browser policy without relying on a front proxy."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        img_src = " ".join(["'self'", "data:", *settings.MEDIA_IMG_SRC])
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'self'; "
            f"img-src {img_src}; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; manifest-src 'self'; worker-src 'self'",
        )
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(self), microphone=(), geolocation=(self), payment=(), usb=()",
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        return response
