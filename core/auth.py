from functools import wraps
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

def membership_required(*roles):
    def decorator(view):
        @login_required
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            # TenantContextMiddleware is the only resolver: it is what honours a verified
            # custom hostname, which pins the tenant even when the user belongs to others.
            membership = getattr(request, "membership", None)
            if membership is None:
                raise PermissionDenied("Your account is not assigned to an organization.")
            if roles and membership.role not in roles:
                raise PermissionDenied
            return view(request, *args, **kwargs)
        return wrapped
    return decorator
