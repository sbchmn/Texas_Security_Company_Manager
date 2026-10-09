import os

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q

from core.models import AuditEvent, Branch, Membership, Organization


class Command(BaseCommand):
    help = "Create the first organization owner from BOOTSTRAP_* environment variables."

    @transaction.atomic
    def handle(self, *args, **options):
        email = os.getenv("BOOTSTRAP_EMAIL", "").strip().casefold()
        password = os.getenv("BOOTSTRAP_PASSWORD", "")
        name = os.getenv("BOOTSTRAP_COMPANY", "Lone Star Security").strip()
        slug = os.getenv("BOOTSTRAP_SLUG", "lone-star-security").strip()
        if not email or not password:
            raise CommandError("BOOTSTRAP_EMAIL and BOOTSTRAP_PASSWORD are required")
        try:
            validate_email(email)
        except ValidationError as exc:
            raise CommandError("BOOTSTRAP_EMAIL must be a valid email address") from exc

        User = get_user_model()
        matches = list(User.objects.filter(
            Q(username__iexact=email) | Q(email__iexact=email),
        ).order_by("pk")[:2])
        if len(matches) > 1:
            raise CommandError("Multiple accounts match BOOTSTRAP_EMAIL; refusing provisioning")

        org = Organization.objects.filter(slug=slug).first()
        if matches:
            user = matches[0]
            identity_matches = (
                (user.username or "").casefold() == email
                and (user.email or "").casefold() == email
            )
            audit_exists = bool(org and AuditEvent.objects.filter(
                organization=org,
                action="organization.bootstrapped",
                target_type="organization",
                target_id=str(org.pk),
                actor=user,
            ).exists())
            verified_email_exists = EmailAddress.objects.filter(
                user=user, email__iexact=email, verified=True, primary=True,
            ).exists()
            owner_membership_exists = bool(org and Membership.objects.filter(
                organization=org, user=user, role=Membership.Role.OWNER, active=True,
            ).exists())
            if not (
                identity_matches and user.is_staff and org and audit_exists
                and verified_email_exists and owner_membership_exists
            ):
                raise CommandError(
                    "A matching account already exists but its bootstrap provenance "
                    "cannot be verified; refusing to elevate or modify it"
                )
            # Provenance is sufficient for an idempotent rerun. In particular, do not
            # reset the account password or repair email/membership state implicitly.
            Branch.objects.get_or_create(organization=org, name="Main Branch")
            self.stdout.write(self.style.SUCCESS(
                f"Owner {email} is ready for {org.display_name}"
            ))
            return

        # A different identity cannot reuse a tenant's existing bootstrap marker to
        # replace its first owner. This check occurs before creating the new account.
        if org and AuditEvent.objects.filter(
            organization=org, action="organization.bootstrapped",
            target_type="organization", target_id=str(org.pk),
        ).exists():
            raise CommandError(
                "This organization already has bootstrap history; refusing to replace "
                "its provisioned identity"
            )

        user = User(username=email, email=email, is_staff=True)
        user.set_password(password)
        user.save()
        # Mark only a newly operator-provisioned identity verified; an existing account
        # must meet the provenance check above and is never silently repaired.
        EmailAddress.objects.update_or_create(
            user=user,
            email=email,
            defaults={"verified": True, "primary": True},
        )
        if org is None:
            org = Organization.objects.create(
                legal_name=name, display_name=name, slug=slug,
            )
        Membership.objects.create(
            organization=org, user=user, role=Membership.Role.OWNER, active=True,
        )
        Branch.objects.get_or_create(organization=org, name="Main Branch")
        AuditEvent.objects.create(
            organization=org,
            action="organization.bootstrapped",
            target_type="organization",
            target_id=str(org.pk),
            actor=user,
        )
        self.stdout.write(self.style.SUCCESS(
            f"Owner {email} is ready for {org.display_name}"
        ))
