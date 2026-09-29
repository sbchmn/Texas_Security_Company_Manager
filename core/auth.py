from functools import wraps
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from .models import Membership

def membership_required(*roles):
    def decorator(view):
        @login_required
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            membership = getattr(request,"membership",None)
            if membership is None:
                membership = (Membership.objects.select_related("organization")
                              .filter(user=request.user, active=True).first())
            if not membership:
                raise PermissionDenied("Your account is not assigned to an organization.")
            if roles and membership.role not in roles:
                raise PermissionDenied
            request.membership = membership
            request.organization = membership.organization
            return view(request, *args, **kwargs)
        return wrapped
    return decorator
