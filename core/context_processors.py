def organization_brand(request):
    membership = getattr(request, "membership", None)
    if not membership and getattr(request, "user", None) and request.user.is_authenticated:
        membership = request.user.organization_memberships.filter(active=True).select_related("organization").first()
    organization = membership.organization if membership else None
    from django.conf import settings
    return {"current_organization": organization, "current_membership": membership, "google_sso_enabled": bool(settings.SOCIALACCOUNT_PROVIDERS["google"]["APP"]["client_id"]), "microsoft_sso_enabled": bool(settings.SOCIALACCOUNT_PROVIDERS["microsoft"]["APP"]["client_id"])}
