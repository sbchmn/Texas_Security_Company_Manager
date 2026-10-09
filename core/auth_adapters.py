"""Allauth policies for the application's invitation-only account creation model."""

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter


class InvitationOnlyAccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        # Accounts are created only by accepting a company-issued invitation.
        return False


class InvitationOnlySocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        # Existing linked social accounts take allauth's normal login path. This hook
        # refuses only creation of a new local account through an uninvited SSO identity.
        return False
