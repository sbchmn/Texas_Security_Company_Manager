import os
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from allauth.account.models import EmailAddress
from allauth.core.exceptions import SignupClosedException
from allauth.socialaccount.internal.flows.signup import process_signup
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import include, path, reverse
from django.utils import timezone

from core.models import (
    AuditEvent, Membership, MembershipInvitation, Organization,
)


def existing_social_login(request):
    """Exercise allauth's real existing-account branch without an external provider."""
    from allauth.socialaccount.internal.flows.login import complete_login

    sociallogin = SocialLogin(
        account=SocialAccount(provider="google", uid="linked-existing-account"),
    )
    return complete_login(request, sociallogin)


urlpatterns = [
    path("accounts/", include("allauth.urls")),
    path("", include("core.urls")),
    path("_test/existing-social-login/", existing_social_login),
]


@override_settings(ROOT_URLCONF=__name__)
class InvitationOnlySignupTests(TestCase):
    def test_local_signup_post_is_closed_without_creating_an_account(self):
        response = self.client.post(reverse("account_signup"), {
            "email": "uninvited-local@example.com",
            "username": "uninvited-local@example.com",
            "password1": "A sufficiently long password",
            "password2": "A sufficiently long password",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.filter(
            username="uninvited-local@example.com",
        ).exists())

    def test_social_signup_flow_closes_before_persisting_a_new_account(self):
        User = get_user_model()
        sociallogin = SocialLogin(
            user=User(username="uninvited-social@example.com"),
            account=SocialAccount(
                provider="google", uid="uninvited-new-social-identity",
            ),
        )
        request = RequestFactory().post("/accounts/google/login/callback/")
        with self.assertRaises(SignupClosedException):
            process_signup(request, sociallogin)
        self.assertFalse(User.objects.filter(
            username="uninvited-social@example.com",
        ).exists())

    def test_linked_existing_social_account_still_uses_allauth_login_flow(self):
        User = get_user_model()
        user = User.objects.create_user(
            username="existing-sso@example.com",
            email="existing-sso@example.com",
            password="existing local password",
        )
        SocialAccount.objects.create(
            user=user, provider="google", uid="linked-existing-account",
        )

        response = self.client.get("/_test/existing-social-login/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            str(self.client.session["_auth_user_id"]), str(user.pk),
        )

    def test_valid_invitation_still_creates_and_authenticates_the_invited_user(self):
        owner = get_user_model().objects.create_user(
            username="signup-owner@example.com", password="owner password",
        )
        organization = Organization.objects.create(
            legal_name="Invitation LLC", display_name="Invitation", slug="invitation-signup",
        )
        Membership.objects.create(
            user=owner, organization=organization, role=Membership.Role.OWNER,
        )
        invitation, token = MembershipInvitation.issue(
            organization=organization, email="invited-user@example.com",
            role=Membership.Role.ADMIN, invited_by=owner,
            expires_at=timezone.now() + timedelta(hours=72),
        )

        response = self.client.post(reverse("invitation_accept", args=[token]), {
            "first_name": "Invited", "last_name": "User",
            "password": "A sufficiently long passphrase",
            "password_confirmation": "A sufficiently long passphrase",
        })

        self.assertEqual(response.status_code, 302)
        user = get_user_model().objects.get(email="invited-user@example.com")
        self.assertEqual(str(self.client.session["_auth_user_id"]), str(user.pk))
        self.assertTrue(Membership.objects.filter(
            organization=organization, user=user, role=Membership.Role.ADMIN,
            active=True,
        ).exists())
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.accepted_at)


class BootstrapPreclaimTests(TestCase):
    @staticmethod
    def environment(email="bootstrap-operator@example.com", password="bootstrap test password"):
        return {
            "BOOTSTRAP_EMAIL": email,
            "BOOTSTRAP_PASSWORD": password,
            "BOOTSTRAP_COMPANY": "Bootstrap Test LLC",
            "BOOTSTRAP_SLUG": "bootstrap-test-company",
        }

    def test_mismatched_preclaimed_username_is_refused_without_verification_or_elevation(self):
        User = get_user_model()
        attacker = User.objects.create_user(
            username="bootstrap-operator@example.com",
            email="preclaimed-mailbox@example.net",
            password="attacker's existing password",
        )
        EmailAddress.objects.create(
            user=attacker, email=attacker.email, verified=False, primary=True,
        )

        with patch.dict(os.environ, self.environment()):
            with self.assertRaises(CommandError):
                call_command("bootstrap_admin", stdout=StringIO())

        attacker.refresh_from_db()
        self.assertTrue(attacker.check_password("attacker's existing password"))
        self.assertFalse(Membership.objects.filter(user=attacker).exists())
        self.assertFalse(EmailAddress.objects.filter(
            user=attacker, email__iexact="bootstrap-operator@example.com",
            verified=True,
        ).exists())
        self.assertFalse(Organization.objects.filter(
            slug="bootstrap-test-company",
        ).exists())

    def test_initial_provision_and_proven_rerun_are_idempotent_without_password_reset(self):
        User = get_user_model()
        with patch.dict(os.environ, self.environment()):
            call_command("bootstrap_admin", stdout=StringIO())

        user = User.objects.get(username="bootstrap-operator@example.com")
        original_password_hash = user.password
        self.assertTrue(user.check_password("bootstrap test password"))
        with patch.dict(os.environ, self.environment(password="different rerun password")):
            call_command("bootstrap_admin", stdout=StringIO())

        user.refresh_from_db()
        self.assertEqual(user.password, original_password_hash)
        self.assertTrue(user.check_password("bootstrap test password"))
        self.assertEqual(Membership.objects.filter(
            user=user, role=Membership.Role.OWNER, active=True,
        ).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(
            action="organization.bootstrapped", actor=user,
        ).count(), 1)

    def test_matching_but_unproven_existing_account_is_not_elevated(self):
        User = get_user_model()
        existing = User.objects.create_user(
            username="bootstrap-operator@example.com",
            email="bootstrap-operator@example.com",
            password="existing account password",
        )
        EmailAddress.objects.create(
            user=existing, email=existing.email, verified=True, primary=True,
        )

        with patch.dict(os.environ, self.environment()):
            with self.assertRaises(CommandError):
                call_command("bootstrap_admin", stdout=StringIO())

        self.assertFalse(Membership.objects.filter(user=existing).exists())
        self.assertFalse(Organization.objects.filter(
            slug="bootstrap-test-company",
        ).exists())
